"""
Home Pricing API — FastAPI
POST /price  →  pricing suggestion for a property
GET  /health →  liveness check
GET  /market/{zip_code} → current market snapshot
"""

import sys
import pathlib
import logging
from typing import Optional

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, model_validator

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from model.predict import predict_for_zip
from db import execute

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(
    title="CA Home Pricing API",
    description="Pricing suggestions for East Bay CA residential homes",
    version="0.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_dashboard = pathlib.Path(__file__).parent.parent / "dashboard"


@app.get("/", include_in_schema=False)
def serve_dashboard():
    return FileResponse(_dashboard / "index.html")


ZIP_TO_CITY = {
    "94702": "Berkeley",  "94703": "Berkeley",  "94704": "Berkeley",
    "94705": "Berkeley",  "94706": "Berkeley",  "94707": "Berkeley",
    "94708": "Berkeley",  "94709": "Berkeley",  "94710": "Berkeley",
    "94563": "Orinda",
    "94556": "Moraga",
    "94549": "Lafayette",
    "94595": "Walnut Creek", "94596": "Walnut Creek",
    "94597": "Walnut Creek", "94598": "Walnut Creek",
    "94507": "Alamo",
    "94506": "Danville",  "94526": "Danville",
    "94582": "San Ramon", "94583": "San Ramon",
}


class PriceRequest(BaseModel):
    # ── Location — provide address OR zip_code (address takes priority) ────────
    address:     Optional[str] = Field(None, description="Full street address, e.g. '123 Main St, Danville, CA'")
    zip_code:    Optional[str] = Field(None, description="5-digit zip code (used if address not provided)")

    # ── Property characteristics ───────────────────────────────────────────────
    sqft_living:  Optional[int]   = Field(None, description="Interior square footage")
    sqft_lot:     Optional[int]   = Field(None, description="Lot square footage")
    bedrooms:     Optional[int]   = Field(None, ge=0, le=20)
    bathrooms:    Optional[float] = Field(None, ge=0, le=20)
    year_built:   Optional[int]   = Field(None, ge=1800, le=2026)
    pool:         Optional[bool]  = False
    view_score:   Optional[int]   = Field(0, ge=0, le=2, description="0=none 1=partial 2=panoramic")
    corner_lot:   Optional[bool]  = False
    busy_street:  Optional[bool]  = False
    slope_grade:  Optional[int]   = Field(0, ge=0, le=100, description="Slope severity 0–100")
    hoa_monthly:  Optional[int]   = Field(0, ge=0)

    # ── Listing signals ────────────────────────────────────────────────────────
    current_list_price:  Optional[int]  = Field(None, ge=0, description="Current asking price")
    original_list_price: Optional[int]  = Field(None, ge=0, description="Original list price (before any reductions)")
    days_on_market:      Optional[int]  = Field(None, ge=0, description="Days this listing has been active")
    previously_removed:  Optional[bool] = Field(False, description="Was this listing previously withdrawn/expired and re-listed?")

    # ── Comparable homes ──────────────────────────────────────────────────────
    comps: Optional[list[str]] = Field(
        None,
        max_length=5,
        description="Up to 5 comparable property addresses",
    )

    @model_validator(mode="after")
    def require_location(self):
        if not self.address and not self.zip_code:
            raise ValueError("Provide either 'address' or 'zip_code'")
        return self


class PriceResponse(BaseModel):
    zip_code:           str
    city:               str
    as_of:              str
    address:            Optional[str]
    ml_estimate:        int
    adjusted_estimate:  int
    confidence_low:     int
    confidence_high:    int
    suggested_offer:    int
    offer_strategy:     str
    property_factors:   dict
    combined_factor:    float
    top_ml_drivers:     dict
    market_context:     dict
    listing_analysis:   dict
    comp_analysis:      Optional[dict]


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/cities")
def list_cities():
    rows = execute("SELECT city, zip_codes FROM target_cities WHERE is_active = 1 ORDER BY city",
                   fetch=True)
    return {"cities": rows}


@app.get("/market/{zip_code}")
def market_snapshot(zip_code: str):
    rows = execute("""
        SELECT metric_date, city, median_list_price, median_sale_price,
               median_dom, active_listings, list_to_sale_ratio, price_cut_pct,
               new_listings, sold_count, source
        FROM market_metrics
        WHERE zip_code = %s
        ORDER BY metric_date DESC
        LIMIT 6
    """, (zip_code,), fetch=True)
    if not rows:
        raise HTTPException(404, f"No market data for zip {zip_code}")
    return {"zip_code": zip_code, "city": ZIP_TO_CITY.get(zip_code), "recent": rows}


@app.post("/price", response_model=PriceResponse)
def price_address(req: PriceRequest):
    resolved_address = req.address

    # ── Resolve zip + city ────────────────────────────────────────────────────
    if req.address:
        try:
            from data.geocode import geocode
            geo = geocode(req.address)
        except Exception as e:
            log.warning(f"Geocoding failed for '{req.address}': {e}")
            geo = {}

        if not geo or not geo.get("in_target"):
            if req.zip_code:
                zip_code = req.zip_code.strip()[:5]
                log.info(f"Geocoding failed/out-of-area — falling back to provided zip {zip_code}")
            else:
                detail = (
                    f"Could not geocode '{req.address}' to a supported area. "
                    "Provide a zip_code as fallback or check the address."
                )
                raise HTTPException(422, detail)
        else:
            zip_code = geo["zip_code"]
    else:
        zip_code = req.zip_code.strip().split("-")[0][:5]

    city = ZIP_TO_CITY.get(zip_code)
    if not city:
        raise HTTPException(
            400,
            f"Zip {zip_code} is not in our target area. "
            f"Supported zips: {sorted(ZIP_TO_CITY.keys())}",
        )

    # ── Build property dict ───────────────────────────────────────────────────
    listing_fields = {"address", "zip_code", "current_list_price", "original_list_price",
                      "days_on_market", "previously_removed", "comps"}
    prop = {k: v for k, v in req.model_dump().items()
            if k not in listing_fields and v is not None and v is not False}

    # ── Core prediction ───────────────────────────────────────────────────────
    try:
        result = predict_for_zip(
            zip_code, city, prop,
            current_list_price=req.current_list_price,
            original_list_price=req.original_list_price,
            days_on_market=req.days_on_market,
            previously_removed=req.previously_removed or False,
        )
    except FileNotFoundError:
        raise HTTPException(503, "Model not trained yet. Run: python -m model.train")
    except Exception as e:
        log.exception("Prediction error")
        raise HTTPException(500, str(e))

    if "error" in result:
        raise HTTPException(422, result["error"])

    # ── Comp analysis ─────────────────────────────────────────────────────────
    comp_analysis = None
    if req.comps:
        try:
            from model.comps import analyze_comps
            comp_analysis = analyze_comps(req.comps, prop, result["adjusted_estimate"])
            # If we got valid comps, use blended price as suggested offer
            if comp_analysis.get("n_valid", 0) > 0:
                result["suggested_offer"] = comp_analysis["blended_price"]
        except Exception as e:
            log.warning(f"Comp analysis failed: {e}")

    result["address"]      = resolved_address
    result["comp_analysis"] = comp_analysis

    return result


@app.get("/predictions/recent")
def recent_predictions(limit: int = 20):
    rows = execute("""
        SELECT id, run_date, model_version, hedonic_estimate, ml_estimate,
               ensemble_estimate, confidence_low, confidence_high,
               suggested_offer, offer_strategy, notes
        FROM model_predictions
        ORDER BY run_date DESC, id DESC
        LIMIT %s
    """, (limit,), fetch=True)
    return {"predictions": rows}


@app.get("/flags/active")
def active_flags():
    rows = execute("""
        SELECT pf.flag_date, pf.flag_type, pf.flag_detail,
               p.address, p.city, p.zip_code
        FROM property_flags pf
        JOIN properties p ON p.id = pf.property_id
        WHERE pf.is_active = TRUE
        ORDER BY pf.flag_date DESC
        LIMIT 50
    """, fetch=True)
    return {"flags": rows}


@app.get("/zhvi/{zip_code}")
def zhvi_trend(zip_code: str, months: int = 24):
    rows = execute("""
        SELECT metric_date, home_value, source
        FROM zhvi
        WHERE zip_code = %s
          AND metric_date >= DATE_SUB(CURDATE(), INTERVAL %s MONTH)
        ORDER BY metric_date ASC
    """, (zip_code, months), fetch=True)
    return {"zip_code": zip_code, "city": ZIP_TO_CITY.get(zip_code), "series": rows}


@app.get("/dashboard/summary")
def dashboard_summary():
    rows = execute("""
        SELECT
            tc.city,
            tc.zip_codes,
            mm.metric_date,
            ROUND(AVG(mm.median_list_price))     AS median_list_price,
            ROUND(AVG(mm.median_sale_price))      AS median_sale_price,
            ROUND(AVG(mm.median_dom))             AS median_dom,
            ROUND(AVG(mm.list_to_sale_ratio), 4)  AS list_to_sale_ratio,
            ROUND(AVG(mm.active_listings))        AS active_listings,
            ROUND(AVG(mm.price_cut_pct), 2)       AS price_cut_pct
        FROM target_cities tc
        JOIN market_metrics mm
          ON FIND_IN_SET(mm.zip_code, REPLACE(tc.zip_codes, ' ', '')) > 0
        WHERE mm.metric_date = (
            SELECT MAX(m2.metric_date) FROM market_metrics m2
            WHERE FIND_IN_SET(m2.zip_code, REPLACE(tc.zip_codes, ' ', '')) > 0
              AND m2.median_dom IS NOT NULL
        )
        GROUP BY tc.city, tc.zip_codes, mm.metric_date
        ORDER BY tc.city
    """, fetch=True)

    fred_rows = execute("""
        SELECT series_id, value, indicator_date
        FROM economic_indicators e1
        WHERE series_id IN ('MORTGAGE30US','UMCSENT','FEDFUNDS','SFXRSA')
          AND indicator_date = (
              SELECT MAX(indicator_date) FROM economic_indicators e2
              WHERE e2.series_id = e1.series_id
          )
    """, fetch=True)

    return {"cities": rows, "indicators": fred_rows}


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8002, reload=True)
