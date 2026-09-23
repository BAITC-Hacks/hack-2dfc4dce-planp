"""Integration checks use only supplied workbooks, never invented business rows."""
import os
from pathlib import Path

from openpyxl import load_workbook
import pytest

from smartbuyer.data import FILES, load_dataset, _aggregate_transactions
from datetime import datetime
from collections import defaultdict
from hashlib import sha256


DATA_ROOT = Path(os.environ.get("HACKALEM_DATA_DIR", os.environ.get("HACKALEM_DATA_ROOT", str(
    Path.home() / "OneDrive/Desktop/HackAlem_Logistics_2026-09-23/04_Excel"))))


@pytest.fixture(scope="module")
def dataset():
    if not DATA_ROOT.is_dir():
        pytest.skip("Set HACKALEM_DATA_DIR to the supplied 04_Excel directory.")
    return load_dataset(DATA_ROOT)


def rows(supplier, role, sheet):
    book = load_workbook(DATA_ROOT / FILES[supplier][role], read_only=True, data_only=True, keep_links=False)
    try:
        return list(book[sheet].iter_rows(values_only=True))
    finally:
        book.close()


def test_unique_keys_and_all_provided_files(dataset):
    keys = [(i["supplier"], i["sku"]) for i in dataset["items"]]
    assert len(keys) == len(set(keys))
    assert len(dataset["sources"]) == 12
    assert all(len(s["sha256"]) == 64 for s in dataset["sources"])
    assert dataset["partial_months"] == ["2026-09"]
    assert dataset["as_of"] == "2026-09-22"
    assert "filename" in dataset["as_of_basis"]
    assert all(sha256((DATA_ROOT / s["file"]).read_bytes()).hexdigest() == s["sha256"] for s in dataset["sources"])


@pytest.mark.parametrize("supplier,key_column,start_column", [("SystemElectric", 2, 4), ("IEK", 1, 2)])
def test_monthly_rows_and_quantities_match_independent_read(dataset, supplier, key_column, start_column):
    original = rows(supplier, "sales", "Лист_1")
    expected = {str(r[key_column]).strip(): r for r in original[2:] if r[key_column] is not None}
    actual = {i["sku"]: i for i in dataset["items"] if i["supplier"] == supplier}
    assert set(expected) <= set(actual)
    for sku, row in expected.items():
        item = actual[sku]
        assert len(item["history"]) == 33
        assert list(item["history"])[0] == "2024-01"
        assert list(item["history"])[-1] == "2026-09"
        assert item["partial_months"] == ["2026-09"]
        assert list(item["history"].values()) == list(row[start_column:start_column + 33])
    assert any(v is None for item in actual.values() for v in item["history"].values())
    assert any(v is not None and v < 0 for item in actual.values() for v in item["history"].values())


def test_snapshot_join_reserve_once_and_no_guessed_eta(dataset):
    original = rows("SystemElectric", "snapshot", "TDSheet")
    actual = {i["sku"]: i for i in dataset["items"] if i["supplier"] == "SystemElectric"}
    for row in original[2:]:
        item = actual[str(row[1]).strip()]
        assert item["stock"]["reported"] == row[49]
        assert item["stock"]["reserved"] == row[50]
        assert item["stock"]["free"] == row[51]
        assert item["stock"]["free"] == item["stock"]["reported"] - item["stock"]["reserved"]
        assert item["stock"]["status"] == "reported_not_confirmed"
        assert item["inbound"][0]["quantity"] == row[54]
        assert item["inbound"][0]["eta"] is None
    assert sum(i["stock"]["status"] == "reported_not_confirmed" for i in actual.values()) == len(original) - 2


def test_pack_by_article_and_missing_not_one(dataset):
    original = rows("SystemElectric", "pack", "Лист_1")
    expected = {str(r[3]).strip(): r[4] for r in original[2:] if r[3] is not None}
    actual = {i["sku"]: i for i in dataset["items"] if i["supplier"] == "SystemElectric"}
    assert all(i["pack_multiple"] == expected.get(sku) for sku, i in actual.items())
    assert any(i["pack_multiple"] is None and i["stock"]["reported"] is not None for i in actual.values())
    assert all(i["moq"] is None for i in actual.values())


def test_iek_month_start_stock_not_current(dataset):
    items = [i for i in dataset["items"] if i["supplier"] == "IEK"]
    assert items
    assert all(i["stock"]["reported"] is None and i["stock"]["free"] is None for i in items)
    assert all(i["pack_multiple"] is None for i in items)
    assert any(i["inbound"] for i in items)
    assert all(p["status"] == "reported_deadline_not_confirmed" for i in items for p in i["inbound"])


@pytest.mark.parametrize("supplier,unit_column", [("SystemElectric", 3), ("IEK", 1)])
def test_units_join_by_internal_code_and_reference(dataset, supplier, unit_column):
    original = rows(supplier, "stock_history", "Лист_1")
    expected = {str(row[2]).strip(): (number, row[unit_column])
                for number, row in enumerate(original[2:], 3) if row[2] is not None}
    matched = 0
    for item in (i for i in dataset["items"] if i["supplier"] == supplier):
        source = expected.get(item["internal_code"])
        if source is None:
            assert item["unit"] is None
            continue
        matched += 1
        number, value = source
        assert item["unit"] == (str(value).strip() or None if value is not None else None)
        assert {"file": FILES[supplier]["stock_history"], "sheet": "Лист_1",
                "range": f"{'D' if unit_column == 3 else 'B'}{number}", "field": "unit"} in item["source_refs"]
    assert matched > 0


def test_missing_root_has_clear_error(tmp_path):
    with pytest.raises(ValueError, match="Не найден исходный XLSX"):
        load_dataset(tmp_path)


@pytest.mark.parametrize("supplier,start_column", [("SystemElectric", 4), ("IEK", 3)])
def test_stock_history_is_exact_month_start_source(dataset, supplier, start_column):
    original = rows(supplier, "stock_history", "Лист_1")
    expected = {str(r[2]).strip(): r for r in original[2:] if r[2] is not None}
    for item in (i for i in dataset["items"] if i["supplier"] == supplier):
        source = expected.get(item["internal_code"])
        if source is None:
            assert item["stock_history"] == {}
            continue
        assert list(item["stock_history"].values()) == list(source[start_column:start_column + 33])
        assert item["stock_history_metadata"] == {"timing": "month_start", "daily_availability": False}
        assert any(r["field"] == "stock_history" for r in item["source_refs"])


@pytest.fixture(scope="module")
def issued_operations(dataset):
    """A few actual rows and an independent one-SKU aggregate; no made-up sales."""
    item = next(i for i in dataset["items"] if i["supplier"] == "SystemElectric" and i["sku"] == "ATN544045")
    expected = defaultdict(lambda: defaultdict(float))
    samples = {}
    book = load_workbook(DATA_ROOT / FILES["SystemElectric"]["transactions"], read_only=True, data_only=True)
    try:
        for row in book["Лист_1"].iter_rows(min_row=2, values_only=True):
            doc, qty = str(row[2] or ""), row[7]
            if not doc.startswith("Расходная накладная "):
                samples.setdefault("non_sales", row)
                continue
            if isinstance(qty, (int, float)) and qty < 0:
                samples.setdefault("negative", row)
            if str(row[3]).strip() != item["internal_code"] or not isinstance(qty, (int, float)) or qty <= 0:
                continue
            stamp = datetime.strptime(row[0], "%d.%m.%Y %H:%M:%S")
            if stamp.date().isoformat() > dataset["as_of"]:
                continue
            assert str(row[5]).strip() == item["unit"]
            samples.setdefault("sale", row)
            if row[:3] != samples["sale"][:3]:
                samples.setdefault("second_sale", row)
            expected[stamp.strftime("%Y-%m")][(stamp, str(row[1]).strip(), doc.strip())] += qty
    finally:
        book.close()
    return item, samples, expected


def test_transactions_match_independent_source_and_keep_history(dataset, issued_operations):
    item, samples, expected = issued_operations
    assert samples.keys() >= {"sale", "negative", "non_sales"}
    assert set(item["transactions_monthly"]) == set(expected)
    for month, documents in expected.items():
        actual = item["transactions_monthly"][month]
        assert actual["document_count"] == len(documents)
        assert actual["positive_total"] == sum(documents.values())
        assert sorted(actual["document_quantities"]) == sorted(documents.values())
        assert set(actual) == {"document_quantities", "positive_total", "document_count"}
    sources = [s for s in dataset["sources"] if s["role"] == "transactions"]
    assert len(sources) == 2 and all(s["status"] == "read_cached_values" for s in sources)
    assert all(s["parse_summary"]["accepted_rows"] > 0 for s in sources)
    assert len(dataset["sources"]) == 12
    assert all(s["status"] == "not_parsed" for s in dataset["sources"] if s["role"] == "seasonality")
    assert any(not i["transactions_monthly"] for i in dataset["items"])


def test_document_lines_are_aggregated_once_and_non_sales_excluded(dataset, issued_operations):
    item, samples, _ = issued_operations
    sale = samples["sale"]
    # Repeating a supplied line tests the grouping contract, not a new business fixture.
    aggregate, _, stats = _aggregate_transactions(iter([sale, sale, samples["negative"], samples["non_sales"]]), [item], dataset["as_of"])
    month = datetime.strptime(sale[0], "%d.%m.%Y %H:%M:%S").strftime("%Y-%m")
    actual = aggregate[item["internal_code"]][month]
    assert actual == {"document_quantities": [2 * sale[7]], "positive_total": 2 * sale[7], "document_count": 1}
    assert stats["non_positive_or_invalid_rows"] == 1
    assert stats["non_sales_rows"] == 1


def test_missing_keys_units_and_ambiguous_join_are_not_zero_sales(dataset, issued_operations):
    item, samples, _ = issued_operations
    sale = samples["sale"]
    # Structural damage to an actual row checks rejection, without generating sales.
    missing = list(sale)
    missing[1] = None
    unknown = list(sale)
    unknown[3] = None
    invalid_unit = list(sale)
    invalid_unit[5] = None
    aggregate, _, stats = _aggregate_transactions(iter([missing, unknown, invalid_unit]), [item], dataset["as_of"])
    assert aggregate == {}
    assert stats["missing_document_key_rows"] == 2
    assert stats["unmatched_unit_rows"] == 1
    aggregate, _, stats = _aggregate_transactions(iter([sale]), [item, item], dataset["as_of"])
    assert aggregate == {} and stats["unmatched_or_ambiguous_code_rows"] == 1


def test_document_key_excludes_warehouse_and_future_is_rejected(dataset, issued_operations):
    item, samples, _ = issued_operations
    sale, second = samples["sale"], samples["second_sale"]
    other_warehouse = list(sale)
    other_warehouse[6] = None  # Warehouse is not part of the requested document identity.
    future = list(sale)
    future[0] = "23.09.2026 00:00:00"  # Boundary validation, not an imported business event.
    aggregate, _, stats = _aggregate_transactions(iter([sale, other_warehouse, second, future]), [item], dataset["as_of"])
    assert sum(m["document_count"] for m in aggregate[item["internal_code"]].values()) == 2
    assert sum(m["positive_total"] for m in aggregate[item["internal_code"]].values()) == 2 * sale[7] + second[7]
    assert stats["after_cutoff_rows"] == 1
