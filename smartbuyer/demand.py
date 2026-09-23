"""Causal, explainable estimates; never overwrite observed sales or invent clients."""

from calendar import monthrange
from math import fsum, isclose, isfinite
from statistics import median

from .forecast import _month, _shift, _valid


PARAMETERS = {
    "min_documents": 5, "outlier_median_multiplier": 3,
    "outlier_mad_multiplier": 3, "min_in_stock_months": 3,
    "reference_months": 12, "low_sales_ratio": 0.5,
}


def adjust_demand(item, as_of):
    """Each completed month uses its documents and strictly earlier observed refs.

    Opening zero is a risk indicator, not proof of absent days. The month-level
    estimate is conditional; imputed months NEVER train subsequent references.
    """
    cutoff = _month(as_of[:7])
    partial = set(item.get("partial_months") or [])
    stock = item.get("stock_history") or {}
    transactions = item.get("transactions_monthly") or {}
    history, rows, observed = {}, [], {}
    summary = dict(months_adjusted=0, outlier_months=0, outlier_documents=0,
                   removed_units=0.0, stockout_months=0, estimated_lost_units=0.0)
    for month, raw in sorted((item.get("history") or {}).items()):
        current = _month(month)
        if current >= cutoff or month in partial:
            continue
        opening = stock.get(month)
        valid_raw = _valid(raw)
        row = dict(month=month, raw=raw if valid_raw else None,
                   after_outlier=raw if valid_raw else None, adjusted=raw if valid_raw else None,
                   opening_stock=opening if _valid(opening) else None,
                   outlier_documents=0, removed_units=0.0, estimated_lost_units=0.0,
                   reference_units=None, reference_months=[], reference_method=None, stockout_status="invalid_sales",
                   outlier_threshold=None, document_count=0, removal_share=0.0, reasons=[])
        rows.append(row)
        # Invalid inputs stay unavailable, never converted to zero or estimated.
        history[month] = raw if valid_raw else None
        if not valid_raw:
            row["reasons"].append("Продажи неизвестны/отрицательны/неконечны; корректировка не выполняется.")
            continue
        tx = transactions.get(month) or {}
        docs = tx.get("document_quantities") or []
        total = tx.get("positive_total")
        positive = [float(value) for value in docs if _valid(value) and value > 0]
        row["document_count"] = len(positive)
        try:
            consistent = (len(positive) == len(docs) and _valid(total) and total > 0
                          and isclose(fsum(positive), total, rel_tol=1e-9, abs_tol=1e-9))
        except OverflowError:
            consistent = False
        if docs and not consistent:
            row["reasons"].append("Операции содержат неположительные/некорректные значения или несогласованную сумму; очистка пропущена.")
        elif consistent and len(positive) >= PARAMETERS["min_documents"]:
            centre = median(positive)
            mad = median(abs(value - centre) for value in positive)
            threshold = max(PARAMETERS["outlier_median_multiplier"] * centre,
                            centre + PARAMETERS["outlier_mad_multiplier"] * 1.4826 * mad)
            excess = fsum(max(0.0, value - threshold) for value in positive)
            share = min(1.0, max(0.0, excess / total))
            row.update(outlier_threshold=threshold, removal_share=share,
                       outlier_documents=sum(value > threshold for value in positive),
                       removed_units=raw * share, after_outlier=raw * (1 - share))
            if excess:
                row["reasons"].append("Избыточная часть крупных документов нормирована долей операций к месячным продажам; это оценка, не идентификация клиента.")
        else:
            row["reasons"].append("Недостаточно положительных документов для робастной очистки (нужно не меньше 5).")
        clean = row["after_outlier"]
        row["adjusted"] = clean
        observed[month] = clean
        if not _valid(opening):
            row["stockout_status"] = "missing_opening_stock"
        elif opening != 0:
            row["stockout_status"] = "not_indicated"
        else:
            first = _shift(current, -PARAMETERS["reference_months"])
            references = [key for key in observed if first <= key < month
                          and _valid(stock.get(key)) and stock[key] > 0]
            row["reference_months"] = references
            row["stockout_status"] = "insufficient_reference"
            if len(references) >= PARAMETERS["min_in_stock_months"]:
                daily = median(observed[key] / monthrange(int(key[:4]), int(key[5:]))[1]
                               for key in references)
                row["reference_method"] = "trailing_in_stock_median_daily"
                seasonal = _shift(current, -12)
                if seasonal in references:
                    daily = min(daily, observed[seasonal] / monthrange(int(seasonal[:4]), int(seasonal[5:]))[1])
                    row["reference_method"] = "min_trailing_median_and_prior_year_same_month"
                reference = daily * monthrange(current.year, current.month)[1]
                row["reference_units"] = reference if isfinite(reference) else None
                row["stockout_status"] = "not_indicated"
                # Use observed low sales, not a low value created by outlier removal.
                if isfinite(reference) and reference > 0 and raw < PARAMETERS["low_sales_ratio"] * reference:
                    row.update(stockout_status="estimated", adjusted=max(clean, reference),
                               estimated_lost_units=max(0.0, reference - clean))
                    row["reasons"].append("Нулевой начальный остаток и низкие наблюдаемые продажи: оценка до медианной дневной базы прошлых месяцев с положительным начальным остатком; дни отсутствия неизвестны.")
        history[month] = row["adjusted"]
        summary["outlier_documents"] += row["outlier_documents"]
        summary["outlier_months"] += row["outlier_documents"] > 0
        summary["removed_units"] += row["removed_units"]
        summary["stockout_months"] += row["stockout_status"] == "estimated"
        summary["estimated_lost_units"] += row["estimated_lost_units"]
        summary["months_adjusted"] += row["adjusted"] != raw
    return {
        "status": "estimated_adjustments" if summary["months_adjusted"] else "unchanged",
        "parameters": dict(PARAMETERS), "summary": summary, "monthly": rows,
        "adjusted_history": history, "source_refs": item.get("source_refs", []),
        "assumptions": [
            "Исходные продажи сохранены отдельно; корректировки — оценки, не подтверждённый скрытый спрос.",
            "Крупные документы ограничены робастным порогом; client ID отсутствует, группировки по клиенту нет.",
            "Доля избытка операций применяется к месячной базе; абсолютные несверенные операции не вычитаются.",
            "Нулевой начальный остаток не доказывает stockout всего месяца; дни отсутствия не вычисляются.",
            "База компенсации: только более ранние месяцы с положительным начальным остатком, после очистки выбросов, без рекурсивной компенсации.",
            "При доступном прошлогоднем аналоге база ограничена меньшим из сезонного дневного уровня и медианы, чтобы не завышать низкий сезон.",
            "Частичные и будущие месяцы исключены. MAE сравнивается с исходными наблюдаемыми продажами, не с истинным скрытым спросом.",
        ],
    }
