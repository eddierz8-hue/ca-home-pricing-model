"""
Daily refresh — orchestrates all data ingest and flag generation.
Run via cron or launchd. Logs to scheduler/refresh.log.
"""

import logging
import pathlib
import sys
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

log_path = pathlib.Path(__file__).parent / "refresh.log"
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(log_path),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger(__name__)


def run():
    log.info(f"=== Daily refresh started {date.today()} ===")

    # ── 1. Zillow Research (ZHVI + market metrics) ────────────────────────────
    try:
        from data.ingest.zillow_research import run_all as zillow_run
        zillow_run()
        log.info("Zillow Research: OK")
    except Exception as e:
        log.error(f"Zillow Research failed: {e}")

    # ── 2. FRED economic indicators ───────────────────────────────────────────
    try:
        from data.ingest.fred import run_all as fred_run
        fred_run()
        log.info("FRED: OK")
    except Exception as e:
        log.error(f"FRED failed: {e}")

    # ── 3. Redfin market data ─────────────────────────────────────────────────
    try:
        from data.ingest.redfin import run_all as redfin_run
        redfin_run()
        log.info("Redfin: OK")
    except Exception as e:
        log.error(f"Redfin failed: {e}")

    # ── 4. Google Trends sentiment ────────────────────────────────────────────
    try:
        from data.ingest.google_trends import run_all as trends_run
        trends_run()
        log.info("Google Trends: OK")
    except Exception as e:
        log.error(f"Google Trends failed: {e}")

    # ── 5. Realtor.com market data ────────────────────────────────────────────
    try:
        from data.ingest.realtor_com import run_all as realtor_run
        realtor_run()
        log.info("Realtor.com: OK")
    except Exception as e:
        log.error(f"Realtor.com failed: {e}")

    # ── 6. Generate property flags ────────────────────────────────────────────
    try:
        from scheduler.flag_engine import run as flags_run
        flags_run()
        log.info("Flag engine: OK")
    except Exception as e:
        log.error(f"Flag engine failed: {e}")

    # ── 6. Retrain models on fresh data ───────────────────────────────────────
    try:
        from model.train import main as train_run
        hedonic_m, ml_m = train_run()
        log.info(f"Model retrain: hedonic R²={hedonic_m['cv_r2_mean']:.3f}, "
                 f"ML R²={ml_m['xgb_r2']:.3f}")
    except Exception as e:
        log.error(f"Model retrain failed: {e}")

    log.info(f"=== Daily refresh complete {date.today()} ===")


if __name__ == "__main__":
    run()
