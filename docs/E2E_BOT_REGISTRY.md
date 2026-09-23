# QORAI — адресные сценарии MVP

2026-09-23. Статус API не подменяет браузерный путь. Источник тестов — выданные XLSX, технические настройки помечены как настройки, не новые бизнес-данные.

| ID | Путь / наблюдаемый итог | Evidence / статус |
|---|---|---|
| API-01 | bootstrap → item: источник/дата/прогноз/остаток совпадают | `tests/test_api.py::test_bootstrap_source_date_defaults_and_selected_forecast` PASS |
| API-02 | пустая политика → blocked/null и причины | `test_empty_policy_blocks_order_not_zero` PASS |
| API-03 | явная политика → условный план → CSV ровно того же результата | `test_plan_identity_csv_exact_quantity_and_status` PASS |
| API-04 | два сценария одного SKU → отказ, без двойной закупки | `test_export_rejects_two_scenarios_of_same_sku` PASS |
| API-05 | прогноз → CSV сохраняет AZ/статус без двойного AY | `test_forecast_export_matches_same_report_and_free_stock` PASS |
| API-06 | поздний заказ → ранний дефицит остаётся видимым | `test_late_new_order_never_fixes_prior_shortage` PASS |
| API-07 | reload failure → последний исправный снимок доступен | `test_failed_reload_preserves_snapshot_and_results` PASS |
| API-08 | reload success → старый result_id недоступен | `test_successful_reload_invalidates_saved_results` PASS |
| WEB-01 | поиск/выбор товара → источник и расчёт на экране | PASS: ведущий выбрал ATN544045, сопоставимые MAE/6точек отображены |
| WEB-02 | явная политика/согласия → условный план и график | PASS: технические настройки дают Q300/mean_12; исправленный stale draft перепроверен |
| WEB-03 | ETA существующей партии → сценарий без изменения факта | PASS: ATN540126, существующая партия120, ETA24.09→08.10, Q0→36, дефицит01.10; источник сохранён |
| WEB-04 | добавить черновик/экспорт → файл совпадает с сервером | PASS: реальный CSV3219байт прочитан, Q300/status/source совпали; изменение ETA очищает черновик/отключает экспорт, Q0 добавить нельзя |
| WEB-05 | перечитать Excel → снимок сохранён, расчёты/черновик инвалидированы | PASS: перед reload Q36/draft1; loader/disabled → тот же SKU и дата22.09, рекомендаций нет, draft0, CSV disabled |
| WEB-06 | поиск без совпадений → пустой список → клавиатурная очистка | PASS: IEK+ATN даёт0, очистка возвращает2463 позиции IEK |
| DESIGN-01 | desktop QORAI → сравнение3 рабочих экранов со Stitch | PASS в рамках MVP, evidence в design-qa.md; не pixel-perfect и не все функции референса |
| DESIGN-02 | узкий viewport390×844 → мобильная компоновка | NOT VERIFIED: браузер оставил фактическую ширину1218; CSS не заменяет browser proof |

Команда API-runner: `py -m pytest tests/test_api.py -q`, 19 passed/0skips, 9.58s. Независимый общий прогон пяти наборов:91passed/0skips20.30s, без P0/P1 app/replenishment. Browser-статусы выше этим не закрыты. Не создавались фоновые расписания или внешние QA-боты.

Последующие узкие проверки: API28PASS/9.95s после hosting-boundary; frontend18 helper assertions PASS после QORAI branding. Ни одна из этих проверок не является публичным deploy или повтором полного91-тестового прогона.

Финальный API-guard:37 passed/0skips13.32s после добавления явного PUBLIC_ORIGIN для TLS-proxy; чужие Host/Origin отклонены. Реальный внешний хостинг этим не проверен.

Итоговый объединённый release-run после bootstrap-helper: `py -m pytest -q` →127passed/0skips19.43s. Перезапущенный preview принят ведущим в браузере (QORAI,22.09,3045items,errorlogs=[]). Это окончательный локальный функциональный результат; mobile и внешний deploy остаются с указанными ограничениями.
