# CA Home Pricing Model

Residential home pricing model for 8 East Bay cities: Berkeley, Orinda, Moraga, Lafayette, Walnut Creek, Alamo, Danville, and San Ramon.

Given an address, the model returns a pricing suggestion grounded in:
- **Hedonic regression** — explicit feature coefficients (beds, sqft, slope, busy street, DOM, etc.)
- **ML ensemble** — XGBoost/LightGBM for accuracy
- **Economic context** — mortgage rates, employment, consumer sentiment, local inventory

## Cities & Zip Codes

| City | County | Zips |
|---|---|---|
| Berkeley | Alameda | 94702–94710 |
| Orinda | Contra Costa | 94563 |
| Moraga | Contra Costa | 94556 |
| Lafayette | Contra Costa | 94549 |
| Walnut Creek | Contra Costa | 94595–94598 |
| Alamo | Contra Costa | 94507 |
| Danville | Contra Costa | 94506, 94526 |
| San Ramon | Contra Costa | 94582, 94583 |

## Data Sources

| Source | Data | Key |
|---|---|---|
| Zillow Research | ZHVI, list/sale prices, DOM, price cuts | Free CSV |
| Redfin Data Center | Market tracker (city + zip) | Free CSV |
| FRED (St. Louis Fed) | Mortgage rates, CA unemployment, Case-Shiller | Free API key |
| Google Trends | Search demand proxy per city | No key needed |

## Setup

```bash
cd home_pricing
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Fill in DB_PASS and FRED_API_KEY in .env
```

Apply schema:
```bash
mysql -u newuser -p home_pricing < db/schema.sql
```

## Running

**Initial historical load (3 years):**
```bash
python -m data.ingest.zillow_research
python -m data.ingest.fred
python -m data.ingest.redfin
python -m data.ingest.google_trends
```

**Daily refresh:**
```bash
python -m scheduler.daily_refresh
```

## Project Structure

```
home_pricing/
├── db/
│   ├── schema.sql          # MySQL schema (10 tables)
│   └── connection.py       # Connection pool
├── data/
│   └── ingest/
│       ├── zillow_research.py
│       ├── fred.py
│       ├── redfin.py
│       └── google_trends.py
├── model/                  # Hedonic + ML models (coming next)
├── scheduler/
│   ├── daily_refresh.py    # Orchestrator
│   └── flag_engine.py      # Stale/reduction/new flags
└── dashboard/              # Web UI (coming next)
```

## Database Schema

10 tables: `properties`, `listings`, `sales`, `market_metrics`, `zhvi`, `economic_indicators`, `consumer_sentiment`, `model_predictions`, `property_flags`, `target_cities`.
