"""Same-origin demo with explicit host opt-in; issued sources, no external orders."""

import csv
from datetime import date
import hashlib
import io
import json
import os
from pathlib import Path
import re
from threading import RLock
from typing import Annotated, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, field_validator

from .cli import _csv_cell, build_report, render_report
from .data import load_dataset


DEFAULT_DATA = Path.home() / "OneDrive/Desktop/HackAlem_Logistics_2026-09-23/04_Excel"
WEB = Path(__file__).resolve().parents[1] / "web"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}


def allowed_hosts():
    """Exact hostnames only; absent configuration keeps the app local."""
    value = os.environ.get("HACKALEM_ALLOWED_HOSTS", "")
    configured = {host.strip().lower() for host in value.split(",")} if value.strip() else set()
    for host in configured:
        if host in LOCAL_HOSTS:
            continue
        if host == "0.0.0.0" or len(host) > 253 or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        ):
            raise ValueError("HACKALEM_ALLOWED_HOSTS: нужны точные имена хостов через запятую, без URL, порта и wildcard.")
    return LOCAL_HOSTS | configured


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    lead_days: Annotated[int, Field(strict=True, ge=0, le=180)] | None = None
    cover_days: Annotated[int, Field(strict=True, ge=1, le=180)] | None = None
    safety_days: Annotated[float, Field(strict=True, ge=0, le=3650)] | None = None
    moq: Annotated[float, Field(strict=True, ge=0, le=1e12)] | None = None
    method: Literal["auto", "mean_12", "seasonal_growth"] = "auto"
    use_reported_stock: Annotated[bool, Field(strict=True)] = False
    regular_only: Annotated[bool, Field(strict=True)] = False


class PlanRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str = Field(min_length=1, max_length=300)
    policy: Policy = Field(default_factory=Policy)
    eta_overrides: dict[str, str] = Field(default_factory=dict, max_length=100)

    @field_validator("eta_overrides")
    @classmethod
    def check_eta(cls, values):
        for index, value in values.items():
            if not index.isascii() or not index.isdigit() or str(int(index)) != index:
                raise ValueError("Нужен индекс существующей поставки.")
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("Дата должна иметь формат YYYY-MM-DD.")
        return values


class ExportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    result_ids: list[Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]] = Field(min_length=1, max_length=5000)


def _error(status, code, message):
    raise HTTPException(status, {"code": code, "message": message})


def _key(item):
    return item["supplier"] + "::" + item["sku"]


def _csv_response(content, filename):
    return Response(content, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


def create_app(data_root=None):
    app = FastAPI(title="QORAI — управление запасами", docs_url=None, redoc_url=None)
    allowed = allowed_hosts()
    public_origin = os.environ.get("HACKALEM_PUBLIC_ORIGIN") or None
    if public_origin:
        try:
            public = urlsplit(public_origin)
            if (public.scheme != "https" or public.hostname not in allowed
                    or public.username is not None or public.password is not None
                    or public.path or public.query or public.fragment
                    or public_origin != public.geturl() or public.netloc.endswith(":")
                    or any(char.isspace() for char in public_origin)
                    or (public.port is not None and not 1 <= public.port <= 65535)):
                raise ValueError
        except ValueError:
            raise ValueError("HACKALEM_PUBLIC_ORIGIN: нужен точный HTTPS origin разрешённого хоста без пути, credentials, query или fragment.") from None
    # ponytail: one local process and one lock; move state out only for multi-user deployment.
    lock = RLock()
    root = Path(data_root) if data_root is not None else Path(os.environ.get("HACKALEM_DATA_DIR", DEFAULT_DATA))
    app.state.snapshot = None
    app.state.results = {}

    @app.middleware("http")
    async def local_origin(request: Request, call_next):
        try:
            host = request.url.hostname
            origin = request.headers.get("origin")
            expected_origin = public_origin or f"{request.url.scheme}://{request.url.netloc}"
            same_origin = not origin or origin == expected_origin
        except ValueError:
            host, same_origin = None, False
        if host not in allowed or not same_origin:
            return JSONResponse({"error": {"code": "host_or_origin_rejected", "message": "Хост или источник запроса не разрешён настройками приложения."}}, status_code=403)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(HTTPException)
    async def http_error(_request, exc):
        detail = exc.detail if isinstance(exc.detail, dict) else {"code": "request_error", "message": "Запрос не выполнен."}
        return JSONResponse({"error": detail}, status_code=exc.status_code)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request, _exc):
        return JSONResponse({"error": {"code": "invalid_input", "message": "Проверьте поля запроса: типы, даты, диапазоны и обязательные значения."}}, status_code=400)

    @app.exception_handler(Exception)
    async def unexpected_error(_request, _exc):
        return JSONResponse({"error": {"code": "internal_error", "message": "Расчёт не выполнен. Проверьте данные и повторите запрос."}}, status_code=500)

    def read_snapshot():
        try:
            dataset = load_dataset(root)
            cutoff = date.fromisoformat(dataset["as_of"])
            target = f"{cutoff.year + (cutoff.month == 12):04d}-{cutoff.month % 12 + 1:02d}"
            report = build_report(dataset, target)
            items = {_key(item): item for item in dataset["items"]}
            rows = {_key(row): row for row in report["rows"]}
            if len(items) != len(dataset["items"]):
                raise ValueError("Duplicate source keys")
            return {"dataset": dataset, "report": report, "items": items, "rows": rows}
        except Exception:
            _error(503, "data_unavailable", "Не удалось прочитать выданные Excel. Проверьте папку HACKALEM_DATA_DIR и целостность 12 файлов; последний исправный снимок сохранён.")

    def snapshot():
        with lock:
            if app.state.snapshot is None:
                app.state.snapshot = read_snapshot()
            return app.state.snapshot

    def selected(snap, key):
        if key not in snap["items"]:
            _error(404, "item_not_found", "Товар не найден в выданных источниках.")
        return snap["items"][key], snap["rows"][key]

    def bootstrap_payload(snap):
        dataset, report = snap["dataset"], snap["report"]
        items = []
        for key, item in snap["items"].items():
            row = snap["rows"][key]
            metrics = row["backtest"]["metrics"]
            items.append({"key": key, **{field: item.get(field) for field in ("sku", "supplier", "name", "unit", "stock", "pack_multiple", "moq")},
                          "forecast_units": row["forecasts"]["seasonal_growth"]["forecast_units"],
                          "mean_units": row["forecasts"]["mean_12"]["forecast_units"],
                          "forecast_status": row["forecast_status"], "backtest_tested": row["backtest"]["tested"],
                          "seasonal_mae": metrics["seasonal_growth"]["mae_units"], "mean_mae": metrics["mean_12"]["mae_units"]})
        return {"as_of": dataset["as_of"], "target_month": report["target_month"], "coverage": report["coverage"],
                "suppliers": sorted({item["supplier"] for item in dataset["items"]}), "items": items,
                "warnings": dataset["warnings"], "sources": dataset["sources"], "policy_defaults": Policy().model_dump()}

    @app.get("/api/health")
    def health():
        return {"status": "ok", "dataset_loaded": app.state.snapshot is not None,
                "local_only": allowed <= LOCAL_HOSTS}

    @app.get("/api/bootstrap")
    def bootstrap():
        return bootstrap_payload(snapshot())

    @app.get("/api/item")
    def item(key: str):
        snap = snapshot()
        source_item, row = selected(snap, key)
        return {"item": source_item, "report": row, "metadata": {"as_of": snap["dataset"]["as_of"],
                "target_month": snap["report"]["target_month"], "warnings": snap["dataset"]["warnings"]}}

    @app.post("/api/plan")
    def plan(body: PlanRequest):
        with lock:
            snap = snapshot()
            source_item, _row = selected(snap, body.key)
            try:
                from .replenishment import plan_item
            except ImportError:
                _error(503, "core_unavailable", "Ядро закупочного сценария ещё не подключено.")
            result = plan_item(source_item, snap["dataset"]["as_of"], body.policy.model_dump(), body.eta_overrides)
            result["as_of"] = snap["dataset"]["as_of"]
            identity = {"sources": snap["dataset"]["sources"], "request": body.model_dump(), "result": result}
            result_id = hashlib.sha256(json.dumps(identity, ensure_ascii=False, sort_keys=True, allow_nan=False).encode()).hexdigest()
            result["result_id"] = result_id
            app.state.results[result_id] = result
            return result

    @app.post("/api/export")
    def export(body: ExportRequest):
        with lock:
            if any(result_id not in app.state.results for result_id in body.result_ids):
                _error(404, "result_not_found", "Расчёт не найден или устарел после перечитывания Excel. Выполните расчёт снова.")
            results = [app.state.results[key] for key in dict.fromkeys(body.result_ids)]
            if len({_key(result["item"]) for result in results}) != len(results):
                _error(400, "conflicting_plans", "Выбрано несколько сценариев одного товара. Оставьте один сценарий на артикул поставщика, чтобы не удвоить закупку.")
            results = [result for result in results if result["status"] == "provisional" and result["order"]["quantity"] is not None and result["order"]["quantity"] > 0]
            if not results:
                _error(400, "nothing_to_export", "Нет положительных неблокированных заказов для экспорта. Прогноз можно экспортировать отдельно.")
            fields = ["supplier", "sku", "name", "unit", "quantity", "status", "stock_status", "as_of", "planned_order_date", "arrival", "method", "policy", "assumptions", "reasons", "sources", "result_id"]
            output = io.StringIO(newline="")
            writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
            writer.writeheader()
            for result in sorted(results, key=lambda value: (value["item"]["supplier"], value["item"]["sku"])):
                row = {**{field: result["item"].get(field) for field in ("supplier", "sku", "name", "unit")},
                       "quantity": result["order"]["quantity"], "status": result["status"],
                       "stock_status": result["item"]["stock"].get("status"), "as_of": result["as_of"],
                       "planned_order_date": result["order"].get("planned_order_date"),
                       "arrival": result["order"]["arrival"], "method": result["forecast"]["method"], "result_id": result["result_id"]}
                for field in ("policy", "assumptions", "reasons", "sources"):
                    row[field] = json.dumps(result[field], ensure_ascii=False, allow_nan=False)
                writer.writerow({field: _csv_cell(row[field]) for field in fields})
            return _csv_response(output.getvalue(), "conditional-orders.csv")

    @app.get("/api/forecast-export")
    def forecast_export(key: str | None = None):
        snap = snapshot()
        report = snap["report"]
        if key is not None:
            _source, row = selected(snap, key)
            report = {**report, "rows": [row]}
        return _csv_response(render_report(report, "csv"), "sales-forecast.csv")

    @app.post("/api/reload")
    def reload():
        with lock:
            fresh = read_snapshot()
            app.state.snapshot = fresh
            app.state.results.clear()
            return bootstrap_payload(fresh)

    @app.get("/")
    def index():
        if not (WEB / "index.html").is_file():
            _error(503, "ui_unavailable", "Интерфейс ещё собирается; API и CLI доступны отдельно.")
        return FileResponse(WEB / "index.html")

    app.mount("/static", StaticFiles(directory=WEB, check_dir=False), name="static")
    return app


app = create_app()
