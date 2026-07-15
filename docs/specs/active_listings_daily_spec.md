# Spec: `active_listings_daily` — Daily Active-Listing Snapshots

Status: **DRAFT — for review, not yet implemented**
Owner: home_pricing project
Related tables: `properties`, `listings`, `market_metrics`, `property_flags`

## 1. Why this table

The schema already has a `listings` table with an `active` status, but **nothing populates it today**. The only per-property ingest jobs are:

- `data/ingest/redfin_listings.py` — pulls **sold** comps only, into `properties` + `sales`
- `data/ingest/realtor_com.py` — pulls **aggregated** zip/county metrics only, into `market_metrics`

There is no job that fetches individual currently-for-sale homes. As a result, `scheduler/flag_engine.py`'s `stale_listing`, `price_reduction`, and `new_listing` logic queries `listings WHERE status='active'` against an empty table — those flags never fire.

`active_listings_daily` closes that gap: a new, append-only table that stores one row per active listing per day, for every property in the 8 target cities. It becomes the raw feed that:
- upserts the existing `listings` table (current state per listing)
- drives `property_flags` (new listing, price cut, stale, back-on-market, delisted)
- is available directly for day-by-day price/DOM trend queries that `listings` alone (single row, overwritten via `updated_at`) can't answer

## 2. Scope — zip codes

No new zip config needed. This reuses the existing `target_cities` table / `TARGET_ZIPS` set already defined in `data/ingest/realtor_com.py` and `TARGET_CITIES` in `data/ingest/redfin_listings.py`:

Berkeley (94702–94710), Orinda (94563), Moraga (94556), Lafayette (94549), Walnut Creek (94595–94598), Alamo (94507), Danville (94506, 94526), San Ramon (94582–94583).

## 3. Table DDL

```sql
-- ─── ACTIVE LISTINGS DAILY ─────────────────────────────────────────────────────
-- One row per active listing per source per calendar day. Append-only.
CREATE TABLE IF NOT EXISTS active_listings_daily (
    id                  BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    property_id         INT UNSIGNED NOT NULL,
    listing_id          INT UNSIGNED,               -- rolls up into listings.id (current-state row)
    snapshot_date       DATE NOT NULL,

    -- source identity
    source              ENUM('redfin','realtor','zillow','mls','manual') NOT NULL DEFAULT 'redfin',
    source_id           VARCHAR(64) NOT NULL,        -- external listing ID, stable across days
    source_url          VARCHAR(512),
    mls_number          VARCHAR(32),

    -- denormalized location (query convenience — same pattern as market_metrics)
    city                VARCHAR(64) NOT NULL,
    zip_code            CHAR(5) NOT NULL,

    -- market state as of this snapshot
    status              ENUM('active','pending','contingent','coming_soon') NOT NULL DEFAULT 'active',
    property_type       VARCHAR(32),                 -- SFR, condo, townhouse, etc.
    list_price           INT UNSIGNED NOT NULL,
    price_per_sqft        DECIMAL(8,2),
    days_on_market        SMALLINT UNSIGNED,

    -- day-over-day deltas (computed at ingest time vs prior snapshot for same source_id)
    price_change_amt      INT,                        -- negative = reduction
    price_change_pct      DECIMAL(6,3),
    is_new_today          BOOLEAN DEFAULT FALSE,
    is_price_drop_today   BOOLEAN DEFAULT FALSE,

    -- enrichment (nullable — populated only if source provides it)
    photo_count            SMALLINT UNSIGNED,
    photo_url_primary       VARCHAR(512),
    virtual_tour_url         VARCHAR(512),
    listing_agent_name        VARCHAR(128),
    listing_brokerage          VARCHAR(128),
    open_house_next             DATETIME,
    hoa_monthly_listed           SMALLINT UNSIGNED,   -- as-advertised; may differ from properties.hoa_monthly

    raw_payload              JSON,                    -- full source row, for audit/debugging
    fetched_at               TIMESTAMP DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (property_id) REFERENCES properties(id) ON DELETE CASCADE,
    FOREIGN KEY (listing_id) REFERENCES listings(id) ON DELETE SET NULL,
    UNIQUE KEY uq_daily_snapshot (source, source_id, snapshot_date),
    INDEX idx_property_date (property_id, snapshot_date),
    INDEX idx_snapshot_date (snapshot_date),
    INDEX idx_zip_date (zip_code, snapshot_date),
    INDEX idx_city_date (city, snapshot_date),
    INDEX idx_status (status)
) ENGINE=InnoDB;
```

### Design notes

- **Not duplicating static physical attributes.** Beds/baths/sqft/lot/year built/pool/etc. already live in `properties` and rarely change; this table only stores what's specific to *this listing on this day* (price, status, DOM, deltas) plus a few listing-only enrichment fields properties doesn't have (MLS#, agent, photos, virtual tour). Join to `properties` via `property_id` for physical attributes.
- **`listing_id` is nullable and back-filled.** On first sight of a `source_id`, we create/find the row in `listings` (matching its existing `uq_source_listing (source, source_id)` key) and link it here. This keeps `listings` as the single current-state row per listing, while `active_listings_daily` is the full history.
- **Grain is (source, source_id, snapshot_date)**, not (property_id, snapshot_date) — a property with duplicate listings across sources (e.g. re-listed under a new MLS#) stays distinguishable, matching how `listings` already keys on `(source, source_id)`.
- **Retention: indefinite**, per your call. At 8 cities' worth of active inventory (roughly low hundreds of concurrent active listings) this is a few hundred rows/day — trivial volume for MySQL even over years. Revisit partitioning only if row count grows unexpectedly (e.g. if scope expands beyond these 8 cities).

## 4. Data source & fetch routine

### 4.1 Source

Redfin's public GIS-CSV endpoint — the same one `redfin_listings.py` already uses for sold comps (`https://www.redfin.com/stingray/api/gis-csv`), switching the `status` param from `9` (sold) to the active-inventory value.

> **Verify at implementation time:** Redfin's `status` parameter isn't documented publicly; `redfin_listings.py` empirically uses `9` for sold. The commonly-observed value for active/for-sale is `1`, but this must be confirmed against a live response (check the `STATUS` column in the returned CSV) before relying on it — don't hardcode blind.

Reuse from `redfin_listings.py` as-is:
- `_discover_region_id()` — city → Redfin region ID
- `HEADERS`, `AUTOCOMPLETE_URL` / `DOWNLOAD_URL`
- Per-city pagination loop + `time.sleep()` politeness pattern

### 4.2 New module: `data/ingest/active_listings.py`

```
def _download_active(region_id, region_type="2", page=1) -> pd.DataFrame
    # same as _download_sold() but status=<active value>, no sold_within_days param

def _upsert_property(row) -> int
    # reuse redfin_listings._upsert_property (same CSV shape)

def _upsert_listing_current_state(property_id, row, snapshot_date) -> int
    # INSERT ... ON DUPLICATE KEY UPDATE into `listings`, keyed on (source, source_id)
    # bumps price_reductions / last_price_cut_date / last_price_cut_amt when
    # list_price has dropped vs the prior stored value
    # returns listings.id

def _insert_daily_snapshot(property_id, listing_id, row, snapshot_date, prior_price)
    # INSERT INTO active_listings_daily (idempotent via uq_daily_snapshot —
    # safe to re-run same day)
    # price_change_amt/pct computed against prior_price (looked up from
    # yesterday's snapshot for the same source_id, or NULL if none)
    # is_new_today = True if no prior snapshot exists for this source_id

def detect_delistings(snapshot_date)
    # property_ids with an 'active' row in active_listings_daily yesterday
    # but no row today => listing left the active feed.
    # Mark the corresponding `listings` row status='pending' or 'sold'/'withdrawn'
    # (best-effort: without a second source we can't distinguish sold vs
    # withdrawn from absence alone — default to 'pending' and let a later
    # Redfin sold-comp match promote it to 'sold' via redfin_listings.py;
    # otherwise it ages into 'withdrawn' after N days absent — TBD threshold)

def ingest_city(city_state) -> int
    # discover region, paginate _download_active, for each row:
    #   property_id = _upsert_property(row)
    #   listing_id  = _upsert_listing_current_state(...)
    #   _insert_daily_snapshot(...)

def run_all(snapshot_date=today)
    # loop TARGET_CITIES, then detect_delistings(snapshot_date)
```

Field mapping from Redfin CSV (same columns already used in `redfin_listings.py`, plus a few for this table):

| Redfin CSV column | Target column |
|---|---|
| `ADDRESS`, `CITY`, `ZIP OR POSTAL CODE` | `properties` (existing upsert) |
| `PRICE` | `list_price` |
| `$/SQUARE FEET` | `price_per_sqft` |
| `DAYS ON MARKET` | `days_on_market` |
| `STATUS` | `status` (mapped to enum) |
| `PROPERTY TYPE` | `property_type` |
| `URL` | `source_url` |
| `MLS#` | `mls_number` (present in most Redfin CSV exports) |
| — (not in Redfin CSV) | `photo_count`, `listing_agent_name`, `listing_brokerage`, `virtual_tour_url`, `open_house_next` left `NULL` until/unless a richer source (RapidAPI) is added later |

### 4.3 Idempotency & error handling

Matches existing conventions in the codebase:
- `INSERT ... ON DUPLICATE KEY UPDATE` / `INSERT IGNORE` throughout — safe to re-run the same day
- Per-city `try/except` so one city's failure doesn't abort the run (same pattern as `daily_refresh.py`'s per-step try/except)
- `time.sleep(2-3)` between requests, matching `redfin_listings.py`

## 5. Orchestration

Folds into the existing `scheduler/daily_refresh.py` orchestrator — no new launchd plist needed, reuses `com.homepricing.refresh.plist`'s daily cadence.

Insert as a new step **before** the flag engine, since flags depend on this data existing:

```python
# ── new step: Active listings (daily snapshot) ──────────────────────────
try:
    from data.ingest.active_listings import run_all as active_listings_run
    active_listings_run()
    log.info("Active listings: OK")
except Exception as e:
    log.error(f"Active listings failed: {e}")

# ── existing: Generate property flags ────────────────────────────────────
try:
    from scheduler.flag_engine import run as flags_run
    flags_run()
    ...
```

## 6. Downstream: `property_flags` extensions

`flag_engine.py` already has `flag_stale_listings()`, `flag_price_reductions()`, `flag_new_listings()` wired to read from `listings` — once `listings` is actually populated (step above), these start working with **no changes**. Two flag types defined in the `property_flags` enum are still unimplemented and become buildable once this table exists:
- `back_on_market` — a `source_id` reappears in `active_listings_daily` after a gap (previously delisted)
- `hot_market_alert` — needs `market_metrics.absorption_rate`, already partially fed by `realtor_com.py`

These are out of scope for this spec but noted as natural next steps.

## 7. Explicitly out of scope for this spec

- RapidAPI or MLS integration for photo/agent/open-house enrichment (fields left nullable, ready for a future source)
- Reliable sold-vs-withdrawn disambiguation on delisting (needs a second corroborating source or a time-based heuristic — flagged as TBD in §4.2)
- Table partitioning/archival (not warranted at current volume; revisit if scope expands beyond 8 cities)
