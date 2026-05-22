"""
Full prediction pipeline.
Input:  zip_code + property characteristics + optional listing signals
Output: price estimate, confidence range, offer suggestion, key factors,
        listing analysis, optional comp blend
"""

import sys
import pathlib
import logging
import json
from datetime import date, timedelta

import pandas as pd

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from db import execute
from model import hedonic, ml_model

log = logging.getLogger(__name__)

# City median property characteristics (Census ACS, East Bay)
CITY_MEDIANS = {
    "Berkeley":      {"sqft": 1450, "lot": 4200,  "beds": 3, "baths": 1.5},
    "Orinda":        {"sqft": 2400, "lot": 16000, "beds": 4, "baths": 2.5},
    "Moraga":        {"sqft": 2200, "lot": 10000, "beds": 4, "baths": 2.5},
    "Lafayette":     {"sqft": 2200, "lot": 10000, "beds": 4, "baths": 2.5},
    "Walnut Creek":  {"sqft": 1800, "lot": 6500,  "beds": 3, "baths": 2.0},
    "Alamo":         {"sqft": 3000, "lot": 20000, "beds": 4, "baths": 3.0},
    "Danville":      {"sqft": 2400, "lot": 12000, "beds": 4, "baths": 2.5},
    "San Ramon":     {"sqft": 2000, "lot": 6000,  "beds": 4, "baths": 2.5},
}

DEFAULT_MEDIANS = {"sqft": 1850, "lot": 6500, "beds": 3, "baths": 2.0}

FRED_SERIES = {
    "MORTGAGE30US": "mortgage_rate_30y",
    "MORTGAGE15US": "mortgage_rate_15y",
    "UMCSENT":      "consumer_sentiment",
    "SFXRSA":       "sf_hpi",
    "FEDFUNDS":     "fed_funds_rate",
    "CPIAUCSL":     "cpi",
    "CAUR":         "ca_unemployment",
}


def _latest_fred() -> dict:
    series_list = "', '".join(FRED_SERIES.keys())
    rows = execute(f"""
        SELECT series_id, value
        FROM economic_indicators e1
        WHERE series_id IN ('{series_list}')
          AND indicator_date = (
              SELECT MAX(indicator_date) FROM economic_indicators e2
              WHERE e2.series_id = e1.series_id
          )
    """, fetch=True)
    return {FRED_SERIES[r["series_id"]]: float(r["value"]) for r in rows}


def _latest_zhvi(zip_code: str) -> dict:
    rows = execute("""
        SELECT home_value, metric_date
        FROM zhvi
        WHERE zip_code = %s AND source = 'zhvi_sfr'
        ORDER BY metric_date DESC
        LIMIT 13
    """, (zip_code,), fetch=True)
    if not rows:
        return {}
    latest   = rows[0]
    lag1_val = rows[1]["home_value"] if len(rows) > 1 else latest["home_value"]
    lag3_val = rows[3]["home_value"] if len(rows) > 3 else latest["home_value"]
    mom      = (latest["home_value"] - lag1_val) / lag1_val if lag1_val else 0
    return {
        "home_value": float(latest["home_value"]),
        "zhvi_lag1":  float(lag1_val),
        "zhvi_lag3":  float(lag3_val),
        "zhvi_mom":   float(mom),
    }


def _latest_market(zip_code: str) -> dict:
    rows = execute("""
        SELECT median_list_price, median_sale_price, median_dom,
               active_listings, list_to_sale_ratio, price_cut_pct,
               absorption_rate, sold_count, new_listings, metric_date
        FROM market_metrics
        WHERE zip_code = %s
          AND granularity = 'zip'
          AND median_dom IS NOT NULL
        ORDER BY metric_date DESC
        LIMIT 1
    """, (zip_code,), fetch=True)
    if not rows:
        return {}
    r = rows[0]
    return {
        "median_list_price":  float(r["median_list_price"] or 0),
        "median_sale_price":  float(r["median_sale_price"] or 0),
        "median_dom":         float(r["median_dom"] or 30),
        "active_listings":    float(r["active_listings"] or 0),
        "list_to_sale_ratio": float(r["list_to_sale_ratio"] or 1.0),
        "price_cut_pct":      float(r["price_cut_pct"] or 0),
        "absorption_rate":    float(r["absorption_rate"] or 0),
    }


def _latest_sentiment(city: str) -> dict:
    rows = execute("""
        SELECT AVG(value) AS vol
        FROM consumer_sentiment
        WHERE city = %s
          AND source = 'google_trends'
          AND metric_date >= %s
    """, (city, (date.today() - timedelta(days=28)).isoformat()), fetch=True)
    vol = float(rows[0]["vol"]) if rows and rows[0]["vol"] else 50.0
    return {"search_volume_ma4": vol}


def _offer_strategy(market: dict, property_dom: int | None) -> str:
    """
    Determine offer strategy.
    If property_dom is provided, use it for the DOM test; otherwise fall back
    to the market median DOM.  List-to-sale ratio always comes from market data.
    """
    effective_dom = property_dom if property_dom is not None else market.get("median_dom", 30)
    lts           = market.get("list_to_sale_ratio", 1.0)
    price_cuts    = market.get("price_cut_pct", 0) or 0

    if effective_dom < 14 and lts >= 1.02:
        return "aggressive"
    if effective_dom > 45 or price_cuts > 0.25:
        return "conservative"
    return "market"


def _offer_price(estimate: float, strategy: str, previously_removed: bool) -> int:
    multipliers = {"aggressive": 1.03, "market": 1.00, "conservative": 0.965}
    mult = multipliers.get(strategy, 1.0)
    if previously_removed:
        mult *= 0.97  # -3% back-on-market penalty
    return round(estimate * mult)


def _listing_analysis(
    estimate: float,
    current_list_price: int | None,
    original_list_price: int | None,
    days_on_market: int | None,
    previously_removed: bool,
    market_dom: float,
) -> dict:
    """Produce informational listing signals (no model mutation)."""
    analysis: dict = {
        "previously_removed": previously_removed,
    }

    if current_list_price:
        ratio = current_list_price / estimate
        analysis["current_list_price"]    = current_list_price
        analysis["list_vs_estimate_pct"]  = round((ratio - 1) * 100, 1)
        analysis["list_price_assessment"] = (
            "underpriced" if ratio < 0.95
            else "overpriced" if ratio > 1.05
            else "at_market"
        )

    if current_list_price and original_list_price:
        analysis["original_list_price"] = original_list_price
        if original_list_price > current_list_price:
            reduction = original_list_price - current_list_price
            analysis["price_reduction"]     = reduction
            analysis["price_reduction_pct"] = round(reduction / original_list_price * 100, 1)
        else:
            analysis["price_reduction"]     = 0
            analysis["price_reduction_pct"] = 0.0

    if days_on_market is not None:
        analysis["property_dom"] = days_on_market
        if market_dom:
            analysis["dom_vs_market_pct"] = round(
                (days_on_market - market_dom) / market_dom * 100, 1
            )

    return analysis


def predict_for_zip(
    zip_code: str,
    city: str,
    prop: dict,
    *,
    current_list_price: int | None = None,
    original_list_price: int | None = None,
    days_on_market: int | None = None,
    previously_removed: bool = False,
    _save: bool = True,
) -> dict:
    """
    Full prediction for a known zip + property characteristics + optional listing signals.

    prop keys (all optional): sqft_living, sqft_lot, bedrooms, bathrooms,
      year_built, pool, view_score, corner_lot, busy_street, slope_grade, hoa_monthly

    Listing signal keys:
      current_list_price, original_list_price, days_on_market, previously_removed
    """
    # ── 1. Pull latest market context ────────────────────────────────────────
    fred   = _latest_fred()
    zhvi   = _latest_zhvi(zip_code)
    market = _latest_market(zip_code)
    sent   = _latest_sentiment(city)

    today = date.today()
    feature_row = {
        "zip_code":  zip_code,
        "month_num": today.month,
        "year":      today.year,
        **fred,
        **zhvi,
        **market,
        **sent,
    }

    if not zhvi:
        return {"error": f"No ZHVI data for zip {zip_code}. Run the ingest pipeline first."}

    # ── 2. ML ensemble baseline ───────────────────────────────────────────────
    ml_result = ml_model.predict(feature_row)
    ml_est    = ml_result["ml_estimate"]

    # ── 3. Hedonic property adjustments ──────────────────────────────────────
    city_med = CITY_MEDIANS.get(city, DEFAULT_MEDIANS)
    prop_with_medians = {
        "median_sqft_zip":  city_med["sqft"],
        "median_lot_zip":   city_med["lot"],
        "median_beds_zip":  city_med["beds"],
        "median_baths_zip": city_med["baths"],
        **prop,
    }
    adj       = hedonic.apply_property_adjustments(ml_est, prop_with_medians)
    final_est = adj["adjusted_price"]

    # ── 4. Confidence interval (±10%) ────────────────────────────────────────
    ci_low  = round(final_est * 0.90)
    ci_high = round(final_est * 1.10)

    # ── 5. Offer strategy + price ─────────────────────────────────────────────
    strategy    = _offer_strategy(market, days_on_market)
    offer_price = _offer_price(final_est, strategy, previously_removed)

    # ── 6. Listing signals (informational) ───────────────────────────────────
    listing = _listing_analysis(
        final_est,
        current_list_price,
        original_list_price,
        days_on_market,
        previously_removed,
        market.get("median_dom", 30),
    )

    result = {
        "zip_code":          zip_code,
        "city":              city,
        "as_of":             today.isoformat(),
        "ml_estimate":       ml_est,
        "adjusted_estimate": final_est,
        "confidence_low":    ci_low,
        "confidence_high":   ci_high,
        "suggested_offer":   offer_price,
        "offer_strategy":    strategy,
        "property_factors":  adj["factors"],
        "combined_factor":   adj["combined_factor"],
        "top_ml_drivers":    ml_result.get("top_factors", {}),
        "listing_analysis":  listing,
        "market_context": {
            "median_dom":         market.get("median_dom", "?"),
            "list_to_sale_ratio": market.get("list_to_sale_ratio", "?"),
            "active_listings":    market.get("active_listings", "?"),
            "mortgage_rate_30y":  fred.get("mortgage_rate_30y", "?"),
            "search_demand_index": sent.get("search_volume_ma4", "?"),
        },
    }

    if _save:
        _save_prediction(result, prop)

    return result


def _save_prediction(result: dict, prop: dict):
    try:
        feature_blob = json.dumps({
            **result["market_context"],
            **result["top_ml_drivers"],
            **result["property_factors"],
        })
        listing = result.get("listing_analysis", {})
        notes = (
            f"{result['city']} {result['zip_code']} | "
            f"sqft={prop.get('sqft_living','?')} beds={prop.get('bedrooms','?')} "
            f"baths={prop.get('bathrooms','?')} yr={prop.get('year_built','?')}"
        )
        if listing.get("current_list_price"):
            notes += f" | list=${listing['current_list_price']:,}"
        if listing.get("property_dom") is not None:
            notes += f" dom={listing['property_dom']}d"
        if listing.get("previously_removed"):
            notes += " [BOM]"

        execute("""
            INSERT INTO model_predictions
                (run_date, model_version, hedonic_estimate, ml_estimate,
                 ensemble_estimate, confidence_low, confidence_high,
                 suggested_offer, offer_strategy, feature_json, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            result["as_of"], "v0.2",
            result["adjusted_estimate"],
            result["ml_estimate"],
            result["adjusted_estimate"],
            result["confidence_low"],
            result["confidence_high"],
            result["suggested_offer"],
            result["offer_strategy"],
            feature_blob,
            notes,
        ))
    except Exception as e:
        log.warning(f"Could not save prediction to DB: {e}")
