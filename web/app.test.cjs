// Helpers only; browser coverage is a separate real-UI check, not claimed here.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, { value: '', checked: false, textContent: '', innerHTML: '', hidden: false, disabled: false, dataset: {}, elements: {}, addEventListener() {}, classList: { toggle() {} } });
  return nodes.get(id);
}
const context = vm.createContext({
  console, Intl, AbortController, Map, setTimeout, Number, URL,
  document: { getElementById: node, addEventListener() {}, querySelectorAll: () => [] },
  window: { addEventListener() {} }, requestAnimationFrame() {},
});
let source = fs.readFileSync(__dirname + '/app.js', 'utf8');
source = source.replace(/renderDraft\(\); loadBootstrap\(\);\s*$/, '');
vm.runInContext(source, context);
const run = (code) => vm.runInContext(code, context);
assert.equal(run('esc("<img src=x onerror=alert(1)>")'), '&lt;img src=x onerror=alert(1)&gt;');
assert.equal(run('fmt(null)'), 'Нет данных');
assert.equal(run('fmt(0)'), '0');
assert.equal(run('dateText(null)'), 'Не задана');
assert.equal(run('table(["<b>"], [["<script>"]]).includes("<script>")'), false);
assert.equal(run('(()=>{const g=gate(),a=g.start(),b=g.start();return !a.current()&&a.signal.aborted&&b.current()&&!b.signal.aborted})()'), true);
assert.equal(run('(()=>{const g=gate(),a=g.start();g.cancel();return !a.current()&&a.signal.aborted})()'), true);
for (const key of ['lead_days','cover_days','safety_days','moq','method','use_reported_stock','regular_only']) node('policy-form').elements[key] = { value: '', checked: false };
assert.equal(run('readPolicy().lead_days'), null);
node('policy-form').elements.moq.value = '0';
assert.equal(run('readPolicy().moq'), 0);
assert.equal(run('readPolicy().use_reported_stock'), false);
// Symbolic identifiers test result replacement, not a fabricated business scenario.
run('state.key="supplier::sku"; state.item={}; renderRecommendation=()=>{}; renderDraft=()=>{}; message=()=>{}; state.plan={result_id:"old",status:"blocked",order:{quantity:1}}; addDraft()');
assert.equal(run('state.drafts.size'), 0);
run('state.plan={result_id:"old",status:"provisional",order:{quantity:1}};addDraft();state.plan={result_id:"new",status:"provisional",order:{quantity:1}};addDraft()');
assert.equal(run('state.drafts.size'), 1);
assert.equal(run('state.drafts.get(state.key).result_id'), 'new');
run('renderOverview=()=>{};invalidatePlan()');
assert.equal(run('state.plan'), null);
run('addDraft()');
assert.equal(run('state.drafts.size'), 1);
// Policy/ETA editing must invalidate the old exported quantity, not just the visible plan.
run('state.drafts.set("other::sku", {result_id:"other"});invalidatePlan(true)');
assert.equal(run('state.drafts.has(state.key)'), false);
assert.equal(run('state.drafts.has("other::sku")'), true);
(async () => {
  context.fetch = async () => ({ ok: false, status: 400, json: async () => ({error:{message:'Проверьте параметры'}}) });
  await assert.rejects(run('request("/api/plan")'), /Проверьте параметры/);
  console.log('PASS: 18 frontend helper assertions; no business source fixtures created.');
})().catch((error) => { console.error(error); process.exitCode = 1; });
