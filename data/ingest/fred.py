"""
FRED economic indicators ingestor.
Pulls national + Bay Area series relevant to residential pricing.
Requires FRED_API_KEY in .env — free at https://fred.stlouisfed.org/docs/api/api_key.html
"""

import sys
import pathlib
import logging
from datetime import date

import pandas as pd
from fredapi import Fred
from dotenv import load_dotenv
import os

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from db import executemany

load_dotenv()
log = logging.getLogger(__name__)

# FRED series to pull — (series_id, human name, unit, frequency)
SERIES = [
    # Mortgage rates
    ("MORTGAGE30US",  "30-Year Fixed Mortgage Rate",          "percent",  "weekly"),
    ("MORTGAGE15US",  "15-Year Fixed Mortgage Rate",          "percent",  "weekly"),
    # Housing supply / demand
    ("HOUST",         "Housing Starts (national)",             "thousands","monthly"),
    ("MSACSR",        "Monthly Supply of New Houses",          "months",   "monthly"),
    ("MSPUS",         "Median Sales Price US Homes",           "dollars",  "quarterly"),
    # Bay Area / California specific
    ("CASTHPI",       "California All-Transactions HPI",       "index",    "quarterly"),
    ("SFXRSA",        "S&P Case-Shiller San Francisco HPI",    "index",    "monthly"),
    ("CAUR",          "California Unemployment Rate",          "percent",  "monthly"),
    ("SMU06197808000000001SA", "Bay Area Total Nonfarm Employment", "thousands", "monthly"),
    # Consumer / macro
    ("UMCSENT",       "U Michigan Consumer Sentiment",         "index",    "monthly"),
    ("CPIAUCSL",      "CPI All Urban Consumers",               "index",    "monthly"),
    ("FEDFUNDS",      "Federal Funds Rate",                    "percent",  "monthly"),
    ("T10YIE",        "10-Year Breakeven Inflation Rate",      "percent",  "daily"),
]


def ingest_series(series_id: str, name: str, unit: str, frequency: str,
                  fred: Fred, cutoff_years: int = 3):
    cutoff = f"{date.today().year - cutoff_years}-01-01"
    try:
        s = fred.get_series(series_id, observation_start=cutoff)
        s = s.dropna()
    except Exception as e:
        log.warning(f"Could not fetch {series_id}: {e}")
        return 0

    rows = [
        (d.date().isoformat(), series_id, name, float(v), unit, frequency, "FRED")
        for d, v in s.items()
    ]

    sql = """
        INSERT INTO economic_indicators
            (indicator_date, series_id, series_name, value, unit, frequency, source)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE value = VALUES(value)
    """
    n = executemany(sql, rows)
    log.info(f"FRED {series_id}: {n} rows")
    return n


def run_all(cutoff_years: int = 3):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        log.error("FRED_API_KEY not set in .env — skipping FRED ingest")
        return
    fred = Fred(api_key=api_key)
    for series_id, name, unit, freq in SERIES:
        ingest_series(series_id, name, unit, freq, fred, cutoff_years)
    log.info("FRED ingest complete")


if __name__ == "__main__":
    run_all()
