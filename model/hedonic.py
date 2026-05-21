"""
Hedonic regression model.
OLS on log(home_value) — each coefficient is interpretable as a % price effect.
Two components:
  1. Market model: predicts baseline price for a zip/month from market conditions
  2. Property adjustments: multiplicative factors for individual property features
"""

import logging
import pickle
import pathlib

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler
from sklearn.pipeline import Pipeline
from sklearn.model_selection import TimeSeriesSplit, cross_val_score

log = logging.getLogger(__name__)

MODEL_PATH = pathlib.Path(__file__).parent / "saved" / "hedonic.pkl"
MODEL_PATH.parent.mkdir(exist_ok=True)

MARKET_FEATURES = [
    "zhvi_lag1",
    "zhvi_mom",
    "zhvi_lag3",
    "mortgage_rate_30y",
    "fed_funds_rate",
    "consumer_sentiment",
    "sf_hpi",
    "ca_unemployment",
    "cpi",
    "median_dom",
    "active_listings",
    "list_to_sale_ratio",
    "search_volume_ma4",
    "month_num",
    "year",
]


def _prepare_X(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Return feature matrix + list of feature names actually used.
    No zip dummies — they extrapolate poorly in time-series CV.
    Zip identity is captured by zhvi_lag1 which IS zip-specific.
    """
    available = [f for f in MARKET_FEATURES if f in df.columns]
    X = df[available].copy()
    return X, list(X.columns)


def train(df: pd.DataFrame) -> dict:
    """Train market-level hedonic model. Returns metrics dict."""
    target_col = "home_value"
    mask = df[target_col].notna() & df["zhvi_lag1"].notna()
    train_df = df[mask].copy()

    X, feature_names = _prepare_X(train_df)
    y = np.log(train_df[target_col].values)

    # Fill remaining nulls with column medians
    X = X.fillna(X.median())

    pipe = Pipeline([
        ("scaler", StandardScaler()),
        ("ridge",  Ridge(alpha=10.0)),
    ])

    # Time-series split: each fold uses past data to predict future months
    tscv = TimeSeriesSplit(n_splits=5)
    scores = cross_val_score(pipe, X, y, cv=tscv, scoring="r2")
    log.info(f"Hedonic CV R² scores (time-series): {scores.round(4)} | mean={scores.mean():.4f}")

    pipe.fit(X, y)

    MODEL_PATH.write_bytes(pickle.dumps({
        "pipe":          pipe,
        "feature_names": feature_names,
        "train_rows":    len(train_df),
        "cv_r2_mean":    float(scores.mean()),
        "cv_r2_std":     float(scores.std()),
    }))
    log.info(f"Hedonic model saved → {MODEL_PATH}")

    return {
        "model":         "hedonic_ridge",
        "train_rows":    len(train_df),
        "cv_r2_mean":    float(scores.mean()),
        "cv_r2_std":     float(scores.std()),
        "feature_count": len(feature_names),
    }


def predict_market(feature_row: dict) -> float:
    """Predict market baseline price (log-space → dollars) for a feature dict."""
    saved = pickle.loads(MODEL_PATH.read_bytes())
    pipe: Pipeline = saved["pipe"]
    names: list    = saved["feature_names"]

    row = pd.DataFrame([feature_row])
    # Align to training feature set
    for col in names:
        if col not in row.columns:
            row[col] = 0.0
    row = row[names].fillna(0.0)

    log_price = pipe.predict(row)[0]
    return float(np.exp(log_price))


# ── Property adjustment factors ──────────────────────────────────────────────
# Coefficients calibrated from Bay Area hedonic pricing literature.
# Each returns a multiplier applied to the market baseline.

def size_factor(sqft_living: int, median_sqft: int = 1850) -> float:
    """Diminishing returns on size: doubling sqft adds ~65% not 100%."""
    if not sqft_living or sqft_living <= 0:
        return 1.0
    return (sqft_living / median_sqft) ** 0.65


def lot_factor(sqft_lot: int, median_lot: int = 6500) -> float:
    if not sqft_lot or sqft_lot <= 0:
        return 1.0
    return (sqft_lot / median_lot) ** 0.10   # lot adds value but slowly


def age_factor(year_built: int, current_year: int = 2026) -> float:
    """Depreciation: ~0.3%/yr for first 30 yrs, then levels off. Vintage bonus >80 yrs."""
    if not year_built:
        return 1.0
    age = current_year - year_built
    if age <= 0:
        return 1.02        # brand new premium
    if age > 80:
        return 0.97        # vintage/character homes — slight discount relative to new
    depreciation = min(age * 0.003, 0.20)
    return 1.0 - depreciation


def bed_factor(bedrooms: int, median_beds: int = 3) -> float:
    """Each bed above/below median worth ~3% relative to median."""
    if not bedrooms:
        return 1.0
    delta = bedrooms - median_beds
    return 1.0 + delta * 0.03


def bath_factor(bathrooms: float, median_baths: float = 2.0) -> float:
    """Each bath above/below median worth ~4%."""
    if not bathrooms:
        return 1.0
    delta = bathrooms - median_baths
    return 1.0 + delta * 0.04


def pool_factor(has_pool: bool) -> float:
    return 1.025 if has_pool else 1.0


def view_factor(view_score: int) -> float:
    """0=none, 1=partial (+5%), 2=panoramic (+12%)."""
    return {0: 1.0, 1: 1.05, 2: 1.12}.get(view_score or 0, 1.0)


def corner_lot_factor(is_corner: bool) -> float:
    return 0.98 if is_corner else 1.0   # corner lots often less private


def busy_street_penalty(is_busy: bool) -> float:
    return 0.925 if is_busy else 1.0    # -7.5% for arterial/highway adjacency


def slope_penalty(slope_grade: int) -> float:
    """slope_grade 0–100. Steeper = harder access, more maintenance."""
    if not slope_grade:
        return 1.0
    if slope_grade < 15:
        return 1.0
    if slope_grade < 30:
        return 0.97
    if slope_grade < 50:
        return 0.94
    return 0.90


def hoa_penalty(hoa_monthly: int, cap_rate: float = 0.05) -> float:
    """Capitalize annual HOA cost at given cap rate as a price reduction."""
    if not hoa_monthly or hoa_monthly <= 0:
        return 1.0
    annual_hoa = hoa_monthly * 12
    # HOA cost capitalized reduces effective price by annual_hoa / cap_rate
    # but capped at 15% of base price to avoid absurd results
    return max(0.85, 1.0 - (annual_hoa / cap_rate) / 1_500_000)


def apply_property_adjustments(base_price: float, prop: dict) -> dict:
    """
    Apply all property-specific multipliers to base_price.
    Returns dict with final adjusted price and factor breakdown.
    """
    factors = {
        "size":        size_factor(prop.get("sqft_living"), prop.get("median_sqft_zip", 1850)),
        "lot":         lot_factor(prop.get("sqft_lot"), prop.get("median_lot_zip", 6500)),
        "age":         age_factor(prop.get("year_built")),
        "bedrooms":    bed_factor(prop.get("bedrooms"), prop.get("median_beds_zip", 3)),
        "bathrooms":   bath_factor(prop.get("bathrooms"), prop.get("median_baths_zip", 2.0)),
        "pool":        pool_factor(prop.get("pool", False)),
        "view":        view_factor(prop.get("view_score", 0)),
        "corner_lot":  corner_lot_factor(prop.get("corner_lot", False)),
        "busy_street": busy_street_penalty(prop.get("busy_street", False)),
        "slope":       slope_penalty(prop.get("slope_grade", 0)),
        "hoa":         hoa_penalty(prop.get("hoa_monthly", 0)),
    }

    combined = 1.0
    for f in factors.values():
        combined *= f

    adjusted_price = base_price * combined

    return {
        "base_price":      round(base_price),
        "adjusted_price":  round(adjusted_price),
        "combined_factor": round(combined, 4),
        "factors":         {k: round(v, 4) for k, v in factors.items()},
    }
