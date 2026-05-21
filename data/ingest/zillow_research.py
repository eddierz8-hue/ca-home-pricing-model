"""
Zillow Research public data downloader.
Downloads ZHVI (home value index) and market metric CSVs at zip-code level.
Source: https://www.zillow.com/research/data/ — no API key required.
"""

import io
import sys
import pathlib
import logging
from datetime import date

import pandas as pd
import requests

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from db import execute, executemany

log = logging.getLogger(__name__)

# Target zip codes for our 8 cities
TARGET_ZIPS = {
    "94702", "94703", "94704", "94705", "94706", "94707", "94708", "94709", "94710",  # Berkeley
    "94563",  # Orinda
    "94556",  # Moraga
    "94549",  # Lafayette
    "94595", "94596", "94597", "94598",  # Walnut Creek
    "94507",  # Alamo
    "94506", "94526",  # Danville
    "94582", "94583",  # San Ramon
}

ZILLOW_FEEDS = {
    "zhvi_sfr":       "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.33_0.67_sm_sa_month.csv",
    "zhvi_sfr_upper": "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_sfr_tier_0.67_1.0_sm_sa_month.csv",
    "zhvi_condo":     "https://files.zillowstatic.com/research/public_csvs/zhvi/Zip_zhvi_uc_condo_tier_0.33_0.67_sm_sa_month.csv",
    "median_list_price":  "https://files.zillowstatic.com/research/public_csvs/mlp/Zip_mlp_uc_sfrcondo_week.csv",
    "median_sale_price":  "https://files.zillowstatic.com/research/public_csvs/msp/Zip_msp_uc_sfrcondo_sm_month.csv",
    "days_on_market":     "https://files.zillowstatic.com/research/public_csvs/dom/Zip_median_dom_uc_sfrcondo_sm_month.csv",
    "list_to_sale_ratio": "https://files.zillowstatic.com/research/public_csvs/lst/Zip_mean_lst_uc_sfrcondo_sm_month.csv",
    "price_cut_pct":      "https://files.zillowstatic.com/research/public_csvs/mpc/Zip_mpc_uc_sfrcondo_sm_month.csv",
}

HEADERS = {"User-Agent": "Mozilla/5.0 (research data download)"}


def _fetch_csv(url: str) -> pd.DataFrame:
    resp = requests.get(url, headers=HEADERS, timeout=60)
    resp.raise_for_status()
    return pd.read_csv(io.StringIO(resp.text), dtype={"RegionName": str})


def _melt_timeseries(df: pd.DataFrame, value_col: str) -> pd.DataFrame:
    id_cols = ["RegionName", "RegionID", "StateName", "State", "City", "Metro", "CountyName"]
    id_cols = [c for c in id_cols if c in df.columns]
    date_cols = [c for c in df.columns if c not in id_cols]
    melted = df.melt(id_vars=id_cols, value_vars=date_cols, var_name="metric_date", value_name=value_col)
    melted["metric_date"] = pd.to_datetime(melted["metric_date"], errors="coerce")
    melted = melted.dropna(subset=["metric_date", value_col])
    melted = melted[melted["RegionName"].isin(TARGET_ZIPS)]
    return melted


def ingest_zhvi(feed_key: str = "zhvi_sfr", cutoff_years: int = 3):
    url = ZILLOW_FEEDS[feed_key]
    log.info(f"Fetching ZHVI from {url}")
    df = _fetch_csv(url)
    series = _melt_timeseries(df, "home_value")
    cutoff = pd.Timestamp(date.today().replace(year=date.today().year - cutoff_years))
    series = series[series["metric_date"] >= cutoff]

    rows = []
    for _, row in series.iterrows():
        rows.append((
            row["RegionName"],
            row.get("City"),
            row["metric_date"].date().isoformat(),
            int(row["home_value"]),
            feed_key,
        ))

    sql = """
        INSERT INTO zhvi (zip_code, city, metric_date, home_value, source)
        VALUES (%s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE home_value = VALUES(home_value)
    """
    n = executemany(sql, rows)
    log.info(f"ZHVI ({feed_key}): upserted {n} rows")
    return n


def ingest_market_metrics(cutoff_years: int = 3):
    cutoff = pd.Timestamp(date.today().replace(year=date.today().year - cutoff_years))
    metric_map = {
        "median_list_price": "median_list_price",
        "median_sale_price": "median_sale_price",
        "days_on_market": "median_dom",
        "list_to_sale_ratio": "list_to_sale_ratio",
        "price_cut_pct": "price_cut_pct",
    }

    # Collect one dataframe per metric then merge on zip+date
    frames = {}
    for feed_key, col_name in metric_map.items():
        try:
            df = _fetch_csv(ZILLOW_FEEDS[feed_key])
            melted = _melt_timeseries(df, col_name)
            melted = melted[melted["metric_date"] >= cutoff]
            frames[col_name] = melted[["RegionName", "City", "metric_date", col_name]].rename(
                columns={"RegionName": "zip_code", "City": "city"}
            )
        except Exception as e:
            log.warning(f"Skipping {feed_key}: {e}")

    if not frames:
        log.warning("No market metric frames fetched")
        return 0

    merged = None
    for col_name, frame in frames.items():
        if merged is None:
            merged = frame
        else:
            merged = merged.merge(frame, on=["zip_code", "city", "metric_date"], how="outer")

    rows = []
    for _, row in merged.iterrows():
        rows.append((
            row["metric_date"].date().isoformat(),
            row.get("city"),
            row["zip_code"],
            "zip",
            int(row["median_list_price"]) if pd.notna(row.get("median_list_price")) else None,
            int(row["median_sale_price"]) if pd.notna(row.get("median_sale_price")) else None,
            int(row["median_dom"]) if pd.notna(row.get("median_dom")) else None,
            float(row["list_to_sale_ratio"]) if pd.notna(row.get("list_to_sale_ratio")) else None,
            float(row["price_cut_pct"]) if pd.notna(row.get("price_cut_pct")) else None,
            "zillow_research",
        ))

    sql = """
        INSERT INTO market_metrics
            (metric_date, city, zip_code, granularity,
             median_list_price, median_sale_price, median_dom,
             list_to_sale_ratio, price_cut_pct, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE
            median_list_price  = VALUES(median_list_price),
            median_sale_price  = VALUES(median_sale_price),
            median_dom         = VALUES(median_dom),
            list_to_sale_ratio = VALUES(list_to_sale_ratio),
            price_cut_pct      = VALUES(price_cut_pct)
    """
    n = executemany(sql, rows)
    log.info(f"Market metrics: upserted {n} rows")
    return n


def run_all():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest_zhvi("zhvi_sfr")
    ingest_zhvi("zhvi_sfr_upper")
    ingest_zhvi("zhvi_condo")
    ingest_market_metrics()
    log.info("Zillow Research ingest complete")


if __name__ == "__main__":
    run_all()
