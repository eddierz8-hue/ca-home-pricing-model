"""
Comparable-home (comp) analysis engine.

For each comp address:
  1. Geocode → zip_code, city
  2. Run the same ML+hedonic model with the subject property's characteristics
  3. Collect estimates

Blend: 40% comp average + 60% subject ML estimate → blended_price.
Pass _save=False so comp predictions are not written to model_predictions.
"""

import sys
import pathlib
import logging

sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

log = logging.getLogger(__name__)

COMP_WEIGHT = 0.40


def analyze_comps(
    comp_addresses: list,
    subject_prop: dict,
    subject_estimate: float,
) -> dict:
    """
    Geocode each comp, run prediction, blend with subject estimate.

    Args:
        comp_addresses:   list of free-text address strings
        subject_prop:     same property dict passed to predict_for_zip
        subject_estimate: ML+hedonic estimate for the subject property

    Returns dict with keys:
      comps, comp_avg, blended_price, comp_weight, n_valid
    """
    from data.geocode import geocode
    from model.predict import predict_for_zip

    results = []
    valid_estimates = []

    for addr in comp_addresses:
        if not addr or not addr.strip():
            continue
        geo = geocode(addr)
        if not geo:
            results.append({"address": addr, "error": "Geocoding failed", "estimate": None})
            continue
        if not geo.get("in_target"):
            results.append({
                "address":  addr,
                "zip_code": geo.get("zip_code"),
                "city":     geo.get("city"),
                "error":    "Not in target area",
                "estimate": None,
            })
            continue

        zip_code = geo["zip_code"]
        city     = geo["city"]
        try:
            pred = predict_for_zip(zip_code, city, subject_prop, _save=False)
            est  = pred.get("adjusted_estimate")
            results.append({
                "address":     addr,
                "zip_code":    zip_code,
                "city":        city,
                "estimate":    est,
                "ml_baseline": pred.get("ml_estimate"),
            })
            if est:
                valid_estimates.append(est)
        except Exception as e:
            log.warning(f"Comp prediction failed for '{addr}': {e}")
            results.append({"address": addr, "error": str(e), "estimate": None})

    if not valid_estimates:
        return {
            "comps":         results,
            "comp_avg":      None,
            "blended_price": round(subject_estimate),
            "comp_weight":   0,
            "n_valid":       0,
        }

    comp_avg     = sum(valid_estimates) / len(valid_estimates)
    blended      = round(COMP_WEIGHT * comp_avg + (1 - COMP_WEIGHT) * subject_estimate)

    return {
        "comps":         results,
        "comp_avg":      round(comp_avg),
        "blended_price": blended,
        "comp_weight":   COMP_WEIGHT,
        "n_valid":       len(valid_estimates),
    }
