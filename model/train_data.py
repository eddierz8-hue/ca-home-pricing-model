"""
Training data builder.
Joins ZHVI, market_metrics, economic_indicators, and consumer_sentiment
into a flat feature matrix. Each row = one zip-code/month observation.
Target = median_sale_price (from market_metrics) or ZHVI (fallback).
"""

import sys
import pathlib
import logging

import pandas as pd
import numpy as np

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from db import execute

log = logging.getLogger(__name__)

# Economic series we pull from FRED
FRED_SERIES = {
    "MORTGAGE30US": "mortgage_rate_30y",
    "MORTGAGE15US": "mortgage_rate_15y",
    "UMCSENT":      "consumer_sentiment",
    "SFXRSA":       "sf_hpi",
    "FEDFUNDS":     "fed_funds_rate",
    "CPIAUCSL":     "cpi",
    "CAUR":         "ca_unemployment",
}


def _load_zhvi() -> pd.DataFrame:
    rows = execute("""
        SELECT zip_code, metric_date, home_value
        FROM zhvi
        WHERE source = 'zhvi_sfr_upper'
        ORDER BY zip_code, metric_date
    """, fetch=True)
    df = pd.DataFrame(rows)
    df["metric_date"] = pd.to_datetime(df["metric_date"])
    df["home_value"] = pd.to_numeric(df["home_value"])
    return df


def _load_market_metrics() -> pd.DataFrame:
    rows = execute("""
        SELECT zip_code, city, metric_date,
               median_list_price, median_sale_price, median_dom,
               active_listings, list_to_sale_ratio, price_cut_pct,
               new_listings, sold_count, absorption_rate
        FROM market_metrics
        WHERE granularity = 'zip'
          AND zip_code IS NOT NULL
        ORDER BY zip_code, metric_date
    """, fetch=True)
    df = pd.DataFrame(rows)
    df["metric_date"] = pd.to_datetime(df["metric_date"])
    for col in ["median_list_price", "median_sale_price", "median_dom",
                "active_listings", "list_to_sale_ratio", "price_cut_pct",
                "new_listings", "sold_count", "absorption_rate"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def _load_fred() -> pd.DataFrame:
    series_list = "', '".join(FRED_SERIES.keys())
    rows = execute(f"""
        SELECT series_id, indicator_date, value
        FROM economic_indicators
        WHERE series_id IN ('{series_list}')
        ORDER BY series_id, indicator_date
    """, fetch=True)
    df = pd.DataFrame(rows)
    df["indicator_date"] = pd.to_datetime(df["indicator_date"])
    df["value"] = pd.to_numeric(df["value"])
    # Pivot to wide format, resample to monthly
    df = df.rename(columns={"indicator_date": "metric_date"})
    df["series_name"] = df["series_id"].map(FRED_SERIES)
    wide = df.pivot_table(index="metric_date", columns="series_name", values="value", aggfunc="last")
    wide = wide.resample("MS").last().ffill()
    wide = wide.reset_index()
    return wide


def _load_sentiment() -> pd.DataFrame:
    rows = execute("""
        SELECT city, metric_date, AVG(value) AS search_volume
        FROM consumer_sentiment
        WHERE source = 'google_trends'
          AND metric_name = 'search_volume_homes_for_sale'
        GROUP BY city, metric_date
        ORDER BY city, metric_date
    """, fetch=True)
    df = pd.DataFrame(rows)
    df["metric_date"] = pd.to_datetime(df["metric_date"])
    df["search_volume"] = pd.to_numeric(df["search_volume"])
    # 4-week rolling average to smooth
    df = df.sort_values(["city", "metric_date"])
    df["search_volume_ma4"] = df.groupby("city")["search_volume"].transform(
        lambda x: x.rolling(4, min_periods=1).mean()
    )
    return df[["city", "metric_date", "search_volume_ma4"]]


def build() -> pd.DataFrame:
    log.info("Loading ZHVI...")
    zhvi = _load_zhvi()

    log.info("Loading market metrics...")
    mkt = _load_market_metrics()

    log.info("Loading FRED indicators...")
    fred = _load_fred()

    log.info("Loading consumer sentiment...")
    sent = _load_sentiment()

    # Resample ZHVI to monthly (it's already monthly)
    # Merge market metrics onto ZHVI (both zip + monthly)
    # Use period-start alignment (floor to month)
    zhvi["month"] = zhvi["metric_date"].dt.to_period("M").dt.to_timestamp()
    mkt["month"]  = mkt["metric_date"].dt.to_period("M").dt.to_timestamp()
    fred["month"] = fred["metric_date"].dt.to_period("M").dt.to_timestamp()

    # Aggregate market metrics to monthly per zip (take mean if multiple sources)
    mkt_monthly = mkt.groupby(["zip_code", "month"]).agg({
        "city":                 "first",
        "median_list_price":    "mean",
        "median_sale_price":    "mean",
        "median_dom":           "mean",
        "active_listings":      "mean",
        "list_to_sale_ratio":   "mean",
        "price_cut_pct":        "mean",
        "new_listings":         "mean",
        "sold_count":           "mean",
        "absorption_rate":      "mean",
    }).reset_index()

    # Merge ZHVI + market
    df = zhvi[["zip_code", "month", "home_value"]].merge(
        mkt_monthly, on=["zip_code", "month"], how="left"
    )

    # Merge FRED (broadcast across all zips)
    df = df.merge(fred, on="month", how="left")

    # Merge sentiment on city
    sent["month"] = sent["metric_date"].dt.to_period("M").dt.to_timestamp()
    df = df.merge(sent[["city", "month", "search_volume_ma4"]], on=["city", "month"], how="left")

    # Feature engineering
    df = df.sort_values(["zip_code", "month"])
    grp = df.groupby("zip_code")

    df["zhvi_lag1"]  = grp["home_value"].shift(1)
    df["zhvi_lag3"]  = grp["home_value"].shift(3)
    df["zhvi_lag12"] = grp["home_value"].shift(12)
    df["zhvi_mom"]   = (df["home_value"] - df["zhvi_lag1"]) / df["zhvi_lag1"]
    df["zhvi_yoy"]   = (df["home_value"] - df["zhvi_lag12"]) / df["zhvi_lag12"]

    df["month_num"]  = df["month"].dt.month
    df["year"]       = df["month"].dt.year

    # Target: next month ZHVI (for forward-looking prediction)
    df["target_zhvi"] = grp["home_value"].shift(-1)

    # Also use median_sale_price where available as target
    df["target_price"] = df["median_sale_price"].combine_first(df["target_zhvi"])

    df = df.dropna(subset=["home_value", "zhvi_lag1"])
    log.info(f"Training dataset: {len(df)} rows, {df['zip_code'].nunique()} zips, "
             f"{df['month'].min().date()} → {df['month'].max().date()}")
    return df


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    df = build()
    print(df[["zip_code", "month", "home_value", "mortgage_rate_30y", "consumer_sentiment"]].tail(10))
    print(f"\nShape: {df.shape}")
    print(f"\nNull counts:\n{df.isnull().sum()[df.isnull().sum() > 0]}")
