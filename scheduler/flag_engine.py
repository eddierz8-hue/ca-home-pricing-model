"""
Flag engine — generates property_flags based on daily market conditions.
Runs after ingest. Flags stale listings, price reductions, outliers vs model estimate.
"""

import sys
import pathlib
import logging
from datetime import date

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))
from db import execute, executemany

log = logging.getLogger(__name__)


def _city_median_dom() -> dict:
    rows = execute("""
        SELECT city, AVG(median_dom) AS avg_dom
        FROM market_metrics
        WHERE metric_date >= DATE_SUB(CURDATE(), INTERVAL 90 DAY)
          AND median_dom IS NOT NULL
        GROUP BY city
    """, fetch=True)
    return {r["city"]: float(r["avg_dom"]) for r in rows}


def flag_stale_listings():
    medians = _city_median_dom()
    if not medians:
        log.warning("No median DOM data — skipping stale listing flags")
        return

    rows = execute("""
        SELECT l.id AS listing_id, l.property_id, p.city, l.days_on_market
        FROM listings l
        JOIN properties p ON p.id = l.property_id
        WHERE l.status = 'active'
          AND l.days_on_market IS NOT NULL
    """, fetch=True)

    flags = []
    today = date.today().isoformat()
    for row in rows:
        threshold = medians.get(row["city"], 30) * 1.5
        if row["days_on_market"] > threshold:
            flags.append((
                row["property_id"],
                row["listing_id"],
                today,
                "stale_listing",
                f"DOM {row['days_on_market']} > 1.5x city median ({threshold:.0f} days)",
            ))

    if flags:
        sql = """
            INSERT IGNORE INTO property_flags
                (property_id, listing_id, flag_date, flag_type, flag_detail)
            VALUES (%s, %s, %s, %s, %s)
        """
        n = executemany(sql, flags)
        log.info(f"Stale listing flags: {n}")


def flag_price_reductions():
    rows = execute("""
        SELECT l.id AS listing_id, l.property_id,
               l.price_reductions, l.last_price_cut_date, l.last_price_cut_amt
        FROM listings l
        WHERE l.status = 'active'
          AND l.last_price_cut_date = CURDATE()
    """, fetch=True)

    flags = []
    today = date.today().isoformat()
    for row in rows:
        flag_type = "multiple_reductions" if (row["price_reductions"] or 0) >= 2 else "price_reduction"
        flags.append((
            row["property_id"],
            row["listing_id"],
            today,
            flag_type,
            f"Cut #{row['price_reductions']}: ${row['last_price_cut_amt']:,} on {row['last_price_cut_date']}",
        ))

    if flags:
        sql = """
            INSERT IGNORE INTO property_flags
                (property_id, listing_id, flag_date, flag_type, flag_detail)
            VALUES (%s, %s, %s, %s, %s)
        """
        n = executemany(sql, flags)
        log.info(f"Price reduction flags: {n}")


def flag_new_listings():
    rows = execute("""
        SELECT l.id AS listing_id, l.property_id, p.address, p.city
        FROM listings l
        JOIN properties p ON p.id = l.property_id
        WHERE l.list_date = CURDATE()
    """, fetch=True)

    flags = [
        (r["property_id"], r["listing_id"], date.today().isoformat(),
         "new_listing", f"New listing: {r['address']}, {r['city']}")
        for r in rows
    ]

    if flags:
        sql = """
            INSERT IGNORE INTO property_flags
                (property_id, listing_id, flag_date, flag_type, flag_detail)
            VALUES (%s, %s, %s, %s, %s)
        """
        n = executemany(sql, flags)
        log.info(f"New listing flags: {n}")


def resolve_old_flags():
    execute("""
        UPDATE property_flags pf
        JOIN listings l ON l.id = pf.listing_id
        SET pf.is_active = FALSE, pf.resolved_at = CURDATE()
        WHERE pf.is_active = TRUE
          AND l.status IN ('sold', 'withdrawn', 'expired')
    """)


def run():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    log.info("Flag engine running")
    resolve_old_flags()
    flag_stale_listings()
    flag_price_reductions()
    flag_new_listings()
    log.info("Flag engine complete")


if __name__ == "__main__":
    run()
