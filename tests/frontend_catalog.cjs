// Integration checks use only the currently served issued dataset, never fabricated SKU rows.
// Start the local API before running: node tests/frontend_catalog.cjs
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const nodes = new Map();
function node(id) {
  if (!nodes.has(id)) nodes.set(id, { value:'',textContent:'',innerHTML:'',hidden:false,disabled:false,elements:{},dataset:{},addEventListener(){},setAttribute(){},removeAttribute(){},classList:{toggle(){}} });
  return nodes.get(id);
}
const context = vm.createContext({console,Intl,AbortController,Map,Number,URL,setTimeout,requestAnimationFrame(){},window:{addEventListener(){},scrollTo(){}},document:{getElementById:node,addEventListener(){},querySelectorAll:()=>[]}});
const source = fs.readFileSync(__dirname+'/../web/app.js','utf8').replace(/renderDraft\(\); loadBootstrap\(\);\s*$/, '');
vm.runInContext(source,context);
const run=(code)=>vm.runInContext(code,context);
(async()=>{
  const response=await fetch('http://127.0.0.1:8765/api/bootstrap'); assert.equal(response.status,200);
  context.issued=await response.json();run('state.bootstrap=issued');
  assert.equal(run('state.key'),null);assert.equal(run('state.tab'),'catalog');
  assert.equal(run('catalogItems(issued.items,"","").length'),3045);
  assert.equal(run('catalogItems(issued.items,"SystemElectric","").length'),582);
  assert.equal(run('catalogItems(issued.items,"IEK","").length'),2463);
  assert.equal(run('catalogItems(issued.items,"","ATN540126").length'),1);
  assert.equal(run('catalogItems(issued.items,"","ATN540126")[0].key'),'SystemElectric::ATN540126');
  assert.equal(run('catalogItems(issued.items,"","ATN540126")[0].stock.reported'),42);
  assert.equal(run('catalogItems(issued.items,"","ATN540126")[0].stock.reserved'),19);
  assert.equal(run('catalogItems(issued.items,"","ATN540126")[0].stock.free'),23);
  assert.equal(run('catalogItems(issued.items,"","ATN540126")[0].pack_multiple'),6);
  assert.equal(run('catalogPage(issued.items,1).rows.length'),25);
  assert.equal(run('catalogPage(issued.items,999).page'),122);
  assert.equal(run('catalogPage(issued.items,999).rows.length'),20);
  assert.equal(run('catalogPage(catalogItems(issued.items,"","ATN540126"),122).page'),1);
  node('search').value='ATN540126';run('renderCatalog()');
  assert.match(node('catalog-table').innerHTML,/data-select-item="SystemElectric::ATN540126"/);
  node('supplier').value='IEK';node('search').value='';run('renderCatalog()');
  assert.match(node('catalog-table').innerHTML,/Нет данных/);
  assert.equal(run('fmt(0)'),'0');assert.equal(run('fmt(-1)'),'-1');
  node('search').value='__not-an-issued-sku__';run('renderCatalog()');
  assert.match(node('catalog-table').innerHTML,/По этому фильтру товаров нет/);
  assert.equal(node('catalog-next').disabled,true);assert.equal(node('catalog-prev').disabled,true);
  console.log('PASS: 23 catalog checks against the issued dataset; browser QA is a separate check.');
})().catch((error)=>{console.error(error);process.exitCode=1;});
