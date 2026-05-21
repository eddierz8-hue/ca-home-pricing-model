"""
Google Trends ingestor — local real estate search demand proxy.
Uses pytrends (unofficial Google Trends API wrapper).
Pulls weekly search interest for "homes for sale [city] CA" per target city.
"""

import sys
import pathlib
import logging
import time
from datetime import date, timedelta

from pytrends.request import TrendReq

sys.path.insert(0, str(pathlib.Path(__file__).parents[2]))
from db import executemany

log = logging.getLogger(__name__)

TARGET_CITIES = [
    "Berkeley", "Orinda", "Moraga", "Lafayette",
    "Walnut Creek", "Alamo", "Danville", "San Ramon",
]

GEO = "US-CA"  # California state-level geo filter


def ingest(cutoff_years: int = 3):
    pytrends = TrendReq(hl="en-US", tz=-480, timeout=(10, 25))

    start_date = (date.today() - timedelta(days=cutoff_years * 365)).strftime("%Y-%m-%d")
    end_date   = date.today().strftime("%Y-%m-%d")
    timeframe  = f"{start_date} {end_date}"

    keywords = [f"homes for sale {c} CA" for c in TARGET_CITIES]
    rows = []

    # Trends API allows max 5 keywords per request; retry up to 3x on 429
    for i in range(0, len(keywords), 5):
        batch        = keywords[i:i + 5]
        cities_batch = TARGET_CITIES[i:i + 5]
        for attempt in range(3):
            try:
                pytrends.build_payload(batch, geo=GEO, timeframe=timeframe)
                df = pytrends.interest_over_time()
                if df.empty:
                    log.warning(f"Empty response for batch: {batch}")
                    break
                df = df.drop(columns=["isPartial"], errors="ignore")
                for kw, city in zip(batch, cities_batch):
                    if kw not in df.columns:
                        continue
                    for idx, val in df[kw].items():
                        rows.append((
                            idx.date().isoformat(),
                            city,
                            None,
                            "city",
                            "google_trends",
                            "search_volume_homes_for_sale",
                            float(val),
                        ))
                break  # success
            except Exception as e:
                if "429" in str(e) and attempt < 2:
                    wait = 30 * (attempt + 1)
                    log.warning(f"Rate limited, waiting {wait}s (attempt {attempt+1}/3)")
                    time.sleep(wait)
                else:
                    log.warning(f"Trends batch failed {batch}: {e}")
                    break

        time.sleep(5)  # polite delay between batches

    if not rows:
        log.warning("No Google Trends data collected")
        return 0

    sql = """
        INSERT INTO consumer_sentiment
            (metric_date, city, zip_code, granularity, source, metric_name, value)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        ON DUPLICATE KEY UPDATE value = VALUES(value)
    """
    n = executemany(sql, rows)
    log.info(f"Google Trends: upserted {n} rows")
    return n


def run_all(cutoff_years: int = 3):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    ingest(cutoff_years)
    log.info("Google Trends ingest complete")


if __name__ == "__main__":
    run_all()
