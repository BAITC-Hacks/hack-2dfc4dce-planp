# QORAI — контракт локального API

2026-09-23. API реализован; текущая проверка итерации коррекции спроса — в [QA_REPORT](QA_REPORT.md). Исторический набор после host/TLS-proxy guard содержал 37 API-тестов PASS, он не заменяет проверку новых полей. По умолчанию один процесс `127.0.0.1:8765`, same-origin; без авторизации, внешней отправки заказов и загрузки произвольных файлов.
Источники — только папка `HACKALEM_DATA_DIR` с выданными 12 XLSX. `null` означает неизвестно, не ноль. Ключ товара: `supplier + "::" + sku`.

Host allowlist задаётся явно через `HACKALEM_ALLOWED_HOSTS`; по умолчанию только localhost/127.0.0.1/::1. Origin должен совпадать со схемой/хостом/портом. Внешняя привязка требует явного `HACKALEM_BIND_HOST` и разрешённого домена. Первая публикация Render проверена 23.09.2026; фактические настройки и ограничения — в [DEPLOYMENT.md](DEPLOYMENT.md). Host allowlist не является авторизацией пользователя.

При TLS termination можно явно задать точный `HACKALEM_PUBLIC_ORIGIN`, согласованный с allowlist: это решает HTTPS-origin/HTTP-backend без wildcard доверия прокси. Чужой Host/Origin отклоняется HTTP403. Настройка не создаёт публичный URL и не заменяет авторизацию.

| Метод / путь | Вход | Результат |
|---|---|---|
| GET `/api/health` | — | состояние процесса; не доказательство загрузки данных |
| GET `/api/bootstrap` | — | `as_of`, `target_month` (следующий месяц после источника), `coverage`, `suppliers`, `items`, `warnings`, `sources`, `policy_defaults` |
| GET `/api/item?key=…` | полный ключ | `item` (исходный объект), `report` (строка CLI), `metadata:{as_of,target_month,warnings}` |
| POST `/api/plan` | `{key,policy,eta_overrides:{"index":"YYYY-MM-DD"}}` | результат `plan_item` и серверный `result_id`; нет ядра — 503, не поддельный расчёт |
| POST `/api/export` | `{result_ids:["…"]}` | CSV ранее рассчитанных сервером положительных неблокированных заказов; пустой результат — понятная ошибка |
| GET `/api/forecast-export?key=…` | ключ необязателен | CSV того же объекта отчёта CLI, не заказ поставщику |
| POST `/api/reload` | без тела | перечитать только настроенные XLSX; ошибка сохраняет последний исправный снимок |

`items` в bootstrap: `key, sku, supplier, name, unit, stock, pack_multiple, moq, forecast_units, mean_units, forecast_status, backtest_tested, seasonal_mae, mean_mae`.
`forecast_units` — сезонный кандидат, `mean_units` — простой средний; выбор метода для условной закупки отдельно в политике. Статусы источника и запаса не скрываются.

`sources[].status` различает `read_cached_values` и `not_parsed`. На выданном наборе зарегистрированы 12 файлов: 10 разобраны, 2 (`seasonality` обоих поставщиков) только проверены по хешу. Источники операций имеют `parse_summary` с количеством принятых/исключённых строк. Количество sources не является количеством полностью проанализированных источников; `read_cached_values` не означает влияние всех ячеек файла на прогноз.

В `item` добавлены `stock_history:{YYYY-MM:number|null}` (начало месяца, `stock_history_metadata.timing='month_start'`) и `transactions_monthly:{YYYY-MM:{document_quantities:number[],positive_total:number,document_count:integer}}`. Количества положительны, агрегированы по одному документу и точному коду товара; номера документов, клиентские идентификаторы и сырые строки не передаются. `transactions_metadata` содержит статус сопоставления, срез и ограничения; `source_refs` — файл/лист/диапазон. Пустой объект не означает нулевой спрос.

Строка отчёта и результат `/api/plan` включают `demand_adjustment:{status,parameters,summary,monthly,adjusted_history,source_refs,assumptions}`. `monthly` сохраняет `raw`, `after_outlier`, `adjusted`, `opening_stock`, `removed_units`, `estimated_lost_units`, `outlier_threshold`, `document_count`, `removal_share`, `reference_units`, `reference_months`, `reference_method`, `stockout_status`, `reasons`. Оценки не заменяют исходную `item.history`. MAE оценивает прогноз по исходным наблюдаемым продажам.

`coverage.order_blocked_items` относится к исходному CLI-отчёту прогнозов. Условные заказы рассчитываются отдельно через `/api/plan`; этот счётчик не пересчитывается как число всех возможных допустимых сценариев. Все пять требований кейса и границы подтверждения перечислены в [CASE_COMPLIANCE](CASE_COMPLIANCE.md).

```json
{"lead_days":null,"cover_days":null,"safety_days":null,"moq":null,"method":"auto","use_reported_stock":false,"regular_only":false}
```

Это `policy_defaults`, не введённые за пользователя условия. `lead_days`, `cover_days`, `safety_days`, `moq` — явно согласуемые параметры. `use_reported_stock` подтверждает использование отчётного свободного остатка; `regular_only` ограничивает сценарий регулярными продажами при неизвестных клиентских обязательствах. Даже рассчитанный заказ условный, не подтверждённая потребность компании.

`policy.scenario_mode` принимает `manual` (по умолчанию) или `demonstration`. Явно выбранные демонстрационные условия маркируются в результате/допущениях/CSV; они не объявляются фактической политикой поставщика. CSV также сохраняет описание коррекции спроса из того же серверного расчёта.

ETA меняется только по индексу существующей входящей партии; это сценарий менеджера, а не изменение исходного Excel. Запаздывающая поставка не исправляет уже возникший ранее дефицит. Бронь повторно из AZ не вычитается.

Ошибка: HTTP 400/404/503, JSON `{"error":{"code":"…","message":"…"}}`, русский текст без traceback и приватных путей. Неверные поля/типы — 400. CSV — UTF-8, безопасные текстовые ячейки, числовое количество; идентификатор результата учитывает источник/политику/сценарий. После успешного reload старые результаты недействительны.

Один и тот же result_id в запросе экспорта дедуплицируется; разные сценарии одного supplier::SKU вместе отклоняются HTTP400 `conflicting_plans`, чтобы не удвоить закупку. CSV содержит `planned_order_date` — размещение текущего сценария; `latest_order_date` ядра неизвестен и не выдается как дедлайн.

`/` отдаёт `web/index.html`, `/static/*` — файлы `web/`. Browser E2E и итоговый статус перечисляются отдельно в QA; доступность HTTP сама по себе не означает готовность интерфейса.
