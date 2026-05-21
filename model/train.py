"""
Training orchestrator — run this to (re)train both models.
Usage: python -m model.train
"""

import logging
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def main():
    from model.train_data import build
    from model import hedonic, ml_model

    log.info("=== Building training dataset ===")
    df = build()

    log.info("=== Training hedonic model ===")
    hedonic_metrics = hedonic.train(df)
    log.info(f"Hedonic: {hedonic_metrics}")

    log.info("=== Training ML ensemble ===")
    ml_metrics = ml_model.train(df)
    log.info(f"ML: {ml_metrics}")

    log.info("=== Training complete ===")
    return hedonic_metrics, ml_metrics


if __name__ == "__main__":
    main()
