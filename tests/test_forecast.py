"""Checks use supplied XLSX values only; no generated business-data fixture."""

from calendar import monthrange
from pathlib import Path
import os

from openpyxl import load_workbook
import pytest

from smartbuyer.forecast import forecast_month, rolling_backtest


@pytest.fixture(scope="module")
def supplied_sales():
    root = Path(os.environ.get("HACKALEM_DATA_DIR", "C:/Users/emilk/OneDrive/Desktop/HackAlem_Logistics_2026-09-23/04_Excel"))
    path = root / "Systeme electric" / "Ежемесячные продажи в кол-м выражении SystemElectric 2024-2026.xlsx"
    if not path.is_file():
        pytest.skip("Provide issued workbooks with HACKALEM_DATA_DIR; no substitute data is generated.")
    book = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(book["Лист_1"].values)
    finally:
        book.close()
    # E:AK are the source's consecutive Jan 2024..Sep 2026 month columns.
    assert len(rows[0][4:37]) == 33
    assert "2024" in str(rows[0][4]) and "2026" in str(rows[0][36])
    row = next(row for row in rows[2:] if row[2] == "ATN544045")
    months = [f"{2024 + i // 12:04d}-{i % 12 + 1:02d}" for i in range(33)]
    history = dict(zip(months, row[4:37]))
    negative = next(value for row in rows[2:] for value in row[4:37]
                    if isinstance(value, (int, float)) and value < 0)
    return history, negative


def test_seasonal_growth_matches_independent_source_arithmetic(supplied_sales):
    h, _ = supplied_sales
    result = forecast_month(h, "2026-10", "2026-09-22")
    numerator = sum(h[f"2026-{m:02d}"] for m in (6, 7, 8))
    denominator = sum(h[f"2025-{m:02d}"] for m in (6, 7, 8))
    assert result["status"] == "ok"
    assert result["trend_multiplier"] == pytest.approx(numerator / denominator)
    assert result["forecast_units"] == pytest.approx(h["2025-10"] * numerator / denominator)
    assert result["remaining_units"] == result["forecast_units"]
    assert all(month < "2026-09" for month in result["training_months"])


@pytest.mark.parametrize("method", ["seasonal_growth", "mean_12"])
def test_partial_and_future_months_never_change_forecast(supplied_sales, method):
    h, _ = supplied_sales
    edited = dict(h, **{"2026-09": h["2024-10"], "2026-10": h["2024-11"]})
    assert forecast_month(h, "2026-10", "2026-09-22", method) == forecast_month(edited, "2026-10", "2026-09-22", method)


def test_current_month_only_days_after_cutoff(supplied_sales):
    h, _ = supplied_sales
    result = forecast_month(h, "2026-09", "2026-09-22")
    assert result["remaining_units"] == pytest.approx(result["forecast_units"] * (30 - 22) / 30)
    assert forecast_month(h, "2026-09", "2026-09-30")["remaining_units"] == 0


def test_mean12_uses_each_month_day_count_including_leap_february(supplied_sales):
    h, _ = supplied_sales
    result = forecast_month(h, "2025-02", "2025-01-31", "mean_12")
    expected = sum(h[f"2024-{m:02d}"] / monthrange(2024, m)[1] for m in range(1, 13)) / 12 * 28
    assert monthrange(2024, 2)[1] == 29
    assert result["forecast_units"] == pytest.approx(expected)


@pytest.mark.parametrize("guard", ["missing", "null", "negative", "nonfinite"])
def test_missing_or_invalid_source_not_replaced_with_zero(supplied_sales, guard):
    h, negative = supplied_sales
    edited = dict(h)
    if guard == "missing":
        del edited["2025-10"]
    else:
        edited["2025-10"] = {"null": None, "negative": negative, "nonfinite": float("nan")}[guard]
    result = forecast_month(edited, "2026-10", "2026-09-22")
    assert result["status"] == "insufficient_data"
    assert result["forecast_units"] is None and result["trend_multiplier"] is None
    assert "2025-10" in result["warnings"][0]


def test_override_replaces_observed_trend_and_zero_is_valid(supplied_sales):
    h, _ = supplied_sales
    edited = {month: qty for month, qty in h.items() if month not in ("2026-06", "2025-06")}
    for override in (0, 1):  # algorithm settings, not invented sales observations
        result = forecast_month(edited, "2026-10", "2026-09-22", growth_override=override)
        assert result["status"] == "ok"
        assert result["forecast_units"] == h["2025-10"] * override
        assert result["trend_multiplier"] == override
        assert result["training_months"] == ["2025-10"]


@pytest.mark.parametrize("override", [-1, float("nan"), float("inf"), True])
def test_invalid_override(supplied_sales, override):
    h, _ = supplied_sales
    assert forecast_month(h, "2026-10", "2026-09-22", growth_override=override)["status"] == "invalid_input"


@pytest.mark.parametrize("target,as_of", [("2026-9", "2026-09-22"), ("2026-10", "2026-9-22"), ("2026-10", "2026-02-30"), ("2026-08", "2026-09-22")])
def test_date_validation(supplied_sales, target, as_of):
    assert forecast_month(supplied_sales[0], target, as_of)["status"] == "invalid_input"


def test_known_but_not_yet_available_prior_year_month_is_rejected(supplied_sales):
    result = forecast_month(supplied_sales[0], "2027-09", "2026-09-22", growth_override=1)
    assert result["status"] == "insufficient_data"
    assert "not a completed month" in result["warnings"][0]


def test_method_and_baseline_override_are_not_silently_ignored(supplied_sales):
    h, _ = supplied_sales
    assert forecast_month(h, "2026-10", "2026-09-22", "unknown")["status"] == "invalid_input"
    assert forecast_month(h, "2026-10", "2026-09-22", "mean_12", 1)["status"] == "invalid_input"


def test_backtest_no_valid_points_does_not_report_zero_error(supplied_sales):
    h, _ = supplied_sales
    result = rolling_backtest(h, ["2024-01"])
    assert result["status"] == "insufficient_data" and result["tested"] == 0
    assert all(metric["mae_units"] is None and metric["bias_units"] is None for metric in result["metrics"].values())


def test_rolling_backtest_matches_same_actual_points_and_past_only(supplied_sales):
    h, _ = supplied_sales
    months = [f"2026-{m:02d}" for m in range(1, 9)]
    result = rolling_backtest(h, months)
    assert result["tested"] == result["eligible"] == len(months)
    assert result["skipped"] == []
    for method in ("seasonal_growth", "mean_12"):
        errors = []
        for point in result["points"]:
            month = point["month"]
            past = {key: value for key, value in h.items() if key < month}
            expected = forecast_month(past, month, month + "-01", method)
            assert point["forecasts"][method] == expected["forecast_units"]
            assert point["actual"] == h[month]
            errors.append(point["forecasts"][method] - h[month])
        assert result["metrics"][method]["mae_units"] == pytest.approx(sum(map(abs, errors)) / len(errors))
        assert result["metrics"][method]["bias_units"] == pytest.approx(sum(errors) / len(errors))
    edited = dict(h)
    del edited["2025-02"]  # same point must be excluded for both methods
    skipped = rolling_backtest(edited, months)
    assert skipped["tested"] < result["tested"]
    assert skipped["skipped"]
    assert all(set(point["forecasts"]) == {"seasonal_growth", "mean_12"} for point in skipped["points"])
