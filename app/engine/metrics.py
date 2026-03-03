"""
Metrics computation module.

Provides RMSE, MAE, MAPE, MBE (mean bias error) and skill score.
All functions work on plain Python lists – no external deps.
"""

import math
from typing import Optional


def rmse(actual: list[float], predicted: list[float]) -> Optional[float]:
    """Root Mean Square Error – penalises large errors more heavily."""
    pairs = [(a, p) for a, p in zip(actual, predicted) if a is not None and p is not None]
    if not pairs:
        return None
    return math.sqrt(sum((a - p) ** 2 for a, p in pairs) / len(pairs))


def mae(actual: list[float], predicted: list[float]) -> Optional[float]:
    """Mean Absolute Error."""
    pairs = [(a, p) for a, p in zip(actual, predicted) if a is not None and p is not None]
    if not pairs:
        return None
    return sum(abs(a - p) for a, p in pairs) / len(pairs)


def mbe(actual: list[float], predicted: list[float]) -> Optional[float]:
    """
    Mean Bias Error (signed).
    Positive = model over-predicts on average.
    Negative = model under-predicts on average.
    """
    pairs = [(a, p) for a, p in zip(actual, predicted) if a is not None and p is not None]
    if not pairs:
        return None
    return sum(p - a for a, p in pairs) / len(pairs)


def mape(actual: list[float], predicted: list[float], min_actual: float = 10.0) -> Optional[float]:
    """
    Mean Absolute Percentage Error.
    Skips days where actual < min_actual to avoid division by near-zero.
    """
    pairs = [
        (a, p)
        for a, p in zip(actual, predicted)
        if a is not None and p is not None and a >= min_actual
    ]
    if not pairs:
        return None
    return sum(abs(a - p) / a * 100 for a, p in pairs) / len(pairs)


def skill_score(rmse_calibrated: float, rmse_raw: float) -> Optional[float]:
    """
    Forecast skill score relative to raw (uncalibrated) forecast.
    Returns percentage improvement: positive = calibration helps.
    skill = (1 - rmse_calibrated / rmse_raw) * 100
    """
    if rmse_raw is None or rmse_raw == 0:
        return None
    return (1 - rmse_calibrated / rmse_raw) * 100


def percentile(values: list[float], p: float) -> Optional[float]:
    """Compute p-th percentile (0–100) of a list."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    idx = (len(vals) - 1) * p / 100
    lo, hi = int(idx), min(int(idx) + 1, len(vals) - 1)
    return vals[lo] + (vals[hi] - vals[lo]) * (idx - lo)


def moving_average(values: list[Optional[float]], window: int) -> list[Optional[float]]:
    """
    Compute a trailing moving average of `window` elements.
    None values are skipped in the calculation but preserved in output.
    """
    result: list[Optional[float]] = []
    for i, v in enumerate(values):
        if v is None:
            result.append(None)
            continue
        window_vals = [x for x in values[max(0, i - window + 1) : i + 1] if x is not None]
        result.append(sum(window_vals) / len(window_vals) if window_vals else None)
    return result
