/* Native frontend; all forecasts, quantities and CSVs are authoritative server results.
   Request gates, null-safe fields and result-ID export adapted from Claude Opus's packet draft. */
'use strict';
const $ = (id) => document.getElementById(id);
const state = { bootstrap: null, key: null, item: null, report: null, plan: null, drafts: new Map(), tab: 'today', chart: null };
const numberFormat = new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 2 });
const fmt = (value) => typeof value === 'number' && Number.isFinite(value) ? numberFormat.format(value) : 'Нет данных';
const esc = (value) => String(value ?? '').replace(/[&<>"']/g, (c) => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dateText = (value) => /^\d{4}-\d{2}-\d{2}$/.test(value || '') ? value.split('-').reverse().join('.') : (value || 'Не задана');
const methodText = (value) => ({ mean_12: 'Простое среднее', seasonal_growth: 'Сезонность × динамика', auto: 'Автовыбор' }[value] || 'Не выбран');
function gate() {
  let sequence = 0, controller;
  return { start() { controller?.abort(); controller = new AbortController(); const id = ++sequence; return { signal: controller.signal, current: () => sequence === id }; }, cancel() { controller?.abort(); sequence++; } };
}
const itemGate = gate(), planGate = gate();
function message(text, error = false) { $('message').textContent = text; $('message').hidden = !text; $('message').className = `message${error ? ' error' : ''}`; }
async function request(path, options = {}) {
  const response = await fetch(path, { credentials: 'same-origin', ...options, headers: options.body ? {'Content-Type': 'application/json'} : undefined });
  if (!response.ok) {
    let text = `Не удалось выполнить запрос (HTTP ${response.status}).`;
    try { const body = await response.json(); text = body.error?.message || (typeof body.detail === 'string' ? body.detail : text); } catch (_) { /* Keep safe HTTP message. */ }
    throw new Error(text);
  }
  return response;
}
async function json(path, options) { return (await request(path, options)).json(); }
function table(headers, rows, classes = []) {
  return `<table><thead><tr>${headers.map((x) => `<th>${esc(x)}</th>`).join('')}</tr></thead><tbody>${rows.map((r, i) => `<tr class="${classes[i] || ''}">${r.map((v) => `<td>${esc(v)}</td>`).join('')}</tr>`).join('')}</tbody></table>`;
}
function showTab(tab) {
  state.tab = tab;
  document.querySelectorAll('.page').forEach((el) => { el.hidden = el.id !== tab; });
  document.querySelectorAll('[data-tab]').forEach((el) => { if (el.dataset.tab === tab) el.setAttribute('aria-current', 'page'); else el.removeAttribute('aria-current'); });
  if (tab === 'today') requestAnimationFrame(drawChart);
}
function selectedBrief() { return state.bootstrap?.items.find((item) => item.key === state.key); }
function displaySelector(preferred = state.key) {
  const supplier = $('supplier').value, query = $('search').value.trim().toLocaleLowerCase('ru');
  const items = (state.bootstrap?.items || []).filter((item) => (!supplier || item.supplier === supplier) && (!query || `${item.sku} ${item.name}`.toLocaleLowerCase('ru').includes(query)));
  const fragment = document.createDocumentFragment();
  for (const item of items) { const option = document.createElement('option'); option.value = item.key; option.textContent = `${item.sku} · ${item.name || 'Название не указано'}`; fragment.appendChild(option); }
  $('item-select').replaceChildren(fragment);
  $('item-count').textContent = `(${items.length})`;
  const chosen = items.find((item) => item.key === preferred) || items.find((item) => item.stock?.free != null && item.pack_multiple != null && item.forecast_units != null) || items[0];
  $('item-select').disabled = !chosen;
  if (chosen) { $('item-select').value = chosen.key; if (chosen.key !== state.key || !state.item) selectItem(chosen.key); }
  else { itemGate.cancel(); state.key = null; state.item = null; state.report = null; invalidatePlan(); renderItem(); }
}
function invalidatePlan(removeDraft = false) {
  planGate.cancel(); state.plan = null; $('calculate').disabled = !state.item; $('form-status').textContent = 'Изменения требуют нового расчёта.';
  if (removeDraft && state.drafts.delete(state.key)) {
    renderDraft(); message('Параметры изменены: прежняя рекомендация этого товара удалена из черновика. Рассчитайте потребность заново.');
  }
  renderRecommendation(); renderOverview();
}
async function selectItem(key) {
  state.key = key; state.item = null; state.report = null; invalidatePlan();
  $('selection-meta').textContent = 'Загружаем выбранный товар…'; $('policy-form').reset(); $('calculate').disabled = true;
  const ticket = itemGate.start();
  try {
    const result = await json(`/api/item?key=${encodeURIComponent(key)}`, { signal: ticket.signal });
    if (!ticket.current() || state.key !== key) return;
    state.item = result.item; state.report = result.report; resetPolicy(false); renderItem(); message('');
  } catch (error) { if (ticket.current() && error.name !== 'AbortError') { message(error.message, true); renderItem(); } }
}
function resetPolicy(removeDraft = true) {
  $('policy-form').reset();
  const defaults = state.bootstrap?.policy_defaults || {};
  for (const name of ['lead_days', 'cover_days', 'safety_days', 'moq']) $('policy-form').elements[name].value = name === 'moq' ? (state.item?.moq ?? '') : (defaults[name] ?? '');
  $('policy-form').elements.method.value = defaults.method || 'auto';
  renderIncoming(); invalidatePlan(removeDraft);
}
function renderItem() {
  const item = state.item;
  if (!item) { $('selection-meta').textContent = state.key ? 'Данные товара пока недоступны.' : 'По этому фильтру товаров нет.'; $('calculate').disabled = true; $('forecast-export').disabled = true; renderOverview(); renderRecommendation(); return; }
  const stock = item.stock || {};
  $('selection-meta').innerHTML = `<strong>${esc(item.sku)}</strong> · ${esc(item.name)} &nbsp; / &nbsp; ${esc(item.supplier)} &nbsp; / &nbsp; Ед.: ${esc(item.unit || 'не подтверждена')} &nbsp; / &nbsp; Срез: ${esc(dateText(stock.as_of || state.bootstrap.as_of))}`;
  $('pack-value').textContent = item.pack_multiple == null ? 'Неизвестна' : `${fmt(item.pack_multiple)} ${item.unit || ''}`;
  $('data-summary').innerHTML = `<span>SKU: <strong>${esc(item.sku)}</strong></span><span>Дата набора: <strong>${esc(dateText(state.bootstrap.as_of))}</strong></span><span>Прогноз на: <strong>${esc(state.bootstrap.target_month)}</strong></span><span>Исторических месяцев: <strong>${Object.keys(item.history || {}).length}</strong></span>`;
  const warnings = [...(state.bootstrap.warnings || []), ...(item.warnings || [])];
  $('warnings').innerHTML = [...new Set(warnings)].map((x) => `<li>${esc(x)}</li>`).join('');
  $('source-refs').innerHTML = table(['Файл', 'Лист', 'Диапазон', 'Поле'], (item.source_refs || []).map((r) => [r.file, r.sheet, r.range, r.field]));
  $('calculate').disabled = false; $('forecast-export').disabled = false;
  renderOverview(); renderRecommendation();
}
function metric(label, value, note, style = '') { return `<div class="metric ${style}"><span class="label">${esc(label)}</span><span class="value">${esc(fmt(value))}</span><span class="unit">${esc(state.item?.unit || '')}</span><span class="note">${esc(note)}</span></div>`; }
function explainOrder(plan) {
  if (plan?.status !== 'provisional') return 'Сначала подтвердите необходимые входы для расчёта.';
  const order = plan.order, unit = plan.item.unit || 'ед.', coverage = plan.calendar.filter((row) => row.date >= order.arrival);
  const worst = coverage.reduce((current, row) => !current || row.safety - row.without_order > current.safety - current.without_order ? row : current, null);
  if (!worst) return 'В ответе нет календаря покрытия для объяснения потребности.';
  if (order.quantity === 0) return `В окне ${dateText(order.arrival)} — ${dateText(coverage.at(-1).date)} отчётный свободный остаток ${fmt(plan.item.stock?.free)} ${unit} и учтённые по датам поставки покрывают прогноз и заданный страховой уровень. При этих допущениях дополнительная закупка не нужна.${plan.pre_arrival_risk ? ' До начала этого окна есть отдельный риск дефицита.' : ''}`;
  return `На ${dateText(worst.date)} расчётный баланс без нового заказа — ${fmt(worst.without_order)} ${unit}, а страховой уровень — ${fmt(worst.safety)} ${unit}. Ядро рассчитало потребность ${fmt(order.raw_quantity)} ${unit}. С учётом минимального заказа ${fmt(order.moq)} ${unit} и кратности ${fmt(order.pack_multiple)} ${unit} итоговый заказ — ${fmt(order.quantity)} ${unit}.`;
}
function renderOverview() {
  const item = state.item, plan = state.plan, report = state.report, brief = selectedBrief(), stock = item?.stock || {};
  const inbound = item?.inbound || [];
  const inboundTotal = inbound.length && inbound.every((r) => typeof r.quantity === 'number') ? inbound.reduce((a, r) => a + r.quantity, 0) : null;
  const lastValue = plan?.order?.quantity;
  $('metrics').innerHTML = metric('Отчётный остаток', stock.reported, 'Не подтверждение приёмки в реальном времени') + metric('Отчётная бронь', stock.reserved, 'Уже исключена из свободного остатка', 'accent') + metric('Свободно к расходу', stock.free, 'Повторно бронь не вычитаем') + metric('По таблице в пути', inboundTotal, 'Дата приёмки учитывается отдельно') + metric('Расчётный заказ', lastValue, plan ? 'Условная потребность при выбранной политике' : 'Нужны сроки и правила запаса', lastValue > 0 ? 'accent' : '');
  const ready = plan?.status === 'provisional', risk = ready && plan.first_deficit_date;
  const title = !item ? 'Выберите товар из выданных таблиц' : !plan ? 'История загружена. Для закупки нужны ваши параметры' : !ready ? 'Расчёт остановлен: не хватает подтверждённых входов' : risk ? `Риск дефицита с ${dateText(plan.first_deficit_date)}` : lastValue > 0 ? 'Пополнение поддержит заданный уровень запаса' : 'В выбранном горизонте дополнительный заказ не нужен';
  const sub = !plan ? 'Модель не подставляет выдуманные сроки или запасы. Задайте условия во вкладке «Данные и допущения».' : !ready ? (plan.reasons || []).join(' ') : plan.pre_arrival_risk ? 'Новый заказ не успеет закрыть дефицит до его приёмки. Этот риск остаётся в графике.' : 'Результат условный: прогноз по истории продаж, отчётные остатки и указанные вами сроки. Исходные файлы не изменены.';
  $('summary-banner').classList.toggle('risk', Boolean(risk || (plan && !ready)));
  $('summary-banner').innerHTML = `<div><span class="eyebrow">${ready ? 'Условный сценарий · не подтверждённый заказ' : 'Объяснимый расчёт'}</span><h1>${esc(title)}</h1><p>${esc(sub)}</p></div><button data-go="${ready ? 'recommendations' : 'parameters'}" class="primary" type="button">${ready ? 'Открыть рекомендацию' : 'Задать параметры'}</button>`;
  $('explanation-title').textContent = ready ? 'Обоснование количества' : 'История — основа прогноза';
  const explanation = ready ? `<div class="explanation-block"><strong>Что предлагаем</strong>${lastValue > 0 ? `Заказать ${esc(fmt(lastValue))} ${esc(item.unit || '')} к ${esc(dateText(plan.order.arrival))}.` : 'Не заказывать дополнительно в указанном горизонте.'}</div><div class="explanation-block"><strong>Почему именно столько</strong>${esc(explainOrder(plan))}</div><p class="muted small">Прогноз: ${esc(methodText(plan.forecast.method))}. ${esc(plan.forecast.selection_reason)}</p>${plan.pre_arrival_risk ? '<div class="explanation-block error"><strong>Важно</strong>До новой поставки есть дефицит. Заказ в эту дату не исправляет прежнюю нехватку.</div>' : ''}` : `<div class="explanation-block"><strong>Что уже известно</strong>Вычисления используют историю из выданных таблиц. Месячные продажи не смешиваются с расходными документами.</div><div class="explanation-block warning"><strong>Что ещё нужно</strong>Срок до приёмки, горизонт покрытия и страховой запас. Неизвестные величины не заменяем нулями.</div>`;
  $('explanation').innerHTML = explanation;
  const metrics = report?.backtest?.metrics || {}, seasonal = metrics.seasonal_growth?.mae_units ?? brief?.seasonal_mae, mean = metrics.mean_12?.mae_units ?? brief?.mean_mae, tested = report?.backtest?.tested ?? brief?.backtest_tested ?? 0;
  $('comparison').innerHTML = `<div class="comparison-grid"><div class="comparison-box"><span>Сезонность × динамика · MAE</span><strong>${esc(fmt(seasonal))}</strong><p>В единицах выбранного товара</p></div><div class="comparison-box"><span>Простое среднее · MAE</span><strong>${esc(fmt(mean))}</strong><p>Меньше ошибка — лучше на этих месяцах</p></div><div class="comparison-box"><span>Общих проверенных месяцев</span><strong>${esc(tested)}</strong><p>Это сравнение на истории, не обещание будущей точности и не доказательство экономии.</p></div></div>`;
  const calendar = ready ? plan.calendar : [];
  $('calendar-section').hidden = !calendar.length;
  if (calendar.length) {
    $('calendar').innerHTML = table(['Дата', 'Прогноз расхода', 'Приход в эту дату', 'Без нового заказа', 'С новым заказом', 'Страховой запас'], calendar.map((r) => [dateText(r.date), fmt(r.demand), fmt(r.inbound), fmt(r.without_order), fmt(r.with_order), fmt(r.safety)]), calendar.map((r) => r.pre_arrival_risk ? 'risk-row' : ''));
    state.chart = { labels: calendar.map((r) => r.date), lines: [{name:'Без нового заказа', color:'#ba1a1a', values:calendar.map((r) => r.without_order)}, {name:'С новым заказом', color:'#004ac6', values:calendar.map((r) => r.with_order)}, {name:'Страховой запас', color:'#9aa7bf', values:calendar.map((r) => r.safety), dash:true}] };
    $('chart-title').textContent = 'Расчётный баланс и дефицит по датам'; $('chart-subtitle').textContent = `${dateText(calendar[0].date)} — ${dateText(calendar.at(-1).date)} · ${methodText(plan.forecast.method)}`;
    $('chart-caption').textContent = 'Ниже нуля — расчётный дефицит, не отрицательный физический склад. Линии — условная проекция; источник остатка и реестр поставок не меняются.';
  } else {
    const history = Object.entries(item?.history || {}).sort(([a], [b]) => a.localeCompare(b));
    state.chart = { labels: history.map(([m]) => m), lines: [{name:'Наблюдаемые продажи', color:'#004ac6', values:history.map(([,q]) => q)}] };
    $('chart-title').textContent = 'История продаж'; $('chart-subtitle').textContent = 'Месячные значения из выданного источника'; $('chart-caption').textContent = `Пустые значения остаются разрывами. ${state.bootstrap?.as_of?.slice(0,7) || 'Текущий месяц источника'} — неполный месяц, исключён из обучения.`;
  }
  $('chart-legend').innerHTML = state.chart.lines.map((line) => `<span style="--legend-color:${line.color}">${line.name}</span>`).join('');
  $('chart-table').innerHTML = table(['Период', ...state.chart.lines.map((l) => l.name)], state.chart.labels.map((label, i) => [label, ...state.chart.lines.map((l) => fmt(l.values[i]))]));
  requestAnimationFrame(drawChart);
}
function drawChart() {
  const canvas = $('main-chart'); if (!state.chart || state.tab !== 'today' || !canvas.clientWidth) return;
  const width = canvas.clientWidth, height = canvas.clientHeight, dpr = window.devicePixelRatio || 1;
  canvas.width = width * dpr; canvas.height = height * dpr;
  const ctx = canvas.getContext('2d'); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, width, height);
  const { labels, lines } = state.chart, values = lines.flatMap((l) => l.values).filter((v) => typeof v === 'number' && Number.isFinite(v));
  ctx.font = '11px "Segoe UI", sans-serif'; ctx.fillStyle = '#565e74';
  if (!values.length) { ctx.textAlign = 'center'; ctx.fillText('Нет достаточных числовых данных для графика', width / 2, height / 2); return; }
  const left = 59, right = 15, top = 18, bottom = 42, low = Math.min(0, ...values), high = Math.max(1, ...values), range = high - low || 1;
  const x = (i) => left + i / Math.max(1, labels.length - 1) * (width-left-right), y = (v) => top + (high-v) / range * (height-top-bottom);
  for (let i=0; i<=4; i++) { const value = low + range*i/4, yy = y(value); ctx.beginPath();ctx.strokeStyle='#e5eeff';ctx.lineWidth=1;ctx.moveTo(left,yy);ctx.lineTo(width-right,yy);ctx.stroke();ctx.fillStyle='#737686';ctx.textAlign='right';ctx.fillText(numberFormat.format(value),left-9,yy+4); }
  if (low < 0) { const zero = y(0); ctx.beginPath();ctx.strokeStyle='#737686';ctx.lineWidth=1.5;ctx.setLineDash([4,3]);ctx.moveTo(left,zero);ctx.lineTo(width-right,zero);ctx.stroke();ctx.setLineDash([]);ctx.fillStyle='white';ctx.fillRect(0,zero-8,left-4,17);ctx.fillStyle='#ba1a1a';ctx.textAlign='right';ctx.fillText('0',left-9,zero+4); }
  const step = Math.max(1, Math.ceil(labels.length / (width < 450 ? 4 : 6)));
  for (let i=0; i<labels.length; i+=step) { const label = labels[i].length === 10 ? labels[i].slice(8)+'.'+labels[i].slice(5,7) : labels[i].slice(5)+'.'+labels[i].slice(2,4);ctx.fillStyle='#737686';ctx.textAlign='center';ctx.fillText(label,x(i),height-14); }
  for (const line of lines) { ctx.beginPath();ctx.strokeStyle=line.color;ctx.lineWidth=2.5;ctx.setLineDash(line.dash ? [5,4] : []);let started=false;line.values.forEach((value,i)=>{if(typeof value !=='number' || !Number.isFinite(value)){started=false;return;}if(!started){ctx.moveTo(x(i),y(value));started=true;}else ctx.lineTo(x(i),y(value));});ctx.stroke();ctx.setLineDash([]); }
}
function renderIncoming() {
  const incoming = state.item?.inbound || [], active = incoming.map((row, index) => ({...row,index})).filter((row) => row.quantity !== 0);
  $('incoming').innerHTML = active.length ? active.map((row) => `<div class="incoming-row"><div><strong>${esc(fmt(row.quantity))} ${esc(state.item.unit || '')}</strong><br>Из источника: ${esc(row.eta ? dateText(row.eta) : row.eta_label || 'Дата неизвестна')}<br><span class="muted">${row.quantity == null ? 'Неизвестное количество блокирует расчёт.' : 'Сценарная дата не подтверждает приёмку.'}</span></div><label>Ожидаемая приёмка<input data-eta="${row.index}" type="date" value="${esc(row.eta || '')}" aria-label="Дата приёмки партии ${row.index + 1}"></label></div>`).join('') : '<p class="muted">В строке источника нет положительного количества товара в пути.</p>';
}
function readPolicy() {
  const form = $('policy-form'), policy = {};
  for (const name of ['lead_days','cover_days','safety_days','moq']) { const value = form.elements[name].value.trim(); policy[name] = value === '' ? null : Number(value); }
  policy.method = form.elements.method.value;
  for (const name of ['use_reported_stock','regular_only']) policy[name] = form.elements[name].checked;
  return policy;
}
async function calculate(event) {
  event.preventDefault(); if (!state.item || !$('policy-form').reportValidity()) return;
  const key = state.key, ticket = planGate.start(), eta_overrides = {};
  document.querySelectorAll('[data-eta]').forEach((input) => { const original = state.item.inbound[Number(input.dataset.eta)]?.eta; if (input.value && input.value !== original) eta_overrides[input.dataset.eta] = input.value; });
  state.plan = null; renderRecommendation(); $('calculate').disabled = true; $('form-status').textContent = 'Рассчитываем по выданным данным…'; message('');
  try {
    const plan = await json('/api/plan', { method:'POST', signal:ticket.signal, body:JSON.stringify({key,policy:readPolicy(),eta_overrides}) });
    if (!ticket.current() || state.key !== key) return;
    state.plan = plan; renderOverview(); renderRecommendation(); $('form-status').textContent = plan.status === 'provisional' ? 'Условный расчёт готов.' : 'Расчёт заблокирован — см. причины.';
    if (plan.status === 'provisional') { showTab('today'); message('Расчёт обновлён. Проверьте график, ограничения и рекомендацию.'); }
    else message((plan.reasons || ['Не хватает данных.']).join(' '), true);
  } catch (error) { if (ticket.current() && error.name !== 'AbortError') { message(error.message, true); $('form-status').textContent = 'Не удалось рассчитать.'; } }
  finally { if (ticket.current()) $('calculate').disabled = false; }
}
function renderRecommendation() {
  const plan = state.plan;
  if (!plan) { $('recommendation').innerHTML = '<div class="empty-state"><h2>Сначала рассчитайте потребность</h2><p>Укажите сроки и правила запаса для выбранного товара. Здесь появятся количество, дата и объяснение.</p><button type="button" class="primary" data-go="parameters">К параметрам</button></div>'; return; }
  const order = plan.order || {}, canAdd = plan.status === 'provisional' && order.quantity > 0 && plan.result_id;
  $('recommendation').innerHTML = `<div class="recommendation-title">${esc(plan.item.sku)} · ${esc(plan.item.name)}</div><span class="badge ${plan.status === 'provisional' ? '' : 'warning'}">${plan.status === 'provisional' ? 'Условный расчёт' : 'Нужны данные'}</span><p class="muted small">Поставщик: ${esc(plan.item.supplier)}</p><div class="order-stats"><div><span>Рекомендовано</span><strong>${esc(fmt(order.quantity))} ${esc(plan.item.unit || '')}</strong></div><div><span>Плановая приёмка</span><strong>${esc(dateText(order.arrival))}</strong></div><div><span>Кратность из таблицы</span><b>${esc(fmt(order.pack_multiple))}</b></div><div><span>Минимальная партия</span><b>${esc(fmt(order.moq))}</b></div></div><div class="explanation-block"><strong>${esc(methodText(plan.forecast?.method))}</strong>${esc(plan.forecast?.selection_reason || 'Сначала подтвердите необходимые входы.')}</div>${plan.reasons?.length ? `<ul class="warning-list">${plan.reasons.map((r) => `<li>${esc(r)}</li>`).join('')}</ul>` : ''}<details><summary>Все допущения расчёта</summary><ul class="warning-list">${(plan.assumptions || []).map((r) => `<li>${esc(r)}</li>`).join('')}</ul></details><button id="add-draft" type="button" class="primary full" ${canAdd ? '' : 'disabled'}>${state.drafts.has(state.key) ? 'Обновить позицию в черновике' : 'Добавить в черновик'}</button><p class="muted small" style="margin-top:12px">Черновик требует согласования. Данные не отправляются во внешние системы.</p>`;
}
function addDraft() {
  const plan = state.plan;
  if (!plan?.result_id || plan.status !== 'provisional' || !(plan.order?.quantity > 0)) return;
  state.drafts.set(state.key, plan); renderDraft(); renderRecommendation(); message('Позиция добавлена в черновик. Повторный расчёт этого товара заменяет её, а не удваивает заказ.');
}
function renderDraft() {
  const groups = new Map();
  for (const [key, plan] of state.drafts) { const name = plan.item.supplier; if (!groups.has(name)) groups.set(name, []); groups.get(name).push([key,plan]); }
  $('draft').innerHTML = groups.size ? [...groups].map(([supplier, rows]) => `<section class="draft-group"><h3>${esc(supplier)}</h3>${rows.map(([key,p]) => `<div class="draft-row"><div class="draft-name"><b>${esc(p.item.sku)}</b> · ${esc(p.item.name)}</div><div class="draft-bottom"><strong>${esc(fmt(p.order.quantity))} ${esc(p.item.unit || '')}</strong><button type="button" data-remove="${esc(key)}">Убрать</button></div><p class="muted small" style="margin:8px 0 0">Приёмка: ${esc(dateText(p.order.arrival))} · условный расчёт</p></div>`).join('')}</section>`).join('') : '<div class="empty-state">Черновик пока пуст.<br>Добавьте рассчитанную рекомендацию.</div>';
  $('draft-count').textContent = state.drafts.size || '';
  $('export-draft').disabled = !state.drafts.size; $('clear-draft').disabled = !state.drafts.size;
}
async function download(path, options, filename) {
  const response = await request(path, options), blob = await response.blob(), url = URL.createObjectURL(blob), link = document.createElement('a');
  link.href = url; link.download = filename; document.body.appendChild(link); link.click(); link.remove(); setTimeout(() => URL.revokeObjectURL(url), 1000);
}
async function exportDraft() {
  if (!state.drafts.size) return;
  $('export-draft').disabled = true;
  try { await download('/api/export', {method:'POST',body:JSON.stringify({result_ids:[...state.drafts.values()].map((p) => p.result_id)})}, 'smartbuyer-order-draft.csv'); message('CSV черновика сформирован сервером и передан браузеру.'); } catch (error) { message(error.message, true); } finally { $('export-draft').disabled = !state.drafts.size; }
}
async function loadBootstrap(reload = false) {
  $('reload').disabled = true; message(reload ? 'Перечитываем выданные Excel…' : 'Загружаем выданные таблицы и расчёты…');
  try {
    const result = await json(reload ? '/api/reload' : '/api/bootstrap', reload ? {method:'POST'} : undefined);
    itemGate.cancel(); planGate.cancel(); const key = state.key; state.bootstrap = result; state.item = null; state.plan = null; state.drafts.clear();
    $('source-date').textContent = `Срез источника: ${dateText(result.as_of)}`;
    const suppliers = document.createDocumentFragment(); const all = document.createElement('option'); all.value='';all.textContent='Все поставщики';suppliers.appendChild(all);
    for(const name of result.suppliers || []){const option=document.createElement('option');option.value=name;option.textContent=name;suppliers.appendChild(option);} $('supplier').replaceChildren(suppliers);
    $('source-files').innerHTML = table(['Файл', 'Поставщик', 'Чтение'], (result.sources || []).map((r) => [r.file,r.supplier,r.status === 'not_parsed' ? 'Сохранён как источник; не смешан с месячной базой' : 'Прочитаны сохранённые значения']));
    renderDraft(); displaySelector(key); message(reload ? 'Источники перечитаны. Старые расчёты и черновик сброшены.' : '');
  } catch(error) { message(error.message, true); } finally { $('reload').disabled=false; }
}
document.addEventListener('click', (event) => {
  const tab = event.target.closest('[data-tab],[data-go]'); if (tab) showTab(tab.dataset.tab || tab.dataset.go);
  if (event.target.closest('#add-draft')) addDraft();
  const remove = event.target.closest('[data-remove]'); if (remove) {state.drafts.delete(remove.dataset.remove);renderDraft();renderRecommendation();}
});
$('supplier').addEventListener('change', () => displaySelector());
$('search').addEventListener('input', () => displaySelector());
$('item-select').addEventListener('change', (event) => selectItem(event.target.value));
$('policy-form').addEventListener('submit', calculate);
$('policy-form').addEventListener('input', () => invalidatePlan(true));
$('policy-form').addEventListener('change', () => invalidatePlan(true));
$('reset-policy').addEventListener('click', () => resetPolicy(true));
$('reload').addEventListener('click', () => loadBootstrap(true));
$('export-draft').addEventListener('click', exportDraft);
$('clear-draft').addEventListener('click', () => {state.drafts.clear();renderDraft();renderRecommendation();message('Черновик очищен. Исходные данные не изменены.');});
$('forecast-export').addEventListener('click', async () => { if (!state.key) return; try { await download(`/api/forecast-export?key=${encodeURIComponent(state.key)}`, undefined, 'smartbuyer-forecast.csv');message('CSV прогноза передан браузеру. Это не заказ поставщику.'); } catch(error) {message(error.message,true);} });
window.addEventListener('resize', drawChart);
renderDraft(); loadBootstrap();
