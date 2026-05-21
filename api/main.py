"""
Home Pricing API — FastAPI
POST /price  →  pricing suggestion for an address
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
from pydantic import BaseModel, Field

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from model.predict import predict_for_zip
from db import execute

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

app = FastAPI(
    title="CA Home Pricing API",
    description="Pricing suggestions for East Bay CA residential homes",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Zip → city mapping
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
    zip_code: str = Field(..., description="5-digit zip code")
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


class PriceResponse(BaseModel):
    zip_code:           str
    city:               str
    as_of:              str
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
    zip_code = req.zip_code.strip().split("-")[0][:5]
    city = ZIP_TO_CITY.get(zip_code)
    if not city:
        raise HTTPException(
            400,
            f"Zip {zip_code} is not in our target area. "
            f"Supported zips: {sorted(ZIP_TO_CITY.keys())}"
        )

    prop = {k: v for k, v in req.model_dump().items()
            if k != "zip_code" and v is not None}

    try:
        result = predict_for_zip(zip_code, city, prop)
    except FileNotFoundError:
        raise HTTPException(
            503,
            "Model not trained yet. Run: python -m model.train"
        )
    except Exception as e:
        log.exception("Prediction error")
        raise HTTPException(500, str(e))

    if "error" in result:
        raise HTTPException(422, result["error"])

    return result


if __name__ == "__main__":
    uvicorn.run("api.main:app", host="0.0.0.0", port=8002, reload=True)
