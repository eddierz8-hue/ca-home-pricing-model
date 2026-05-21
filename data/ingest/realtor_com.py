"""
Realtor.com Research data ingestor.
Downloads county + zip level market CSVs — free, no API key.
Adds: median_listing_price_per_sqft, price cut counts, average listing price.
Source: https://www.realtor.com/research/data/
"""

import io
import sys
import pathlib
import logging
from datetime import date

import pandas as pd
import requests

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from db import executemany

log = logging.getLogger(__name__)

TARGET_ZIPS = {
    "94702","94703","94704","94705","94706","94707","94708","94709","94710",
    "94563","94556","94549","94595","94596","94597","94598",
    "94507","94506","94526","94582","94583",
}

# FIPS for Contra Costa (06013) and Alameda (06001) counties
TARGET_FIPS = {"06013", "06001"}

URLS = {
    "zip":    "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/RDC_Inventory_Core_Metrics_Zip_History.csv",
    "county": "https://econdata.s3-us-west-2.amazonaws.com/Reports/Core/RDC_Inventory_Core_Metrics_County_History.csv",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (research data download)"}


def _fetch(url: str) -> pd.DataFrame:
    log.info(f"Fetching {url}")
    resp = requests.get(url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text), low_memory=False)


def _safe_int(v):
    try:
        f = float(v)
        return int(f) if not pd.isna(f) else None
    except (TypeError, ValueError):
        return None


def _safe_float(v):
    try:
        f = float(v)
        return f if not pd.isna(f) else None
    except (TypeError, ValueError):
        return None


def ingest_zip(cutoff_years: int = 3):
    df = _fetch(URLS["zip"])
    df.columns = [c.lower().strip() for c in df.columns]

    cutoff = pd.Timestamp(date.today().replace(year=date.today().year - cutoff_years))
    df["month_date_yyyymm"] = pd.to_datetime(df["month_date_yyyymm"].astype(str), format="%Y%m", errors="coerce")
    df = df[df["month_date_yyyymm"] >= cutoff]

    zip_col = next((c for c in df.columns if "postal" in c or "zip" in c), None)
    if not zip_col:
        log.warning("No zip column found in Realtor.com zip data")
        return 0

    df[zip_col] = df[zip_col].astype(str).str.zfill(5)
    df = df[df[zip_col].isin(TARGET_ZIPS)]

    rows = []
    for _, row in df.iterrows():
        rows.append((
            row["month_date_yyyymm"].date().isoformat(),
            None,
            str(row[zip_col]),
            "zip",
            _safe_int(row.get("active_listing_count")),
            _safe_int(row.get("median_listing_price")),
            None,
            _safe_int(row.get("median_days_on_market")),
            None,
            None,
            None,
            _safe_int(row.get("new_listing_count")),
            None,
            "realtor_com",
        ))

    sql = """
        INSERT INTO market_metrics
            (metric_date, city, zip_code, granularity,
             active_listings, median_list_price, median_sale_price, median_dom,
             absorption_rate, list_to_sale_ratio, price_cut_pct,
             new_listings, sold_count, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            active_listings   = COALESCE(VALUES(active_listings),   active_listings),
            median_list_price = COALESCE(VALUES(median_list_price), median_list_price),
            median_dom        = COALESCE(VALUES(median_dom),        median_dom),
            new_listings      = COALESCE(VALUES(new_listings),      new_listings)
    """
    n = executemany(sql, rows)
    log.info(f"Realtor.com zip: upserted {n} rows")
    return n


def ingest_county(cutoff_years: int = 3):
    df = _fetch(URLS["county"])
    df.columns = [c.lower().strip() for c in df.columns]

    cutoff = pd.Timestamp(date.today().replace(year=date.today().year - cutoff_years))
    df["month_date_yyyymm"] = pd.to_datetime(df["month_date_yyyymm"].astype(str), format="%Y%m", errors="coerce")
    df = df[df["month_date_yyyymm"] >= cutoff]

    fips_col = next((c for c in df.columns if "fips" in c or "county_fips" in c), None)
    if fips_col:
        df[fips_col] = df[fips_col].astype(str).str.zfill(5)
        df = df[df[fips_col].isin(TARGET_FIPS)]
    else:
        county_col = next((c for c in df.columns if "county" in c and "name" in c), None)
        if county_col:
            df = df[df[county_col].str.contains("Contra Costa|Alameda", na=False)]

    rows = []
    for _, row in df.iterrows():
        city = str(row.get("county_name", "")).replace(" County", "").strip() or None
        rows.append((
            row["month_date_yyyymm"].date().isoformat(),
            city,
            None,
            "city",
            _safe_int(row.get("active_listing_count")),
            _safe_int(row.get("median_listing_price")),
            None,
            _safe_int(row.get("median_days_on_market")),
            None,
            None,
            None,
            _safe_int(row.get("new_listing_count")),
            None,
            "realtor_com",
        ))

    sql = """
        INSERT INTO market_metrics
            (metric_date, city, zip_code, granularity,
             active_listings, median_list_price, median_sale_price, median_dom,
             absorption_rate, list_to_sale_ratio, price_cut_pct,
             new_listings, sold_count, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            active_listings   = COALESCE(VALUES(active_listings),   active_listings),
            median_list_price = COALESCE(VALUES(median_list_price), median_list_price),
            median_dom        = COALESCE(VALUES(median_dom),        median_dom),
            new_listings      = COALESCE(VALUES(new_listings),      new_listings)
    """
    n = executemany(sql, rows)
    log.info(f"Realtor.com county: upserted {n} rows")
    return n


def run_all(cutoff_years: int = 3):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest_zip(cutoff_years)
    ingest_county(cutoff_years)
    log.info("Realtor.com ingest complete")


if __name__ == "__main__":
    run_all()
