"""
County Recorder individual transaction ingestor.

STATUS: STUB — individual property sale data is not freely available.
Individual transactions (address, sale price, beds/baths/sqft, sale date)
require one of:
  - ATTOM Data API (paid) — https://api.attomdata.com
  - CoreLogic (paid, institutional)
  - MLS access via a licensed Realtor
  - County FOIA/public records request (Contra Costa Recorder: 925-335-7900)

This module will load a CSV export in the format below once obtained.
Expected CSV columns (case-insensitive):
  address, city, zip_code, sale_date, sale_price, list_price,
  bedrooms, bathrooms, sqft_living, sqft_lot, year_built,
  days_on_market, property_type, source

Usage once data is available:
  python -m data.ingest.county_recorder --file /path/to/sales.csv
"""

import sys
import csv
import pathlib
import logging
import argparse
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from db import execute

log = logging.getLogger(__name__)

REQUIRED_COLS = {"address", "city", "zip_code", "sale_date", "sale_price"}
TARGET_CITIES = {
    "Berkeley", "Orinda", "Moraga", "Lafayette",
    "Walnut Creek", "Alamo", "Danville", "San Ramon",
}


def load_csv(file_path: str, source: str = "county_recorder") -> int:
    """Load a CSV of individual property sales into properties + sales tables."""
    path = pathlib.Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    loaded = skipped = 0
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        cols = {c.lower().strip() for c in (reader.fieldnames or [])}
        missing = REQUIRED_COLS - cols
        if missing:
            raise ValueError(f"CSV missing required columns: {missing}")

        for row in reader:
            row = {k.lower().strip(): v.strip() for k, v in row.items()}
            city = row.get("city", "").title()
            if city not in TARGET_CITIES:
                skipped += 1
                continue

            # Upsert property
            try:
                execute("""
                    INSERT INTO properties (address, city, zip_code, bedrooms, bathrooms,
                                            sqft_living, sqft_lot, year_built)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON DUPLICATE KEY UPDATE
                        bedrooms    = COALESCE(VALUES(bedrooms),    bedrooms),
                        bathrooms   = COALESCE(VALUES(bathrooms),   bathrooms),
                        sqft_living = COALESCE(VALUES(sqft_living), sqft_living),
                        sqft_lot    = COALESCE(VALUES(sqft_lot),    sqft_lot),
                        year_built  = COALESCE(VALUES(year_built),  year_built)
                """, (
                    row["address"], city, row["zip_code"],
                    _int(row.get("bedrooms")), _float(row.get("bathrooms")),
                    _int(row.get("sqft_living")), _int(row.get("sqft_lot")),
                    _int(row.get("year_built")),
                ))
            except Exception as e:
                log.warning(f"Property insert failed {row.get('address')}: {e}")
                skipped += 1
                continue

            prop = execute(
                "SELECT id FROM properties WHERE address=%s AND zip_code=%s",
                (row["address"], row["zip_code"]), fetch=True,
            )
            if not prop:
                skipped += 1
                continue

            prop_id = prop[0]["id"]
            sale_price = _int(row.get("sale_price"))
            list_price = _int(row.get("list_price"))
            sale_date  = _parse_date(row.get("sale_date"))
            if not sale_price or not sale_date:
                skipped += 1
                continue

            l2s = round(sale_price / list_price, 4) if list_price else None
            try:
                execute("""
                    INSERT IGNORE INTO sales
                        (property_id, source, sale_date, sale_price, list_price,
                         list_to_sale_ratio, days_on_market)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (prop_id, source, sale_date, sale_price, list_price,
                      l2s, _int(row.get("days_on_market"))))
                loaded += 1
            except Exception as e:
                log.warning(f"Sale insert failed: {e}")
                skipped += 1

    log.info(f"Loaded {loaded} sales, skipped {skipped}")
    return loaded


def _int(v):
    try:
        return int(float(v)) if v and str(v).strip() else None
    except (ValueError, TypeError):
        return None


def _float(v):
    try:
        return float(v) if v and str(v).strip() else None
    except (ValueError, TypeError):
        return None


def _parse_date(v):
    if not v:
        return None
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y", "%Y%m%d"):
        try:
            return datetime.strptime(v.strip(), fmt).date().isoformat()
        except ValueError:
            continue
    return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="Load county recorder CSV into home_pricing DB")
    parser.add_argument("--file", required=True, help="Path to CSV file")
    parser.add_argument("--source", default="county_recorder", help="Source label")
    args = parser.parse_args()
    load_csv(args.file, args.source)
