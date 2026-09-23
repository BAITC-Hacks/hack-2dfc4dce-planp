"""Source-derived checks; policy/ETA controls are test settings, not fake records."""

from calendar import monthrange
from copy import deepcopy
from datetime import date, timedelta
from math import ceil
import os
from pathlib import Path

import pytest

from smartbuyer.data import load_dataset
from smartbuyer.replenishment import plan_item


@pytest.fixture(scope="module")
def dataset():
    root = Path(os.environ.get("HACKALEM_DATA_DIR", os.environ.get("HACKALEM_DATA_ROOT", str(
        Path.home() / "OneDrive/Desktop/HackAlem_Logistics_2026-09-23/04_Excel"))))
    if not root.is_dir():
        pytest.skip("Set HACKALEM_DATA_DIR to the supplied workbooks; no generated substitute.")
    return load_dataset(root)


@pytest.fixture
def policy():
    # Explicit manager controls for testing, not assertions of the firm's policy.
    return dict(lead_days=2, cover_days=30, safety_days=0, moq=0,
                method="mean_12", regular_only=True, use_reported_stock=True)


@pytest.fixture(scope="module")
def atlas(dataset):
    return next(i for i in dataset["items"] if i["supplier"] == "SystemElectric" and i["sku"] == "ATN544045")


def test_daily_balance_and_rounding_match_independent_arithmetic(atlas, policy, dataset):
    policy.update(cover_days=180, safety_days=7, moq=atlas["pack_multiple"])
    result = plan_item(atlas, dataset["as_of"], policy)
    assert result["status"] == "provisional"
    months = [f"2025-{m:02d}" for m in range(9, 13)] + [f"2026-{m:02d}" for m in range(1, 9)]
    adjusted = result["demand_adjustment"]["adjusted_history"]
    rate = sum(adjusted[m] / monthrange(int(m[:4]), int(m[5:]))[1] for m in months) / 12
    balance = atlas["stock"]["free"]
    for row in result["calendar"]:
        balance += row["inbound"] - rate
        assert row["demand"] == pytest.approx(rate)
        assert row["without_order"] == pytest.approx(balance)
        assert row["safety"] == pytest.approx(rate * policy["safety_days"])
    relevant = [r for r in result["calendar"] if r["date"] >= result["order"]["arrival"]]
    assert len(relevant) == policy["cover_days"]
    raw = max(0, max(r["safety"] - r["without_order"] for r in relevant))
    pack = atlas["pack_multiple"]
    expected = pack * ceil(max(raw, policy["moq"]) / pack) if raw > 0 else 0
    assert result["order"]["quantity"] == expected
    assert expected > 0 and expected >= raw
    assert result["order"]["planned_order_date"] == dataset["as_of"]
    assert result["order"]["latest_order_date"] is None
    assert all(r["with_order"] >= r["safety"] - 1e-8 for r in relevant)
    assert result["sources"] == atlas["source_refs"]


@pytest.mark.parametrize("key", ["lead_days", "cover_days", "safety_days", "moq"])
def test_missing_policy_blocks_without_fake_defaults(atlas, policy, key, dataset):
    del policy[key]
    result = plan_item(atlas, dataset["as_of"], policy)
    assert result["status"] == "blocked" and result["order"]["quantity"] is None
    assert result["calendar"] == [] and any(key in reason for reason in result["reasons"])


@pytest.mark.parametrize("key,value", [("lead_days", -1), ("lead_days", 0.5), ("lead_days", 181),
    ("cover_days", 0), ("safety_days", -1), ("safety_days", float("nan")),
    ("moq", float("inf")), ("lead_days", True)])
def test_invalid_policy_rejected(atlas, policy, key, value, dataset):
    policy[key] = value
    result = plan_item(atlas, dataset["as_of"], policy)
    assert result["status"] == "blocked" and result["policy"][key] is None


@pytest.mark.parametrize("key", ["use_reported_stock", "regular_only"])
def test_explicit_scenario_acceptance_required(atlas, policy, key, dataset):
    del policy[key]
    assert plan_item(atlas, dataset["as_of"], policy)["status"] == "blocked"


@pytest.mark.parametrize("field", ["free", "pack_multiple", "unit"])
def test_missing_stock_pack_unit_not_invented(atlas, policy, field, dataset):
    item = deepcopy(atlas)
    if field == "free":
        item["stock"]["free"] = None
    else:
        item[field] = None
    result = plan_item(item, dataset["as_of"], policy)
    assert result["status"] == "blocked" and result["order"]["quantity"] is None


def test_reserved_already_deducted_once(dataset, policy):
    for item in dataset["items"]:
        if item["supplier"] != "SystemElectric" or not item["stock"].get("reserved"):
            continue
        result = plan_item(item, dataset["as_of"], policy)
        if result["status"] == "provisional":
            first = result["calendar"][0]
            assert first["without_order"] == pytest.approx(item["stock"]["free"] + first["inbound"] - first["demand"])
            assert item["stock"]["free"] == item["stock"]["reported"] - item["stock"]["reserved"]
            return
    pytest.fail("No source-derived eligible item with reserve found.")


def test_existing_inbound_delay_preserves_raw_quantity_stock_and_provenance(dataset, policy):
    policy.update(lead_days=0, cover_days=3)
    for item in dataset["items"]:
        if item["supplier"] != "SystemElectric" or not any((row["quantity"] or 0) > 0 for row in item["inbound"]):
            continue
        original = deepcopy(item)
        base = plan_item(item, dataset["as_of"], policy, {"0": "2026-09-24"})
        if base["status"] != "provisional":
            continue
        delayed = plan_item(item, dataset["as_of"], policy, {"0": "2026-09-29"})
        assert plan_item(item, dataset["as_of"], policy)["status"] == "blocked"  # source year unknown
        assert delayed["status"] == "provisional" and item == original
        quantity = item["inbound"][0]["quantity"]
        assert base["incoming"][0]["quantity"] == delayed["incoming"][0]["quantity"] == quantity
        assert base["incoming"][0]["original_eta"] is None
        assert base["incoming"][0]["scenario_eta"] == "2026-09-24"
        assert delayed["incoming"][0]["scenario_eta"] == "2026-09-29"
        assert sum(row["inbound"] for row in base["calendar"]) == quantity
        assert sum(row["inbound"] for row in delayed["calendar"]) == 0
        assert base["calendar"][-1]["without_order"] - delayed["calendar"][-1]["without_order"] == pytest.approx(quantity)
        assert delayed["order"]["quantity"] >= base["order"]["quantity"]
        assert delayed["item"]["stock"] == item["stock"]
        return
    pytest.fail("No eligible supplied positive inbound row found.")


def test_early_shortage_is_not_retroactively_fixed(atlas, dataset, policy):
    policy.update(lead_days=180, cover_days=1)
    result = plan_item(atlas, dataset["as_of"], policy)
    assert result["status"] == "provisional" and result["pre_arrival_risk"]
    assert result["first_deficit_date"] < result["order"]["arrival"]
    assert all(row["with_order"] == row["without_order"] for row in result["calendar"][:-1])
    assert result["calendar"][-1]["with_order"] >= -1e-8
    assert result["item"]["stock"]["free"] == atlas["stock"]["free"]


def test_zero_order_and_exact_day_zero_horizon(atlas, dataset, policy):
    policy.update(lead_days=0, cover_days=1, safety_days=0, moq=atlas["pack_multiple"])
    result = plan_item(atlas, dataset["as_of"], policy)
    assert result["status"] == "provisional"
    assert result["order"]["quantity"] == result["order"]["raw_quantity"] == 0
    assert result["order"]["latest_order_date"] is None
    assert len(result["calendar"]) == 1
    row = result["calendar"][0]
    assert row["date"] == dataset["as_of"] and row["demand"] == 0
    assert row["without_order"] == atlas["stock"]["free"]


def test_auto_selects_minimum_common_mae_without_accuracy_claim(atlas, dataset, policy):
    policy["method"] = "auto"
    result = plan_item(atlas, dataset["as_of"], policy)
    metrics = result["metrics"]
    assert metrics["tested"] >= 3
    expected = "seasonal_growth" if metrics["seasonal_mae"] < metrics["mean_mae"] else "mean_12"
    assert result["forecast"]["method"] == expected
    assert result["forecast"]["selection_warning"]


def test_partial_month_remains_excluded_even_after_calendar_month_ends(atlas, policy):
    result = plan_item(atlas, "2026-10-01", policy)
    assert result["status"] == "blocked"
    assert any("2026-09" in reason for reason in result["reasons"])


def test_cannot_create_inbound_through_override(atlas, dataset, policy):
    result = plan_item(atlas, dataset["as_of"], policy, {"99": "2026-09-24"})
    assert result["status"] == "blocked" and result["order"]["quantity"] is None


def test_auto_fallback_does_not_claim_validated_accuracy(atlas, dataset, policy):
    item = deepcopy(atlas)
    for month in ("2025-01", "2025-02", "2025-03"):
        del item["history"][month]
    policy["method"] = "auto"
    result = plan_item(item, dataset["as_of"], policy)
    assert result["status"] == "provisional"
    assert result["metrics"]["tested"] < 3
    assert result["forecast"]["method"] == "mean_12"
    assert "fallback" in result["forecast"]["selection_reason"]


def test_reported_stock_cannot_leak_into_past(atlas, policy):
    result = plan_item(atlas, "2026-09-21", policy)
    assert result["status"] == "blocked"
    assert any("будущего" in reason for reason in result["reasons"])


def test_inbound_unknown_quantity_and_past_due_date_block(dataset, policy):
    item = next(i for i in dataset["items"] if i["supplier"] == "SystemElectric"
                and any((row["quantity"] or 0) > 0 for row in i["inbound"]))
    assert plan_item(item, dataset["as_of"], policy, {"0": dataset["as_of"]})["status"] == "blocked"
    edited = deepcopy(item)
    edited["inbound"][0]["quantity"] = None
    assert plan_item(edited, dataset["as_of"], policy, {"0": "2026-09-24"})["status"] == "blocked"
