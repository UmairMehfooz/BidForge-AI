"""
BidForge AI — Win-Probability Model
====================================
Trains a scikit-learn LogisticRegression on the 120-row historical bid
dataset (app/data/bid_history.csv) at startup and exposes win-probability
predictions for the scoring engine.

Features
--------
    score_pct        — bid quality score 0–100  (Score (%) column)
    compliance_pct   — compliance percentage 0–100
    gaps_found       — number of compliance gaps
    budget_m         — contract budget in numeric millions
    sector_win_rate  — historical win rate of the bid's sector (0–1)

Target: Outcome (Win=1 / Loss=0).

Public API
----------
    ok = train_win_model()                      # called once at startup
    p = predict_win_probability(features: dict) # → float in [0, 1]
    insights = get_model_insights()             # coefficients, accuracy, boundary

Design decisions
----------------
- StandardScaler + LogisticRegression inside a sklearn Pipeline; training on
  120 rows takes milliseconds, well under the 2-second startup budget.
- Missing feature values at predict time are imputed with the training
  medians so the model never raises on partial inputs.
- NOTE: in this synthetic dataset "Score (%) >= 70" perfectly separates
  Win from Loss, so train accuracy ≈ 1.0 is EXPECTED, not a bug. The
  discovered boundary is surfaced via get_model_insights() so the demo can
  call it out honestly.
- If the bid history CSV is missing, train_win_model() returns False and
  predict_win_probability() returns None — the scoring engine then falls
  back to pure heuristics.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import StandardScaler

from app.services.bid_history import get_sector_win_rate, load_bid_history

logger = logging.getLogger(__name__)

FEATURE_NAMES = ["score_pct", "compliance_pct", "gaps_found", "budget_m", "sector_win_rate"]

_pipeline: Pipeline | None = None
_train_medians: dict[str, float] = {}
_train_ranges: dict[str, tuple[float, float]] = {}
_insights: dict[str, Any] = {}


def _build_feature_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Map raw bid-history columns onto the model's feature names."""
    sector_rates = df["Sector"].map(lambda s: get_sector_win_rate(s))
    features = pd.DataFrame({
        "score_pct"      : df["Score (%)"].astype(float),
        "compliance_pct" : df["Compliance %"].astype(float),
        "gaps_found"     : df["Gaps Found"].astype(float),
        "budget_m"       : df["budget_m"].astype(float),
        "sector_win_rate": sector_rates.astype(float),
    })
    # budget_m can be NaN if a row's Budget string failed to parse
    return features.fillna(features.median(numeric_only=True))


def train_win_model() -> bool:
    """
    Train the logistic regression on the historical bid dataset.
    Returns True on success, False when history is unavailable (the scoring
    engine then uses pure heuristics). Never raises.
    """
    global _pipeline, _train_medians, _train_ranges, _insights

    df = load_bid_history()
    if df is None or df.empty:
        logger.warning("Win model NOT trained — bid history unavailable.")
        return False

    try:
        t0 = time.perf_counter()
        X = _build_feature_frame(df)
        y = df["won"].astype(int)

        pipeline = Pipeline([
            ("scaler", StandardScaler()),
            ("clf", LogisticRegression(max_iter=1000)),
        ])
        pipeline.fit(X, y)

        accuracy = float(pipeline.score(X, y))
        clf: LogisticRegression = pipeline.named_steps["clf"]
        coefficients = {
            name: round(float(coef), 4)
            for name, coef in zip(FEATURE_NAMES, clf.coef_[0])
        }

        # Surface the dataset's separating boundary on the dominant feature:
        # highest losing score vs lowest winning score on Score (%).
        max_loss_score = float(df.loc[df["won"] == 0, "Score (%)"].max())
        min_win_score = float(df.loc[df["won"] == 1, "Score (%)"].min())
        perfectly_separable = min_win_score > max_loss_score

        _pipeline = pipeline
        _train_medians = X.median().to_dict()
        # Inputs outside the training distribution saturate the scaler
        # (e.g. 279 gaps vs a 0-8 training range pushed z to ~114 and the
        # probability to 0.9998) — predictions clamp to these ranges.
        _train_ranges = {
            name: (float(X[name].min()), float(X[name].max()))
            for name in FEATURE_NAMES
        }
        _insights = {
            "coefficients"   : coefficients,
            "train_accuracy" : round(accuracy, 4),
            "dominant_feature": max(coefficients, key=lambda k: abs(coefficients[k])),
            "decision_boundary": {
                "feature"            : "score_pct",
                "max_losing_score"   : max_loss_score,
                "min_winning_score"  : min_win_score,
                "perfectly_separable": perfectly_separable,
                "note": (
                    f"Score (%) >= {min_win_score:.0f} separates Win from Loss in the "
                    "historical dataset"
                    + (" perfectly — expected with this synthetic data." if perfectly_separable else ".")
                ),
            },
            "n_samples"     : int(len(df)),
            "train_seconds" : round(time.perf_counter() - t0, 3),
        }

        logger.info(
            "Win model trained: %d rows in %.3fs, train accuracy %.3f, "
            "dominant feature '%s', boundary Score>=%.0f (separable=%s)",
            len(df), _insights["train_seconds"], accuracy,
            _insights["dominant_feature"], min_win_score, perfectly_separable,
        )
        return True
    except Exception as exc:
        logger.warning("Win model training failed (%s) — falling back to heuristics.", exc)
        _pipeline = None
        return False


def predict_win_probability(features: dict[str, Any]) -> float | None:
    """
    Predict the probability of winning for a bid described by `features`
    (keys from FEATURE_NAMES; missing/None values are imputed with the
    training medians). Returns None if the model is not trained.
    """
    if _pipeline is None:
        return None

    row = []
    for name in FEATURE_NAMES:
        value = features.get(name)
        if value is None or (isinstance(value, float) and np.isnan(value)):
            value = _train_medians.get(name, 0.0)
        value = float(value)
        lo, hi = _train_ranges.get(name, (value, value))
        row.append(min(max(value, lo), hi))

    X = pd.DataFrame([row], columns=FEATURE_NAMES)
    prob = float(_pipeline.predict_proba(X)[0][1])
    return round(prob, 4)


def get_model_insights() -> dict[str, Any]:
    """Coefficients, train accuracy, and the discovered decision boundary."""
    return dict(_insights)


def is_trained() -> bool:
    return _pipeline is not None
