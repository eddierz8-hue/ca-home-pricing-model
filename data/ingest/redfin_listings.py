"""
Redfin individual sold-listing scraper.
Uses Redfin's public GIS CSV download endpoint (same as the "Download" button
on their search results page — no API key, within ToS for research).

Discovers region IDs via Redfin's autocomplete API, then downloads up to
350 sold listings per page across all target cities. Loads into `properties`
and `sales` tables.
"""

import io
import sys
import pathlib
import logging
import time
from datetime import date

import pandas as pd
import requests

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from db import execute, executemany

log = logging.getLogger(__name__)

TARGET_CITIES = [
    "Berkeley CA", "Orinda CA", "Moraga CA", "Lafayette CA",
    "Walnut Creek CA", "Alamo CA", "Danville CA", "San Ramon CA",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.redfin.com/",
}

AUTOCOMPLETE_URL = "https://www.redfin.com/stingray/do/query-location-autocomplete"
DOWNLOAD_URL     = "https://www.redfin.com/stingray/api/gis-csv"


def _discover_region_id(city_state: str) -> tuple[str, str] | None:
    """Return (region_id, region_type) for a city string like 'Danville CA'."""
    try:
        resp = requests.get(
            AUTOCOMPLETE_URL,
            params={"location": city_state, "v": 2},
            headers=HEADERS, timeout=15,
        )
        text = resp.text
        # Response is prefixed with '{}&&' — strip it
        if text.startswith("{}&&"):
            text = text[4:]
        import json
        data = json.loads(text)
        for item in data.get("payload", {}).get("sections", []):
            for row in item.get("rows", []):
                if row.get("type") == "2":  # city
                    return row["id"].split("_")[1], "2"
        return None
    except Exception as e:
        log.warning(f"Autocomplete failed for {city_state}: {e}")
        return None


def _download_sold(region_id: str, region_type: str = "2",
                   sold_within_days: int = 1095, page: int = 1) -> pd.DataFrame:
    params = {
        "al": 1,
        "market": "sanfrancisco",
        "num_homes": 350,
        "ord": "redfin-recommended-asc",
        "page_number": page,
        "region_id": region_id,
        "region_type": region_type,
        "sold_within_days": sold_within_days,
        "status": 9,        # sold
        "uipt": "1,2,3,4,5,6,7,8",
        "v": 8,
    }
    resp = requests.get(DOWNLOAD_URL, params=params, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    text = resp.text
    if text.startswith("{}&&"):
        text = text[4:]
    try:
        df = pd.read_csv(io.StringIO(text))
        return df
    except Exception:
        return pd.DataFrame()


def _upsert_property(row: pd.Series) -> int | None:
    """Insert or update property record, return property_id."""
    address = str(row.get("ADDRESS", "")).strip()
    zip_code = str(row.get("ZIP OR POSTAL CODE", "")).strip().split("-")[0][:5]
    city = str(row.get("CITY", "")).strip()

    if not address or not zip_code or len(zip_code) != 5:
        return None

    try:
        beds  = int(float(row["BEDS"])) if pd.notna(row.get("BEDS")) else None
        baths = float(row["BATHS"]) if pd.notna(row.get("BATHS")) else None
        sqft  = int(float(row["SQUARE FEET"])) if pd.notna(row.get("SQUARE FEET")) else None
        lot   = int(float(row["LOT SIZE"])) if pd.notna(row.get("LOT SIZE")) else None
        yr    = int(float(row["YEAR BUILT"])) if pd.notna(row.get("YEAR BUILT")) else None
    except (ValueError, KeyError):
        beds = baths = sqft = lot = yr = None

    sql = """
        INSERT INTO properties (address, city, zip_code, bedrooms, bathrooms,
                                sqft_living, sqft_lot, year_built)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            bedrooms    = COALESCE(VALUES(bedrooms),    bedrooms),
            bathrooms   = COALESCE(VALUES(bathrooms),   bathrooms),
            sqft_living = COALESCE(VALUES(sqft_living), sqft_living),
            sqft_lot    = COALESCE(VALUES(sqft_lot),    sqft_lot),
            year_built  = COALESCE(VALUES(year_built),  year_built)
    """
    execute(sql, (address, city, zip_code, beds, baths, sqft, lot, yr))

    result = execute(
        "SELECT id FROM properties WHERE address=%s AND zip_code=%s",
        (address, zip_code), fetch=True,
    )
    return result[0]["id"] if result else None


def _upsert_sale(property_id: int, row: pd.Series):
    try:
        sale_price = int(float(row["PRICE"])) if pd.notna(row.get("PRICE")) else None
        list_price = int(float(row["ORIGINAL LIST PRICE"])) if pd.notna(row.get("ORIGINAL LIST PRICE")) else None
        sale_date  = pd.to_datetime(row.get("SOLD DATE"), errors="coerce")
        dom        = int(float(row["DAYS ON MARKET"])) if pd.notna(row.get("DAYS ON MARKET")) else None
    except (ValueError, KeyError):
        return

    if not sale_price or not sale_date or pd.isna(sale_date):
        return

    l2s = round(sale_price / list_price, 4) if list_price and list_price > 0 else None

    execute("""
        INSERT IGNORE INTO sales
            (property_id, source, sale_date, sale_price, list_price,
             list_to_sale_ratio, days_on_market)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (property_id, "redfin", sale_date.date().isoformat(),
          sale_price, list_price, l2s, dom))


def ingest_city(city_state: str, sold_within_days: int = 1095):
    log.info(f"Discovering region ID for {city_state}")
    result = _discover_region_id(city_state)
    if not result:
        log.warning(f"Could not find region ID for {city_state}, skipping")
        return 0

    region_id, region_type = result
    log.info(f"{city_state} → region_id={region_id}")

    total = 0
    for page in range(1, 6):   # up to 5 pages × 350 = 1750 listings per city
        try:
            df = _download_sold(region_id, region_type, sold_within_days, page)
        except Exception as e:
            log.warning(f"{city_state} page {page} download failed: {e}")
            break

        if df.empty or len(df) < 2:
            break

        # First row is often a header artifact
        df.columns = [c.strip() for c in df.columns]

        for _, row in df.iterrows():
            prop_id = _upsert_property(row)
            if prop_id:
                _upsert_sale(prop_id, row)
                total += 1

        log.info(f"{city_state} page {page}: {len(df)} records processed")
        if len(df) < 350:
            break
        time.sleep(2)

    log.info(f"{city_state}: {total} sales loaded")
    return total


def run_all(sold_within_days: int = 1095):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    grand_total = 0
    for city in TARGET_CITIES:
        grand_total += ingest_city(city, sold_within_days)
        time.sleep(3)
    log.info(f"Redfin listings ingest complete — {grand_total} total sales loaded")


if __name__ == "__main__":
    run_all()
