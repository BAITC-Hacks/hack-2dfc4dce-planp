"""Read-only command line report on issued workbooks; output goes to stdout only."""

import argparse
from collections import Counter
import csv
from datetime import date
import io
import json
from pathlib import Path
import re
import sys
from zipfile import BadZipFile

from smartbuyer.forecast import forecast_month, rolling_backtest


def _month(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        raise ValueError("Целевой месяц должен иметь формат YYYY-MM.")
    return date.fromisoformat(value + "-01")


def _cutoff(dataset, override):
    source_date = date.fromisoformat(dataset["as_of"])
    cutoff = date.fromisoformat(override) if override else source_date
    if cutoff > source_date:
        raise ValueError("Дата расчёта не может быть позже даты выданного набора.")
    return cutoff.isoformat()


def _number_text(value):
    return "нет данных" if value is None else f"{value:.2f}".replace(".", ",")


def build_report(dataset, target, sku=None, as_of=None):
    """One result object powers both formats; no ordering or inventory simulation."""
    cutoff = _cutoff(dataset, as_of)
    if _month(target) < date.fromisoformat(cutoff).replace(day=1):
        raise ValueError("Целевой месяц раньше месяца расчёта; используйте историческую проверку.")
    selected = [item for item in dataset["items"] if sku is None or item["sku"] == sku]
    if not selected:
        raise ValueError("По выбранному артикулу нет данных в выданном наборе.")
    rows = []
    for item in selected:
        partial = set(item.get("partial_months", []))
        # Partial source months cannot become training or targets, even at an earlier cutoff.
        history = {month: quantity for month, quantity in item["history"].items() if month not in partial}
        methods = {method: forecast_month(history, target, cutoff, method)
                   for method in ("seasonal_growth", "mean_12")}
        months = sorted(month for month in history if month < cutoff[:7])[-6:]
        backtest = rolling_backtest(history, months)
        reasons = [
            "Не подтверждены полнота и состав открытых клиентских обязательств.",
            "Не задан полный срок новой поставки L.",
            "Не задан горизонт покрытия H.",
            "Не задана страховая политика по категориям.",
        ]
        if not item.get("unit"):
            reasons.append("Не подтверждена единица измерения.")
        if item.get("moq") is None:
            reasons.append("Не известен MOQ или подтверждённое отсутствие ограничения.")
        if item.get("pack_multiple") is None:
            reasons.append("Не известна упаковочная кратность.")
        seasonal = methods["seasonal_growth"]
        status = "provisional" if seasonal["status"] == "ok" else "blocked"
        basis = f"{int(target[:4]) - 1:04d}-{target[5:]}"
        explanation = (
            f"Прогноз наблюдаемых продаж на {target}; дата расчёта {cutoff}. "
            f"База {basis}: {_number_text(history.get(basis))}; множитель динамики: "
            f"{_number_text(seasonal['trend_multiplier'])}. Средний метод учитывает число дней "
            "в каждом из 12 полных месяцев. Источник не признан окончательным эталоном; "
            "аномалии и отсутствие товара не исправлены автоматически. Это не объём закупки."
        )
        rows.append({
            "sku": item["sku"], "supplier": item.get("supplier"), "name": item.get("name"),
            "unit": item.get("unit"), "category": item.get("category"),
            "source_refs": item.get("source_refs", []), "stock": item.get("stock"),
            "pack_multiple": item.get("pack_multiple"), "moq": item.get("moq"),
            "inbound": item.get("inbound"), "partial_months": sorted(partial),
            "calculation_status": seasonal["status"], "forecast_status": status,
            "mean_calculation_status": methods["mean_12"]["status"],
            "mean_forecast_status": "provisional" if methods["mean_12"]["status"] == "ok" else "blocked",
            "forecasts": methods, "backtest_months": months, "backtest": backtest,
            "order_status": "blocked", "order_quantity": None, "order_reasons": reasons,
            "warnings": item.get("warnings", []), "explanation": explanation,
        })
    return {
        "schema_version": 1, "dataset_as_of": dataset["as_of"], "as_of": cutoff,
        "dataset_as_of_basis": dataset.get("as_of_basis"),
        "target_month": target, "sources": dataset.get("sources", []),
        "warnings": dataset.get("warnings", []), "rows": rows,
        "coverage": {
            "dataset_items": len(dataset["items"]), "selected_items": len(rows),
            "forecast_status_counts": dict(Counter(row["forecast_status"] for row in rows)),
            "backtest_tested_points": sum(row["backtest"]["tested"] for row in rows),
            "order_blocked_items": len(rows),
        },
        "limitations": [
            "Использованы только выданные источники; их происхождение не объявляется реальным или синтетическим.",
            "Месячная база — рабочее допущение; она не смешивается с операциями.",
            "MAE относится к наблюдаемым продажам отдельного SKU, не к истинному спросу или экономии.",
            "Не рассчитаны твёрдые даты дефицита и количество закупки; неизвестные входы не заменены нулями.",
        ],
    }


def _csv_cell(value):
    """Prevent source text from becoming a spreadsheet formula; numbers stay numeric."""
    if isinstance(value, str) and (value.lstrip().startswith(("=", "+", "-", "@"))
                                  or value.startswith(("\t", "\r", "\n"))):
        return "'" + value
    return value


def render_report(report, output_format="json"):
    if output_format == "json":
        return json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n"
    if output_format != "csv":
        raise ValueError("Формат должен быть json или csv.")
    fields = ["sku", "supplier", "name", "unit", "target_month", "as_of", "dataset_as_of",
              "seasonal_growth_units", "mean_12_units", "seasonal_mae_units", "mean_mae_units",
              "seasonal_bias_units", "mean_bias_units", "backtest_months",
              "backtest_tested", "calculation_status", "forecast_status", "mean_calculation_status",
              "mean_forecast_status", "order_status", "order_quantity", "order_reasons",
              "reported_stock", "reserved_stock", "free_stock", "stock_as_of", "stock_status", "source_refs",
              "sources", "warnings", "explanation"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields, lineterminator="\n")
    writer.writeheader()
    for row in report["rows"]:
        stock = row.get("stock") or {}
        metrics = row["backtest"]["metrics"]
        values = {field: row.get(field) for field in fields}
        values.update(
            target_month=report["target_month"], as_of=report["as_of"], dataset_as_of=report["dataset_as_of"],
            seasonal_growth_units=row["forecasts"]["seasonal_growth"]["forecast_units"],
            mean_12_units=row["forecasts"]["mean_12"]["forecast_units"],
            seasonal_mae_units=metrics["seasonal_growth"]["mae_units"],
            mean_mae_units=metrics["mean_12"]["mae_units"],
            seasonal_bias_units=metrics["seasonal_growth"]["bias_units"],
            mean_bias_units=metrics["mean_12"]["bias_units"],
            backtest_months=json.dumps(row["backtest_months"]), backtest_tested=row["backtest"]["tested"],
            order_reasons=json.dumps(row["order_reasons"], ensure_ascii=False),
            reported_stock=stock.get("reported"), reserved_stock=stock.get("reserved"),
            free_stock=stock.get("free"), stock_as_of=stock.get("as_of"), stock_status=stock.get("status"),
            source_refs=json.dumps(row["source_refs"], ensure_ascii=False),
            sources=json.dumps(report["sources"], ensure_ascii=False),
            warnings=json.dumps({"dataset": report["warnings"], "item": row["warnings"],
                                 "forecasts": {method: result["warnings"]
                                               for method, result in row["forecasts"].items()}}, ensure_ascii=False),
        )
        writer.writerow({key: _csv_cell(value) for key, value in values.items()})
    return output.getvalue()


def main(argv=None):
    parser = argparse.ArgumentParser(description="Прогноз по выданным XLSX; JSON/CSV только в stdout.")
    parser.add_argument("--data-dir", required=True, type=Path)
    parser.add_argument("--target", required=True, help="Целевой месяц YYYY-MM")
    parser.add_argument("--sku")
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    parser.add_argument("--as-of", help="Дата расчёта YYYY-MM-DD, не позже даты выданного набора")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        _month(args.target)
        if not args.data_dir.is_dir():
            raise ValueError("Папка выданных XLSX не найдена: проверьте --data-dir.")
        from smartbuyer.data import load_dataset
        report = build_report(load_dataset(args.data_dir), args.target, args.sku, args.as_of)
        sys.stdout.write(render_report(report, args.format))
        return 0
    except (ValueError, OSError, KeyError, TypeError, BadZipFile) as exc:
        sys.stderr.write(f"Ошибка данных: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
