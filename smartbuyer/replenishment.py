"""Conditional regular-sales replenishment from immutable reported source data."""

from calendar import monthrange
from collections.abc import Mapping
from copy import deepcopy
from datetime import date, timedelta
from math import ceil, isfinite
from numbers import Real
import re

from .forecast import forecast_month, rolling_backtest


def _number(value):
    try:
        return isinstance(value, Real) and not isinstance(value, bool) and isfinite(value) and value >= 0
    except OverflowError:
        return False


def _date(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Дата должна иметь формат YYYY-MM-DD.")
    return date.fromisoformat(value)


def _previous_months(cutoff):
    index = cutoff.year * 12 + cutoff.month - 1
    return [f"{(index - i) // 12:04d}-{(index - i) % 12 + 1:02d}" for i in range(6, 0, -1)]


def plan_item(item: dict, as_of: str, policy: dict, eta_overrides: dict | None = None) -> dict:
    """Return a provisional scenario, never an approved order or live inventory.

    Coverage is exactly H dates inclusive: A .. A+H-1, A=as_of+lead_days.
    The reported AZ is already net of AY. We protect AY and do not subtract it
    twice or claim that the remaining demand is reconciled with client orders.
    """
    item = deepcopy(item)
    stock = item.get("stock") or {}
    if not isinstance(stock, Mapping):
        stock = {}
    result = {
        "status": "blocked", "reasons": [], "assumptions": [
            "Условный расчёт обычных продаж по отчётному свободному остатку AZ, не живой склад.",
            "AZ уже уменьшен на AY: бронь повторно не вычитается.",
            "Состав AY и открытых заказов неизвестен: возможен перехлёст с прогнозом; оценка консервативная.",
            "Это не полный план исполнения клиентских обязательств.",
            "Приходы учитываются как отчётный сценарий, не как подтверждённая приёмка.",
            "Прогноз наблюдаемых продаж распределён равномерно по календарным дням после даты среза.",
            "Отрицательная проекция — расчётный баланс / накопленный дефицит, не отрицательный физический склад или подтверждённые backorders.",
            "Пустые продажи не нули; нет очистки разовых сделок и компенсации stockout.",
            "L — полный срок до принятого прихода. H — число дат A..A+H-1 включительно.",
            "Новый заказ в сценарии размещается на дату расчёта; последний допустимый срок отдельно не вычислен.",
            "Ночная самообучающаяся система не реализована.",
        ],
        "item": {"key": str(item.get("supplier", "")) + "::" + str(item.get("sku", "")),
                 **{key: item.get(key) for key in ("sku", "supplier", "name", "unit")}, "stock": stock},
        "forecast": {"method": None, "monthly": [], "selection_reason": None, "selection_warning": None},
        "metrics": {"tested": 0, "seasonal_mae": None, "mean_mae": None},
        "policy": {},
        "order": {"quantity": None, "raw_quantity": None, "arrival": None,
                  "latest_order_date": None, "planned_order_date": None, "pack_multiple": None, "moq": None},
        "calendar": [], "incoming": [], "sources": deepcopy(item.get("source_refs", [])),
        "first_deficit_date": None, "pre_arrival_risk": False,
    }
    reasons = result["reasons"]
    try:
        cutoff = _date(as_of)
    except (ValueError, TypeError) as exc:
        reasons.append(str(exc))
        return result
    if not isinstance(policy, Mapping):
        reasons.append("Нет параметров закупочной политики.")
        return result
    if policy.get("use_reported_stock") is not True:
        reasons.append("Нужно явно разрешить сценарий по отчётным остаткам и поставкам.")
    if policy.get("regular_only") is not True:
        reasons.append("Неизвестны полные клиентские обязательства; разрешён только сценарий обычных продаж.")
    for key in ("lead_days", "cover_days", "safety_days", "moq"):
        value = policy.get(key)
        if key == "moq" and value is None:
            value = item.get("moq")
        valid = _number(value)
        if key in ("lead_days", "cover_days"):
            valid = valid and isinstance(value, int) and (0 if key == "lead_days" else 1) <= value <= 180
        result["policy"][key] = value if valid else None
        if not valid:
            reasons.append(f"Не задан или некорректен параметр {key}.")
    result["policy"].update({key: policy.get(key) is True for key in ("use_reported_stock", "regular_only")})
    method = policy.get("method", "auto")
    result["policy"]["method"] = method if isinstance(method, str) else None
    if method not in ("auto", "mean_12", "seasonal_growth"):
        reasons.append("Неизвестный метод прогноза.")
    pack = item.get("pack_multiple")
    if not _number(pack) or pack == 0:
        reasons.append("Неизвестна положительная кратность поставки.")
    else:
        result["order"]["pack_multiple"] = pack
    result["order"]["moq"] = result["policy"]["moq"]
    if not isinstance(item.get("unit"), str) or not item["unit"].strip():
        reasons.append("Неизвестна единица измерения.")
    if not _number(stock.get("free")):
        reasons.append("Свободный отчётный остаток отсутствует или некорректен.")
    if stock.get("status") == "inconsistent_reported":
        reasons.append("Отчётные остатки не согласованы: AX − AY не равно AZ.")
    if stock.get("as_of") is not None:
        try:
            stock_date = _date(stock["as_of"])
            if stock_date > cutoff:
                reasons.append("Нельзя использовать остаток из будущего относительно даты расчёта.")
            elif stock_date < cutoff:
                result["assumptions"].append("Отчётный остаток старше даты расчёта; перенос без неизвестных движений — сценарное допущение.")
        except (TypeError, ValueError):
            reasons.append("Некорректна дата отчётного остатка.")
    for field in ("free", "reported", "reserved"):
        value = stock.get(field)
        if value is not None and not _number(value):
            stock[field] = None
            reasons.append(f"Некорректное значение остатка {field}.")
    if eta_overrides is None:
        eta_overrides = {}
    if not isinstance(eta_overrides, Mapping):
        reasons.append("Изменения ETA должны быть словарём существующих индексов.")
        return result
    inbound = item.get("inbound")
    if not isinstance(inbound, list):
        reasons.append("Неизвестен состав поставок в пути.")
        inbound = []
    if any(key not in {str(i) for i in range(len(inbound))} for key in eta_overrides):
        reasons.append("Нельзя добавить новую поставку через изменение ETA.")
    arrivals = {}
    for index, row in enumerate(inbound):
        quantity = row.get("quantity") if isinstance(row, Mapping) else None
        original = row.get("eta") if isinstance(row, Mapping) else None
        scenario = eta_overrides.get(str(index))
        incoming = {"index": index, "quantity": quantity if _number(quantity) else None,
                    "original_eta": original if isinstance(original, str) else None,
                    "scenario_eta": scenario if isinstance(scenario, str) else None,
                    "status": "blocked", "source_status": row.get("status") if isinstance(row, Mapping) else None}
        result["incoming"].append(incoming)
        if not _number(quantity):
            reasons.append(f"Поставка {index}: количество неизвестно или некорректно.")
            continue
        if quantity == 0:
            incoming["status"] = "zero_quantity"
            if str(index) in eta_overrides:
                reasons.append(f"Поставка {index}: нельзя сдвигать нулевой приход как существующий заказ.")
            continue
        try:
            eta = _date(scenario if str(index) in eta_overrides else original)
            if eta <= cutoff:
                raise ValueError("ETA должна быть позже даты среза; просроченный приход требует новой даты.")
        except (ValueError, TypeError) as exc:
            reasons.append(f"Поставка {index}: {exc}")
            continue
        incoming["status"] = "scenario_eta" if str(index) in eta_overrides else "reported_eta"
        arrivals[eta] = arrivals.get(eta, 0) + quantity
    if reasons:
        return result

    settings = result["policy"]
    try:
        arrival = cutoff + timedelta(days=settings["lead_days"])
        end = arrival + timedelta(days=settings["cover_days"] - 1)
    except OverflowError:
        reasons.append("Горизонт выходит за допустимый диапазон дат.")
        return result
    result["order"]["arrival"] = arrival.isoformat()
    history = item.get("history")
    if not isinstance(history, Mapping):
        reasons.append("Нет месячной истории продаж.")
        return result
    partial = set(item.get("partial_months") or [])
    history = {key: value for key, value in history.items() if key not in partial}
    backtest = rolling_backtest(history, [m for m in _previous_months(cutoff) if m not in partial])
    tested = backtest["tested"]
    seasonal_mae = backtest["metrics"]["seasonal_growth"]["mae_units"]
    mean_mae = backtest["metrics"]["mean_12"]["mae_units"]
    result["metrics"].update(tested=tested, seasonal_mae=seasonal_mae, mean_mae=mean_mae)
    selection = result["forecast"]
    selection["selection_warning"] = "Ошибка на периодах выбора метода не является независимой проверкой его будущей точности."
    if method == "auto":
        method = "seasonal_growth" if tested >= 3 and seasonal_mae < mean_mae else "mean_12"
        selection["selection_reason"] = ("Минимальный MAE на одинаковых исторических месяцах; при равенстве выбран mean_12."
                                         if tested >= 3 else "Меньше трёх общих точек: непроверенный fallback mean_12, только при доступной истории.")
    else:
        selection["selection_reason"] = "Метод явно выбран менеджером."
    selection["method"] = method
    days = [cutoff + timedelta(days=i) for i in range(0 if arrival == cutoff else 1, (end - cutoff).days + 1)]
    rates = {}
    for month in sorted({day.isoformat()[:7] for day in days}):
        forecast = forecast_month(history, month, as_of, method)
        if forecast["status"] != "ok":
            reasons.extend(f"{month}: {warning}" for warning in forecast["warnings"])
            return result
        quantity = forecast["forecast_units"]
        selection["monthly"].append({"month": month, "units": quantity})
        rates[month] = quantity / monthrange(int(month[:4]), int(month[5:]))[1]
    balance = stock["free"]
    calendar = []
    for day in days:
        daily = rates[day.isoformat()[:7]]
        demand = daily if day > cutoff else 0
        incoming = arrivals.get(day, 0)
        balance += incoming - demand
        calendar.append({"date": day.isoformat(), "demand": demand, "inbound": incoming,
                         "without_order": balance, "with_order": None,
                         "safety": settings["safety_days"] * daily,
                         "pre_arrival_risk": day < arrival and balance < 0})
    try:
        raw = max(0, max(row["safety"] - row["without_order"] for row in calendar if row["date"] >= arrival.isoformat()))
        if not isfinite(raw) or any(not isfinite(row[key]) for row in calendar for key in ("without_order", "safety")):
            raise ValueError("Нефинитный результат расчёта.")
        quantity = pack * ceil(max(raw, settings["moq"]) / pack) if raw > 0 else 0
        if not isfinite(quantity):
            raise ValueError("Количество заказа выходит за числовой диапазон.")
    except (ValueError, OverflowError):
        reasons.append("Переполнение расчёта; проверьте параметры и количества.")
        return result
    for row in calendar:
        row["with_order"] = row["without_order"] + (quantity if row["date"] >= arrival.isoformat() else 0)
        if not isfinite(row["with_order"]) or (row["date"] >= arrival.isoformat() and row["with_order"] < 0):
            reasons.append("Числовая ошибка прогноза остатка после заказа; количество не предлагается.")
            return result
    result.update(status="provisional", calendar=calendar,
                  first_deficit_date=next((row["date"] for row in calendar if row["without_order"] < 0), None),
                  pre_arrival_risk=any(row["pre_arrival_risk"] for row in calendar))
    result["order"].update(quantity=quantity, raw_quantity=raw,
                           planned_order_date=as_of if quantity > 0 else None)
    if result["pre_arrival_risk"]:
        reasons.append("Дефицит до прихода нового заказа этой закупкой не исправляется.")
    return result
