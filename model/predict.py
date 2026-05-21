"""
Full prediction pipeline.
Input:  address string + optional property overrides
Output: price estimate, confidence range, offer suggestion, key factors

Architecture:
  1. Geocode address → zip code
  2. Pull latest market context for that zip from DB
  3. ML ensemble → market baseline price
  4. Hedonic adjustments → property-specific multipliers
  5. Blend → suggested offer price + offer strategy
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

# City median property characteristics (from Census ACS for these East Bay cities)
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
        WHERE zip_code = %s AND source = 'zhvi_sfr_upper'
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
        "home_value":  float(latest["home_value"]),
        "zhvi_lag1":   float(lag1_val),
        "zhvi_lag3":   float(lag3_val),
        "zhvi_mom":    float(mom),
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


def _offer_strategy(market: dict) -> str:
    dom          = market.get("median_dom", 30)
    lts          = market.get("list_to_sale_ratio", 1.0)
    price_cuts   = market.get("price_cut_pct", 0) or 0

    if dom < 14 and lts >= 1.02:
        return "aggressive"
    if dom > 45 or price_cuts > 0.25:
        return "conservative"
    return "market"


def _offer_price(estimate: float, strategy: str) -> int:
    multipliers = {"aggressive": 1.03, "market": 1.00, "conservative": 0.965}
    return round(estimate * multipliers.get(strategy, 1.0))


def predict_for_zip(
    zip_code: str,
    city: str,
    prop: dict,
) -> dict:
    """
    Full prediction for a known zip + property characteristics.
    prop keys (all optional): sqft_living, sqft_lot, bedrooms, bathrooms,
      year_built, pool, view_score, corner_lot, busy_street, slope_grade, hoa_monthly
    """
    # ── 1. Build feature row from latest market data ──────────────────────────
    fred    = _latest_fred()
    zhvi    = _latest_zhvi(zip_code)
    market  = _latest_market(zip_code)
    sent    = _latest_sentiment(city)

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
        "median_sqft_zip": city_med["sqft"],
        "median_lot_zip":  city_med["lot"],
        "median_beds_zip": city_med["beds"],
        "median_baths_zip":city_med["baths"],
        **prop,
    }
    adj = hedonic.apply_property_adjustments(ml_est, prop_with_medians)

    final_est = adj["adjusted_price"]

    # ── 4. Confidence interval (±10% from ML model) ───────────────────────────
    ci_low  = round(final_est * 0.90)
    ci_high = round(final_est * 1.10)

    # ── 5. Offer strategy ─────────────────────────────────────────────────────
    strategy     = _offer_strategy(market)
    offer_price  = _offer_price(final_est, strategy)

    # ── 6. Market health summary ──────────────────────────────────────────────
    dom    = market.get("median_dom", "?")
    lts    = market.get("list_to_sale_ratio", "?")
    inv    = market.get("active_listings", "?")
    rate   = fred.get("mortgage_rate_30y", "?")
    sent_v = feature_row.get("search_volume_ma4", "?")

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
        "market_context": {
            "median_dom":         dom,
            "list_to_sale_ratio": lts,
            "active_listings":    inv,
            "mortgage_rate_30y":  rate,
            "search_demand_index":sent_v,
        },
    }

    # ── 7. Persist to model_predictions ──────────────────────────────────────
    _save_prediction(result, prop)

    return result


def _save_prediction(result: dict, prop: dict):
    """Persist prediction to model_predictions table for tracking."""
    try:
        feature_blob = json.dumps({**result["market_context"], **result["top_ml_drivers"],
                                   **result["property_factors"]})
        execute("""
            INSERT INTO model_predictions
                (run_date, model_version, hedonic_estimate, ml_estimate,
                 ensemble_estimate, confidence_low, confidence_high,
                 suggested_offer, offer_strategy, feature_json, notes)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            result["as_of"],
            "v0.1",
            result["adjusted_estimate"],
            result["ml_estimate"],
            result["adjusted_estimate"],
            result["confidence_low"],
            result["confidence_high"],
            result["suggested_offer"],
            result["offer_strategy"],
            feature_blob,
            f"{result['city']} {result['zip_code']} | "
            f"sqft={prop.get('sqft_living','?')} beds={prop.get('bedrooms','?')} "
            f"baths={prop.get('bathrooms','?')} yr={prop.get('year_built','?')}",
        ))
    except Exception as e:
        log.warning(f"Could not save prediction to DB: {e}")
