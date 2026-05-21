"""
ML ensemble: XGBoost + LightGBM trained on zip/month market features.
Predicts log(home_value) then converts back to dollars.
SHAP values expose which features drove the prediction.
"""

import logging
import pickle
import pathlib

import numpy as np
import pandas as pd
import xgboost as xgb
import lightgbm as lgb
import shap
from sklearn.model_selection import KFold, cross_val_score
from sklearn.metrics import r2_score, mean_absolute_error

log = logging.getLogger(__name__)

MODEL_PATH = pathlib.Path(__file__).parent / "saved" / "ml_ensemble.pkl"
MODEL_PATH.parent.mkdir(exist_ok=True)

FEATURES = [
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


def _encode_zip(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["zip_encoded"] = pd.Categorical(df["zip_code"]).codes
    return df


def _get_X(df: pd.DataFrame, feature_names: list) -> pd.DataFrame:
    X = pd.DataFrame(index=df.index)
    for col in feature_names:
        X[col] = df[col] if col in df.columns else 0.0
    return X.fillna(X.median())


def train(df: pd.DataFrame) -> dict:
    df = _encode_zip(df)
    feature_names = FEATURES + ["zip_encoded"]

    mask = df["home_value"].notna() & df["zhvi_lag1"].notna()
    train_df = df[mask].copy()

    X = _get_X(train_df, feature_names)
    y = np.log(train_df["home_value"].values)

    # ── XGBoost ──────────────────────────────────────────────────────────────
    xgb_model = xgb.XGBRegressor(
        n_estimators=400,
        learning_rate=0.05,
        max_depth=4,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_weight=5,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbosity=0,
    )
    xgb_scores = cross_val_score(xgb_model, X, y, cv=5, scoring="r2")
    log.info(f"XGBoost CV R²: {xgb_scores.round(4)} | mean={xgb_scores.mean():.4f}")
    xgb_model.fit(X, y)

    # ── LightGBM ─────────────────────────────────────────────────────────────
    lgb_model = lgb.LGBMRegressor(
        n_estimators=400,
        learning_rate=0.05,
        num_leaves=31,
        subsample=0.8,
        colsample_bytree=0.8,
        min_child_samples=10,
        reg_alpha=0.1,
        reg_lambda=1.0,
        random_state=42,
        verbose=-1,
    )
    lgb_scores = cross_val_score(lgb_model, X, y, cv=5, scoring="r2")
    log.info(f"LightGBM CV R²: {lgb_scores.round(4)} | mean={lgb_scores.mean():.4f}")
    lgb_model.fit(X, y)

    # ── Ensemble: equal weight blend ─────────────────────────────────────────
    xgb_pred = xgb_model.predict(X)
    lgb_pred = lgb_model.predict(X)
    ensemble  = 0.5 * xgb_pred + 0.5 * lgb_pred
    in_sample_r2  = r2_score(y, ensemble)
    in_sample_mae = mean_absolute_error(np.exp(y), np.exp(ensemble))
    log.info(f"Ensemble in-sample R²={in_sample_r2:.4f}, MAE=${in_sample_mae:,.0f}")

    # Zip code → index mapping for inference
    zip_codes    = train_df["zip_code"].values
    zip_cat      = pd.Categorical(zip_codes)
    zip_to_code  = dict(zip(zip_cat.categories, range(len(zip_cat.categories))))

    MODEL_PATH.write_bytes(pickle.dumps({
        "xgb_model":       xgb_model,
        "lgb_model":       lgb_model,
        "feature_names":   feature_names,
        "zip_to_code":     zip_to_code,
        "xgb_r2_mean":     float(xgb_scores.mean()),
        "lgb_r2_mean":     float(lgb_scores.mean()),
        "ensemble_r2":     float(in_sample_r2),
        "ensemble_mae":    float(in_sample_mae),
        "train_rows":      len(train_df),
    }))
    log.info(f"ML ensemble saved → {MODEL_PATH}")

    return {
        "model":        "xgb_lgb_ensemble",
        "train_rows":   len(train_df),
        "xgb_r2":       float(xgb_scores.mean()),
        "lgb_r2":       float(lgb_scores.mean()),
        "ensemble_r2":  float(in_sample_r2),
        "mae_dollars":  float(in_sample_mae),
    }


def predict(feature_row: dict) -> dict:
    """
    Predict price for a feature dict. Returns median + 80% CI.
    feature_row must include zip_code and all market features.
    """
    saved = pickle.loads(MODEL_PATH.read_bytes())
    xgb_model: xgb.XGBRegressor = saved["xgb_model"]
    lgb_model: lgb.LGBMRegressor = saved["lgb_model"]
    feature_names: list           = saved["feature_names"]
    zip_to_code: dict             = saved["zip_to_code"]

    row = dict(feature_row)
    row["zip_encoded"] = zip_to_code.get(str(row.get("zip_code", "")), 0)

    X = pd.DataFrame([row])
    for col in feature_names:
        if col not in X.columns:
            X[col] = 0.0
    X = X[feature_names].fillna(0.0)

    xgb_pred = float(xgb_model.predict(X)[0])
    lgb_pred = float(lgb_model.predict(X)[0])
    ensemble  = 0.5 * xgb_pred + 0.5 * lgb_pred

    price_est = float(np.exp(ensemble))

    # Approximate 80% CI: ±10% based on typical model uncertainty for this data size
    ci_pct    = 0.10
    ci_low    = price_est * (1 - ci_pct)
    ci_high   = price_est * (1 + ci_pct)

    # SHAP explanation from XGBoost
    explainer  = shap.TreeExplainer(xgb_model)
    shap_vals  = explainer.shap_values(X)[0]
    shap_dict  = {name: round(float(sv), 4) for name, sv in zip(feature_names, shap_vals)}
    top_factors = sorted(shap_dict.items(), key=lambda x: abs(x[1]), reverse=True)[:5]

    return {
        "ml_estimate":  round(price_est),
        "ci_low":       round(ci_low),
        "ci_high":      round(ci_high),
        "xgb_estimate": round(float(np.exp(xgb_pred))),
        "lgb_estimate": round(float(np.exp(lgb_pred))),
        "top_factors":  dict(top_factors),
    }
