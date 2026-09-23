"""Deterministic monthly forecasts of observed sales, not unconstrained demand.

No stockout imputation, anomaly removal, implicit zero filling or future training.
"""

from calendar import monthrange
from collections.abc import Mapping, Sequence
from datetime import date
from math import fsum, isfinite
from numbers import Real
import re


def _month(value: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("month must be YYYY-MM")
    return date.fromisoformat(value + "-01")


def _shift(month: date, offset: int) -> str:
    year, index = divmod(month.year * 12 + month.month - 1 + offset, 12)
    shifted = date(year, index + 1, 1)
    return f"{shifted.year:04d}-{shifted.month:02d}"


def _valid(value: object) -> bool:
    try:
        return isinstance(value, Real) and not isinstance(value, bool) and isfinite(value) and value >= 0
    except OverflowError:
        return False


def forecast_month(
    history: Mapping[str, float | None], target_month: str, as_of: str,
    method: str = "seasonal_growth", growth_override: float | None = None,
) -> dict:
    """Forecast a full month and its portion strictly AFTER the as-of date.

    growth_override is a multiplier (1.2 means +20%), replacing observed growth.
    Zero is a valid override. mean_12 does not accept a growth override.
    training_months includes every required input, even when it is unavailable.
    """
    result = {
        "status": "invalid_input", "method": method, "target_month": target_month,
        "as_of": as_of, "forecast_units": None, "remaining_units": None,
        "trend_multiplier": None, "training_months": [], "warnings": [],
        "assumptions": [
            "History contains observed monthly sales, not cleaned regular demand.",
            "Only complete months strictly before the as-of month are used.",
            "Monthly sales are distributed uniformly over calendar days.",
            "Remaining quantity excludes the as-of day; no actual-to-date subtraction.",
        ],
    }
    try:
        target = _month(target_month)
        if not isinstance(as_of, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", as_of):
            raise ValueError("as_of must be YYYY-MM-DD")
        cutoff = date.fromisoformat(as_of)
        current = cutoff.replace(day=1)
        if target < current:
            raise ValueError("target_month precedes the as-of month")
        if method not in ("seasonal_growth", "mean_12"):
            raise ValueError("unknown method")
        if not isinstance(history, Mapping):
            raise ValueError("history must be a monthly mapping")
        if growth_override is not None and not _valid(growth_override):
            raise ValueError("growth_override must be a finite nonnegative multiplier")
        if method == "mean_12" and growth_override is not None:
            raise ValueError("mean_12 does not use growth_override")
        for month in history:
            _month(month)
        if method == "mean_12":
            recent = [_shift(current, -i) for i in range(12, 0, -1)]
            required = recent
        else:
            basis = _shift(target, -12)
            recent = [_shift(current, -i) for i in range(3, 0, -1)]
            prior = [_shift(current, -i - 12) for i in range(3, 0, -1)]
            required = [basis] + (recent + prior if growth_override is None else [])
    except (ValueError, TypeError, OverflowError) as exc:
        result["warnings"].append(str(exc))
        return result

    result["training_months"] = sorted(set(required))
    result["status"] = "insufficient_data"
    for month in result["training_months"]:
        value = history.get(month)
        if month >= as_of[:7]:
            reason = "not a completed month available as of forecast"
        elif month not in history or value is None:
            reason = "missing monthly sales; not assumed zero"
        elif not _valid(value):
            reason = "negative, nonnumeric or nonfinite monthly sales"
        else:
            continue
        result["warnings"].append(f"{month}: {reason}")
    if result["warnings"]:
        return result

    try:
        days = monthrange(target.year, target.month)[1]
        trend = None
        if method == "mean_12":
            daily_rates = [history[m] / monthrange(_month(m).year, _month(m).month)[1] for m in recent]
            quantity = fsum(daily_rates) / 12 * days
        else:
            if growth_override is None:
                denominator = fsum(history[m] for m in prior)
                if denominator == 0:
                    result["warnings"].append("Prior-year three-month sales sum is zero; growth undefined.")
                    return result
                trend = fsum(history[m] for m in recent) / denominator
            else:
                trend = float(growth_override)
                result["assumptions"].append("Explicit growth multiplier replaces observed growth.")
            quantity = history[basis] * trend
        if not isfinite(quantity) or (trend is not None and not isfinite(trend)):
            raise OverflowError("nonfinite forecast calculation")
    except (OverflowError, ValueError) as exc:
        result["warnings"].append(str(exc))
        return result
    remaining_days = days - cutoff.day if target == current else days
    result.update(status="ok", forecast_units=quantity,
                  remaining_units=quantity * (remaining_days / days), trend_multiplier=trend)
    return result


def rolling_backtest(history: Mapping[str, float | None], months: Sequence[str],
                     actual_history: Mapping[str, float | None] | None = None) -> dict:
    """Compare both forecasts on the SAME valid actual months, past-only per origin.

    Caller must select completed historical target months, never a partial month.
    Eligible means valid actual; tested additionally requires BOTH forecasts.
    Positive bias means overprediction. Metrics are sales units, not inventory ROI.
    Optional actual_history scores raw observations while history may be causally
    adjusted. Each training value must not depend on subsequent months.
    """
    methods = ("seasonal_growth", "mean_12")
    points, skipped, seen = [], [], set()
    eligible = 0
    for month in months:
        try:
            _month(month)
        except (ValueError, TypeError) as exc:
            skipped.append({"month": month, "reasons": [str(exc)]})
            continue
        if month in seen:
            skipped.append({"month": month, "reasons": ["duplicate test month"]})
            continue
        seen.add(month)
        actual = (history if actual_history is None else actual_history).get(month)
        if not _valid(actual):
            skipped.append({"month": month, "reasons": ["Actual is missing, negative or nonfinite."]})
            continue
        eligible += 1
        forecasts = {method: forecast_month(history, month, month + "-01", method) for method in methods}
        reasons = [f"{method}: {warning}" for method, forecast in forecasts.items()
                   if forecast["status"] != "ok" for warning in forecast["warnings"]]
        if reasons:
            skipped.append({"month": month, "reasons": reasons})
            continue
        points.append({"month": month, "actual": actual,
                       "forecasts": {method: value["forecast_units"] for method, value in forecasts.items()}})
    metrics = {}
    for method in methods:
        errors = [point["forecasts"][method] - point["actual"] for point in points]
        metrics[method] = {"mae_units": fsum(abs(e) for e in errors) / len(errors) if errors else None,
                           "bias_units": fsum(e / len(errors) for e in errors) if errors else None}
    return {"status": "ok" if points else "insufficient_data", "requested": len(months),
            "eligible": eligible, "tested": len(points), "skipped": skipped,
            "points": points, "metrics": metrics,
            "assumptions": ["Caller selected complete historical test months.",
                            "Both methods scored on identical eligible observations.",
                            "Scores use raw observed sales, not imputed latent demand." if actual_history is not None
                            else "Scores use the supplied observed history.",
                            "Monthly source is available immediately after month end.",
                            "Forecast accuracy alone does not establish inventory savings."]}
