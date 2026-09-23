"""API paths on the provided workbooks; policy values are explicit test settings."""

from copy import deepcopy
import csv
import io
import json
import os
from pathlib import Path

from fastapi.testclient import TestClient
import pytest

from smartbuyer import app as api


DATA_ROOT = Path(os.environ.get("HACKALEM_DATA_DIR", api.DEFAULT_DATA))
KEY = "SystemElectric::ATN544045"


@pytest.fixture(scope="module")
def client():
    if not DATA_ROOT.is_dir():
        pytest.skip("Provide issued XLSX through HACKALEM_DATA_DIR; no substitute data.")
    with TestClient(api.create_app(DATA_ROOT), base_url="http://localhost") as client:
        assert client.get("/api/bootstrap").status_code == 200
        yield client


@pytest.fixture
def policy():
    # Technical controls, NOT a claim about the company's lead time or stock policy.
    return dict(lead_days=2, cover_days=180, safety_days=7, moq=0,
                method="mean_12", use_reported_stock=True, regular_only=True)


def test_bootstrap_source_date_defaults_and_selected_forecast(client):
    body = client.get("/api/bootstrap").json()
    item = client.get("/api/item", params={"key": KEY}).json()
    row = next(row for row in body["items"] if row["key"] == KEY)
    assert body["as_of"] == item["metadata"]["as_of"] == "2026-09-22"
    assert body["target_month"] == "2026-10"
    assert len(body["sources"]) == 12 and len(body["items"]) == body["coverage"]["dataset_items"]
    assert row["forecast_units"] == item["report"]["forecasts"]["seasonal_growth"]["forecast_units"]
    assert row["stock"] == item["item"]["stock"]
    assert body["policy_defaults"] == api.Policy().model_dump()
    assert body["policy_defaults"]["lead_days"] is None
    assert body["policy_defaults"]["regular_only"] is False


def test_empty_policy_blocks_order_not_zero(client):
    response = client.post("/api/plan", json={"key": KEY})
    assert response.status_code == 200
    result = response.json()
    assert result["status"] == "blocked" and result["order"]["quantity"] is None
    assert result["reasons"] and len(result["result_id"]) == 64
    exported = client.post("/api/export", json={"result_ids": [result["result_id"]]})
    assert exported.status_code == 400 and exported.json()["error"]["code"] == "nothing_to_export"


def test_demand_adjustment_and_demo_provenance_reach_server_csv(client, policy):
    item_before = client.get("/api/item", params={"key": KEY}).json()
    assert "transactions_monthly" not in item_before["item"]
    adjustment = item_before["report"]["demand_adjustment"]
    assert adjustment["monthly"] and adjustment["source_refs"]
    for point in adjustment["monthly"]:
        assert point["raw"] == item_before["item"]["history"][point["month"]]
    manual = client.post("/api/plan", json={"key": KEY, "policy": policy}).json()
    policy["scenario_mode"] = "demonstration"
    demo = client.post("/api/plan", json={"key": KEY, "policy": policy}).json()
    assert demo["demand_adjustment"] == adjustment
    assert demo["order"] == manual["order"]
    assert demo["result_id"] != manual["result_id"]
    assert demo["policy"]["scenario_mode"] == "demonstration"
    assert any("Демонстрационные настройки" in value for value in demo["assumptions"])
    exported = client.post("/api/export", json={"result_ids": [demo["result_id"]]})
    assert exported.status_code == 200
    row = next(csv.DictReader(io.StringIO(exported.text)))
    assert json.loads(row["policy"])["scenario_mode"] == "demonstration"
    assert json.loads(row["demand_adjustment"]) == adjustment
    assert "Демонстрационные настройки" in row["assumptions"]
    assert client.get("/api/item", params={"key": KEY}).json()["item"] == item_before["item"]


def test_invalid_scenario_mode_rejected(client, policy):
    policy["scenario_mode"] = "silent-default"
    assert client.post("/api/plan", json={"key": KEY, "policy": policy}).status_code == 400


def test_plan_identity_csv_exact_quantity_and_status(client, policy):
    request = {"key": KEY, "policy": policy}
    first = client.post("/api/plan", json=request).json()
    second = client.post("/api/plan", json=request).json()
    assert first == second and first["status"] == "provisional" and first["order"]["quantity"] > 0
    response = client.post("/api/export", json={"result_ids": [first["result_id"], first["result_id"]]})
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert response.status_code == 200 and len(rows) == 1
    row = rows[0]
    assert float(row["quantity"]) == first["order"]["quantity"]
    assert row["status"] == first["status"]
    assert row["stock_status"] == first["item"]["stock"]["status"]
    assert row["as_of"] == first["as_of"] and row["sources"] and row["assumptions"]


def test_export_does_not_accept_client_quantity(client, policy):
    plan = client.post("/api/plan", json={"key": KEY, "policy": policy}).json()
    response = client.post("/api/export", json={"result_ids": [plan["result_id"]], "quantity": 1})
    assert response.status_code == 400
    assert client.post("/api/export", json={"result_ids": ["0" * 64]}).status_code == 404


def test_export_rejects_two_scenarios_of_same_sku(client, policy):
    first = client.post("/api/plan", json={"key": KEY, "policy": policy}).json()
    second = client.post("/api/plan", json={"key": KEY, "policy": {**policy, "safety_days": 8}}).json()
    response = client.post("/api/export", json={"result_ids": [first["result_id"], second["result_id"]]})
    assert response.status_code == 400 and response.json()["error"]["code"] == "conflicting_plans"


def test_csv_formula_escape_is_applied_to_source_text(client, policy):
    plan = client.post("/api/plan", json={"key": KEY, "policy": policy}).json()
    original = client.app.state.results[plan["result_id"]]
    try:
        security_case = deepcopy(original)
        security_case["item"]["name"] = "=1+1"  # security string, not a new business record
        client.app.state.results[plan["result_id"]] = security_case
        response = client.post("/api/export", json={"result_ids": [plan["result_id"]]})
        row = next(csv.DictReader(io.StringIO(response.text)))
        assert row["name"] == "'=1+1"
        assert float(row["quantity"]) == plan["order"]["quantity"]
    finally:
        client.app.state.results[plan["result_id"]] = original


def test_forecast_export_matches_same_report_and_free_stock(client):
    detail = client.get("/api/item", params={"key": KEY}).json()
    response = client.get("/api/forecast-export", params={"key": KEY})
    row = next(csv.DictReader(io.StringIO(response.text)))
    assert float(row["seasonal_growth_units"]) == detail["report"]["forecasts"]["seasonal_growth"]["forecast_units"]
    stock = detail["item"]["stock"]
    assert float(row["free_stock"]) == stock["free"] == stock["reported"] - stock["reserved"]
    assert row["stock_status"] == stock["status"] and row["order_quantity"] == ""


@pytest.mark.parametrize("change", [{"lead_days": -1}, {"lead_days": True}, {"cover_days": 0},
                                    {"regular_only": "true"}, {"method": "invented"}, {"unknown": 1}])
def test_policy_validation_is_readable_without_traceback(client, policy, change):
    response = client.post("/api/plan", json={"key": KEY, "policy": {**policy, **change}})
    assert response.status_code == 400 and response.json()["error"]["message"]
    assert "Traceback" not in response.text and str(DATA_ROOT) not in response.text


def test_eta_validation_and_unknown_item(client):
    assert client.get("/api/item", params={"key": "not-present"}).status_code == 404
    assert client.post("/api/plan", json={"key": KEY, "eta_overrides": {"0": "2026-99-99"}}).status_code == 400
    result = client.post("/api/plan", json={"key": KEY, "eta_overrides": {"99": "2026-09-29"}}).json()
    assert result["status"] == "blocked" and result["order"]["quantity"] is None


def test_late_new_order_never_fixes_prior_shortage(client, policy):
    policy.update(lead_days=180, cover_days=1)
    result = client.post("/api/plan", json={"key": KEY, "policy": policy}).json()
    assert result["status"] == "provisional" and result["pre_arrival_risk"]
    assert result["first_deficit_date"] < result["order"]["arrival"]
    assert all(row["with_order"] == row["without_order"] for row in result["calendar"][:-1])
    assert result["item"]["stock"] == client.get("/api/item", params={"key": KEY}).json()["item"]["stock"]


def test_failed_reload_preserves_snapshot_and_results(client, monkeypatch):
    before = client.app.state.snapshot
    results = dict(client.app.state.results)
    def fail(_root):
        raise OSError("private-path-must-not-leak")
    with monkeypatch.context() as context:
        context.setattr(api, "load_dataset", fail)
        response = client.post("/api/reload")
    assert response.status_code == 503 and "private-path" not in response.text
    assert client.app.state.snapshot is before and client.app.state.results == results
    assert client.get("/api/item", params={"key": KEY}).status_code == 200


def test_successful_reload_invalidates_saved_results(client):
    old = client.post("/api/plan", json={"key": KEY}).json()["result_id"]
    assert client.post("/api/reload").status_code == 200
    assert client.post("/api/export", json={"result_ids": [old]}).status_code == 404


def test_no_arbitrary_http_files_or_external_origin(client):
    assert client.post("/api/plan", json={"key": KEY, "data_dir": "not-allowed"}).status_code == 400
    assert client.post("/api/reload", headers={"Origin": "https://unrelated.example"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "unrelated.example"}).status_code == 403
    assert client.get("/api/health").json()["local_only"] is True


def test_missing_configured_dataset_has_safe_error(tmp_path):
    with TestClient(api.create_app(tmp_path), base_url="http://localhost") as empty:
        response = empty.get("/api/bootstrap")
        assert response.status_code == 503 and response.json()["error"]["code"] == "data_unavailable"
        assert str(tmp_path) not in response.text and "Traceback" not in response.text


def test_hosts_default_to_local_and_origin_must_match(monkeypatch, tmp_path):
    monkeypatch.delenv("HACKALEM_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("HACKALEM_PUBLIC_ORIGIN", raising=False)
    with TestClient(api.create_app(tmp_path), base_url="http://localhost:8765") as local:
        assert local.get("/api/health").json()["local_only"] is True
        assert local.get("/api/health", headers={"Origin": "http://localhost:8765"}).status_code == 200
        for host in ("demo.example", "testserver", "evil.localhost"):
            assert local.get("/api/health", headers={"Host": host}).status_code == 403
        for origin in ("https://localhost:8765", "http://localhost:9999", "http://evil.example", "null", "http://["):
            assert local.post("/api/reload", headers={"Origin": origin}).status_code == 403


def test_explicit_domain_accepts_only_its_own_origin(monkeypatch, tmp_path):
    monkeypatch.setenv("HACKALEM_ALLOWED_HOSTS", "demo.example")
    monkeypatch.delenv("HACKALEM_PUBLIC_ORIGIN", raising=False)
    with TestClient(api.create_app(tmp_path), base_url="https://demo.example") as hosted:
        health = hosted.get("/api/health", headers={"Origin": "https://demo.example"})
        assert health.status_code == 200 and health.json()["local_only"] is False
        assert health.json()["dataset_loaded"] is False
        for host in ("other.example", "sub.demo.example", "demo.example.evil.example"):
            assert hosted.get("/api/health", headers={"Host": host}).status_code == 403
        for origin in ("http://demo.example", "https://other.example", "https://demo.example:444", "https://demo.example/path"):
            assert hosted.post("/api/reload", headers={"Origin": origin}).status_code == 403


def test_public_https_origin_survives_internal_http_proxy(monkeypatch, tmp_path):
    monkeypatch.setenv("HACKALEM_ALLOWED_HOSTS", "demo.example")
    monkeypatch.setenv("HACKALEM_PUBLIC_ORIGIN", "https://demo.example")
    with TestClient(api.create_app(tmp_path), base_url="http://demo.example") as proxied:
        response = proxied.get("/api/health", headers={"Origin": "https://demo.example"})
        assert response.status_code == 200 and response.json()["local_only"] is False
        assert proxied.get("/api/health").status_code == 200
        for origin in ("http://demo.example", "https://other.example", "https://demo.example:444", "null"):
            assert proxied.post("/api/reload", headers={
                "Origin": origin, "X-Forwarded-Proto": "https", "X-Forwarded-Host": "demo.example"
            }).status_code == 403
        assert proxied.get("/api/health", headers={"Host": "other.example", "Origin": "https://demo.example"}).status_code == 403


@pytest.mark.parametrize("origin", ["https://unlisted.example", "http://demo.example", "https://demo.example/path",
                                    "https://user:password@demo.example", "https://demo.example?query=1",
                                    "https://demo.example#fragment", "https://demo.example:70000", "https://demo.example\n"])
def test_public_origin_configuration_is_strict(monkeypatch, tmp_path, origin):
    monkeypatch.setenv("HACKALEM_ALLOWED_HOSTS", "demo.example")
    monkeypatch.setenv("HACKALEM_PUBLIC_ORIGIN", origin)
    with pytest.raises(ValueError, match="HACKALEM_PUBLIC_ORIGIN"):
        api.create_app(tmp_path)


@pytest.mark.parametrize("hosts", ["*", "*.example", "https://demo.example", "demo.example:443", "demo.example,", "0.0.0.0"])
def test_host_allowlist_rejects_wildcards_and_urls(monkeypatch, hosts, tmp_path):
    monkeypatch.setenv("HACKALEM_ALLOWED_HOSTS", hosts)
    with pytest.raises(ValueError, match="HACKALEM_ALLOWED_HOSTS"):
        api.create_app(tmp_path)


def test_portable_launcher_requires_explicit_network_opt_in(monkeypatch):
    from smartbuyer import serve
    monkeypatch.delenv("HACKALEM_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("HACKALEM_BIND_HOST", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    calls = []
    monkeypatch.setattr(serve.uvicorn, "run", lambda *args, **kwargs: calls.append((args, kwargs)))
    serve.main()
    assert calls[-1] == (("smartbuyer.app:app",), {"host": "127.0.0.1", "port": 8765, "workers": 1})
    monkeypatch.setenv("HACKALEM_BIND_HOST", "0.0.0.0")
    with pytest.raises(SystemExit, match="HACKALEM_ALLOWED_HOSTS"):
        serve.main()
    assert len(calls) == 1
    monkeypatch.setenv("HACKALEM_ALLOWED_HOSTS", "demo.example")
    monkeypatch.setenv("PORT", "8080")
    serve.main()
    assert calls[-1][1] == {"host": "0.0.0.0", "port": 8080, "workers": 1}
    monkeypatch.setenv("PORT", "70000")
    with pytest.raises(SystemExit, match="PORT"):
        serve.main()
