# Handoff: `active_listings_daily` table

Status: **Spec approved by user — not yet implemented.** Pick up at "Next steps" below.

## Goal

Add daily tracking of active for-sale homes (not just sold comps) for the 8 target
East Bay cities, so the pricing model and flag engine have real active-listing data
to work from.

## What exists already (read this before touching anything)

- Full spec: [`active_listings_daily_spec.md`](./active_listings_daily_spec.md) — table DDL,
  field-by-field rationale, routine pseudocode, orchestration plan. Read it first; this
  handoff is a pointer + status, not a replacement.
- **Confirmed gap**: `db/schema.sql` already defines a `listings` table with an `active`
  status, but no ingest job writes to it. `scheduler/flag_engine.py`'s stale/price-cut/
  new-listing logic silently no-ops today because `listings` is empty.
- **Reusable code**: `data/ingest/redfin_listings.py` already does region discovery +
  paginated CSV download against Redfin's free GIS-CSV endpoint — for **sold** listings
  (`status=9`). The new ingest job reuses this pattern for active listings.

## Decisions already made (don't re-litigate these — ask the user only if you find a reason they're wrong)

| Decision | Answer |
|---|---|
| New table vs. reuse `listings` | New table `active_listings_daily`, append-only daily snapshots. `listings` stays the current-state row, upserted from the snapshot. |
| Data source | Redfin GIS-CSV endpoint (same one already used for sold comps), switching the `status` param to the active-listings value. |
| Retention | Keep every daily snapshot indefinitely — volume is trivial at 8-city scale. |
| Scheduling | Fold into existing `scheduler/daily_refresh.py` orchestrator, no new launchd job. Insert before `flag_engine` runs. |

## Open / unresolved (flagged in the spec, needs attention during implementation)

1. **Redfin's active `status` param value is unconfirmed.** `redfin_listings.py` uses `9` for
   sold empirically; active is *probably* `1` but must be verified against a live CSV
   response (check the `STATUS` column) before hardcoding.
2. **Sold-vs-withdrawn disambiguation on delisting.** When a listing drops out of the daily
   active feed, we can't tell from absence alone whether it sold or was withdrawn. Spec
   proposes a best-effort heuristic (default to `pending`, promote to `sold` if
   `redfin_listings.py` later picks it up as a comp, age to `withdrawn` after N days —
   threshold TBD). Needs a decision before `detect_delistings()` is implemented.
3. **Enrichment fields** (MLS#, listing agent, photos, virtual tour, open house) are
   nullable in the spec because the free Redfin CSV may not reliably include them —
   confirm actual CSV columns during implementation before assuming they're populated.

## Next steps (in order)

1. Add the `active_listings_daily` DDL from the spec to `db/schema.sql`.
2. Build `data/ingest/active_listings.py` per the spec's §4.2 pseudocode, reusing
   `_discover_region_id`, `HEADERS`, `AUTOCOMPLETE_URL`/`DOWNLOAD_URL` from
   `redfin_listings.py`. Confirm the active `status` param empirically (open item #1).
3. Wire it into `scheduler/daily_refresh.py` before the flag-engine step (spec §5).
4. Manually run once against real data, sanity-check rows in `active_listings_daily`
   and that `listings` gets upserted correctly.
5. Confirm `flag_engine.py`'s existing `flag_stale_listings`/`flag_price_reductions`/
   `flag_new_listings` now actually produce flags (they were previously dead code due
   to the empty `listings` table).
6. Resolve open item #2 (delisting heuristic) before relying on `withdrawn`/`sold`
   status transitions.
7. `back_on_market` and `hot_market_alert` flag types are separate follow-on work,
   not part of this table's initial scope (spec §6).

## Repo / branch state

Cloned at `/workspace/ca-home-pricing-model`, currently on `main` (single upstream
commit visible — shallow clone, `git fetch --unshallow` if full history is needed).
No feature branch has been created for this work yet — confirm branch naming with
the user before committing implementation code.
