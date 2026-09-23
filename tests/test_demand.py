"""Deterministic algorithm inputs, not invented contest source rows or companies."""

from calendar import monthrange
from copy import deepcopy

import pytest

from smartbuyer.demand import adjust_demand
from smartbuyer.forecast import rolling_backtest
from smartbuyer.replenishment import plan_item


def sample():
    months = [f"{year}-{month:02d}" for year in (2024, 2025, 2026) for month in range(1, 13)]
    return {"history": {m: monthrange(int(m[:4]), int(m[5:]))[1] * 2 for m in months},
            "stock_history": {m: 10 for m in months}, "transactions_monthly": {}}


def document_month(item, month, quantities):
    item["transactions_monthly"][month] = {"document_quantities": quantities,
        "positive_total": sum(q for q in quantities if q > 0), "document_count": len(quantities)}


def row(report, month):
    return next(row for row in report["monthly"] if row["month"] == month)


def test_single_extreme_document_normalized_to_authoritative_month_without_mutation():
    item = sample()
    item["history"]["2026-08"] = 104
    document_month(item, "2026-08", [10, 10, 10, 10, 1000])
    original = deepcopy(item)
    result = adjust_demand(item, "2026-09-22")
    value = row(result, "2026-08")
    assert value["outlier_threshold"] == 30
    assert value["outlier_documents"] == 1
    assert value["after_outlier"] == pytest.approx(7)
    assert value["removed_units"] == pytest.approx(97)
    assert result["summary"]["outlier_documents"] == 1
    assert item == original


@pytest.mark.parametrize("quantities", [[10, 10, 10, 10, 30], [10, 10, 10, 1000],
                                       [10, 10, 10, 10, -100], [10] * 5])
def test_threshold_boundary_underhistory_returns_and_normal_documents_do_not_remove(quantities):
    item = sample()
    document_month(item, "2026-08", quantities)
    value = row(adjust_demand(item, "2026-09-22"), "2026-08")
    assert value["adjusted"] == value["raw"] == 62
    assert value["outlier_documents"] == 0


def test_stockout_estimate_uses_prior_in_stock_reference_not_lost_days():
    item = sample()
    item["stock_history"]["2026-08"] = 0
    item["history"]["2026-08"] = 10
    value = row(adjust_demand(item, "2026-09-22"), "2026-08")
    assert value["stockout_status"] == "estimated"
    assert value["adjusted"] == 62
    assert value["estimated_lost_units"] == 52
    assert len(value["reference_months"]) == 12
    assert "lost_days" not in value


@pytest.mark.parametrize("opening,raw", [(None, 0), (10, 0), (0, 31), (0, 62), (-1, 0)])
def test_missing_nonzero_stock_or_exact_half_reference_does_not_impute(opening, raw):
    item = sample()
    item["stock_history"]["2026-08"] = opening
    item["history"]["2026-08"] = raw
    value = row(adjust_demand(item, "2026-09-22"), "2026-08")
    assert value["adjusted"] == raw
    assert value["estimated_lost_units"] == 0


@pytest.mark.parametrize("raw", [None, -1, float("nan"), float("inf")])
def test_unknown_negative_nonfinite_sales_not_imputed(raw):
    item = sample()
    item["stock_history"]["2026-08"] = 0
    item["history"]["2026-08"] = raw
    result = adjust_demand(item, "2026-09-22")
    assert result["adjusted_history"]["2026-08"] is None
    assert row(result, "2026-08")["stockout_status"] == "invalid_sales"


def test_minimum_three_prior_months_and_no_recursive_imputation():
    item = sample()
    item["stock_history"] = {"2026-05": 1, "2026-06": 1, "2026-07": 0, "2026-08": 0}
    item["history"].update({"2026-07": 0, "2026-08": 0})
    result = adjust_demand(item, "2026-09-22")
    assert row(result, "2026-08")["stockout_status"] == "insufficient_reference"
    item["stock_history"]["2026-04"] = 1
    result = adjust_demand(item, "2026-09-22")
    assert row(result, "2026-07")["stockout_status"] == "estimated"
    assert row(result, "2026-08")["reference_months"] == ["2026-04", "2026-05", "2026-06"]


def test_reference_is_outlier_cleaned_observed_and_low_season_not_inflated():
    item = sample()
    item["stock_history"]["2026-08"] = 0
    item["history"].update({"2025-08": 4, "2026-08": 3})
    value = row(adjust_demand(item, "2026-09-22"), "2026-08")
    assert value["reference_units"] == 4
    assert value["adjusted"] == 3
    assert value["reference_method"] == "min_trailing_median_and_prior_year_same_month"
    item["history"]["2025-08"] = 104
    document_month(item, "2025-08", [10, 10, 10, 10, 1000])
    value = row(adjust_demand(item, "2026-09-22"), "2026-08")
    assert value["reference_units"] == pytest.approx(7)


def test_partial_and_future_never_train_and_past_rows_do_not_change():
    item = sample()
    item["partial_months"] = ["2026-07"]
    item["stock_history"]["2026-06"] = 0
    item["history"]["2026-06"] = 1
    early = adjust_demand(item, "2026-07-15")
    item["history"]["2026-08"] = 1000000
    document_month(item, "2026-08", [1, 1, 1, 1, 1000000])
    late = adjust_demand(item, "2026-09-22")
    assert all(row(late, r["month"]) == r for r in early["monthly"])
    assert "2026-07" not in late["adjusted_history"]
    assert "2026-09" not in late["adjusted_history"]
    assert "2026-10" not in late["adjusted_history"]


def test_backtest_trains_adjusted_but_scores_same_raw_targets():
    item = sample()
    item["history"]["2026-06"] = 0
    item["stock_history"]["2026-06"] = 0
    adjusted = adjust_demand(item, "2026-09-22")["adjusted_history"]
    result = rolling_backtest(adjusted, ["2026-06", "2026-07"], actual_history=item["history"])
    assert result["tested"] == 2
    assert result["points"][0]["actual"] == 0  # not estimated 60
    assert result["points"][1]["actual"] == 62
    for method in ("mean_12", "seasonal_growth"):
        expected = sum(abs(p["forecasts"][method] - p["actual"]) for p in result["points"]) / 2
        assert result["metrics"][method]["mae_units"] == pytest.approx(expected)


def test_both_adjustments_change_actual_forecast_and_order_not_only_flags():
    item = sample()
    item.update(supplier="unit-test", sku="math", unit="unit", pack_multiple=1, moq=0,
                stock={"free": 0, "reported": 0, "reserved": 0}, inbound=[])
    policy = dict(lead_days=1, cover_days=30, safety_days=0, moq=0, method="mean_12",
                  regular_only=True, use_reported_stock=True)
    item["history"]["2026-08"] = 620
    raw = plan_item(item, "2026-09-22", policy)
    document_month(item, "2026-08", [10, 10, 10, 10, 1000])
    cleaned = plan_item(item, "2026-09-22", policy)
    assert cleaned["order"]["quantity"] < raw["order"]["quantity"]
    item["transactions_monthly"] = {}
    item["history"]["2026-08"] = 0
    raw_low = plan_item(item, "2026-09-22", policy)
    item["stock_history"]["2026-08"] = 0
    estimated = plan_item(item, "2026-09-22", policy)
    assert estimated["order"]["quantity"] > raw_low["order"]["quantity"]
    assert estimated["demand_adjustment"]["summary"]["stockout_months"] == 1


@pytest.mark.parametrize("history", [None, [], "bad", {"bad": 1}, {"2026-13": 1}, {1: 1}])
def test_planner_malformed_history_is_blocked_not_exception(history):
    item = sample()
    item["history"] = history
    result = plan_item(item, "2026-09-22", {})
    assert result["status"] == "blocked" and result["order"]["quantity"] is None
    assert any("истори" in reason for reason in result["reasons"])
