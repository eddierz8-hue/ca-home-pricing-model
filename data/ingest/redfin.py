"""
Redfin public market data ingestor.
Downloads city/zip level market tracker CSVs from Redfin's data center.
Source: https://www.redfin.com/news/data-center/ — no API key required.
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
    "94702", "94703", "94704", "94705", "94706", "94707", "94708", "94709", "94710",
    "94563", "94556", "94549",
    "94595", "94596", "94597", "94598",
    "94507", "94506", "94526",
    "94582", "94583",
}

TARGET_CITIES = {
    "Berkeley", "Orinda", "Moraga", "Lafayette",
    "Walnut Creek", "Alamo", "Danville", "San Ramon",
}

REDFIN_URLS = {
    "zip":  "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/zip_code_market_tracker.tsv000.gz",
    "city": "https://redfin-public-data.s3.us-west-2.amazonaws.com/redfin_market_tracker/city_market_tracker.tsv000.gz",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (research data download)"}


def _fetch(url: str) -> pd.DataFrame:
    log.info(f"Fetching {url}")
    resp = requests.get(url, headers=HEADERS, timeout=120)
    resp.raise_for_status()
    return pd.read_csv(
        io.BytesIO(resp.content), sep="\t", compression="gzip",
        dtype={"region": str, "region_id": str, "zip_code": str},
        low_memory=False,
    )


def _filter_and_clean(df: pd.DataFrame, granularity: str, cutoff_years: int) -> pd.DataFrame:
    cutoff = pd.Timestamp(date.today().replace(year=date.today().year - cutoff_years))
    df["period_begin"] = pd.to_datetime(df["period_begin"], errors="coerce")
    df = df[df["period_begin"] >= cutoff]

    if granularity == "zip":
        if "zip_code" in df.columns:
            df = df[df["zip_code"].isin(TARGET_ZIPS)]
        elif "region" in df.columns:
            df = df[df["region"].isin(TARGET_ZIPS)]
    else:
        df = df[df["region"].isin(TARGET_CITIES)]
        state_col = "state_code" if "state_code" in df.columns else "state"
        if state_col in df.columns:
            df = df[df[state_col] == "CA"]

    return df


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


def ingest(granularity: str = "zip", cutoff_years: int = 3):
    df = _fetch(REDFIN_URLS[granularity])
    df = _filter_and_clean(df, granularity, cutoff_years)

    rows = []
    for _, row in df.iterrows():
        zip_val  = row.get("zip_code") if granularity == "zip" else None
        city_val = row.get("region")   if granularity == "city" else row.get("city")
        rows.append((
            row["period_begin"].date().isoformat(),
            city_val,
            zip_val,
            granularity,
            _safe_int(row.get("inventory")),
            _safe_int(row.get("median_list_price")),
            _safe_int(row.get("median_sale_price")),
            _safe_int(row.get("median_dom")),
            _safe_float(row.get("months_of_supply")),
            _safe_float(row.get("median_sale_to_list")),
            _safe_float(row.get("percent_homes_with_price_drop")),
            _safe_int(row.get("new_listings")),
            _safe_int(row.get("homes_sold")),
            "redfin",
        ))

    sql = """
        INSERT INTO market_metrics
            (metric_date, city, zip_code, granularity,
             active_listings, median_list_price, median_sale_price, median_dom,
             absorption_rate, list_to_sale_ratio, price_cut_pct,
             new_listings, sold_count, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            active_listings    = VALUES(active_listings),
            median_list_price  = VALUES(median_list_price),
            median_sale_price  = VALUES(median_sale_price),
            median_dom         = VALUES(median_dom),
            absorption_rate    = VALUES(absorption_rate),
            list_to_sale_ratio = VALUES(list_to_sale_ratio),
            price_cut_pct      = VALUES(price_cut_pct),
            new_listings       = VALUES(new_listings),
            sold_count         = VALUES(sold_count)
    """
    n = executemany(sql, rows)
    log.info(f"Redfin {granularity}: upserted {n} rows")
    return n


def run_all(cutoff_years: int = 3):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest("zip", cutoff_years)
    ingest("city", cutoff_years)
    log.info("Redfin ingest complete")


if __name__ == "__main__":
    run_all()
