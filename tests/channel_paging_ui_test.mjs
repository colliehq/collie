// Execute the shipped asynchronous paging functions with controllable requests.
// DOM rendering is irrelevant to these cursor/response races and is not simulated.
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';

const source = readFileSync(new URL('../harness/webui/communications.html', import.meta.url), 'utf8');
const functions = source.slice(source.indexOf('async function loadItems('), source.indexOf('function pager('));
assert.ok(functions.includes('async function turn('));
function fixture() {
  const notices = [], requests = [];
  const context = vm.createContext({selected:'mail',tab:'events',listView:'mail:events',pageTrail:[],generation:0,
    items:[{id:'new'}],hasMore:true,nextBefore:10,paging:false,itemId:'new',
    renderItems(){}, notice(message){notices.push(message);},
    readSection(section, connection, before){
      return new Promise((resolve,reject)=>requests.push({section,connection,before,resolve,reject}));
    }});
  vm.runInContext(functions, context);
  return {context,notices,requests};
}
const plain = value => JSON.parse(JSON.stringify(value));

{
  const {context:c,requests:r} = fixture();
  const pending = c.turn([10]);
  assert.deepEqual(plain(c.pageTrail), [], 'a cursor is not committed before its response');
  assert.deepEqual(plain(c.items), [{id:'new'}]);
  r[0].resolve({events:[{id:'older'}],has_more:false,next_before:0});
  await pending;
  assert.deepEqual(plain(c.pageTrail), [10]);
  assert.deepEqual(plain(c.items), [{id:'older'}]);
  const refresh = c.loadItems();
  assert.equal(r[1].before,10,'refresh keeps the selected historical page');
  r[1].resolve({events:[{id:'older-updated'}],has_more:false,next_before:0});
  await refresh;
  assert.deepEqual(plain(c.items), [{id:'older-updated'}]);
}
{
  const {context:c,requests:r,notices} = fixture();
  const pending = c.turn([10]);
  r[0].reject(new Error('request failed'));
  await pending;
  assert.deepEqual(plain(c.pageTrail), [], 'failure retains the previous page and its rows');
  assert.deepEqual(plain(c.items), [{id:'new'}]);
  assert.equal(c.paging,false);
  assert.deepEqual(notices,['request failed']);
}
{
  const {context:c,requests:r} = fixture();
  const first=c.loadItems([10]), second=c.loadItems([5]);
  r[1].resolve({events:[{id:'wanted'}],has_more:false,next_before:0});
  await second;
  r[0].resolve({events:[{id:'stale'}],has_more:true,next_before:5});
  await first;
  assert.deepEqual(plain(c.pageTrail), [5]);
  assert.deepEqual(plain(c.items), [{id:'wanted'}]);
}
{
  const {context:c,requests:r} = fixture();
  const pending=c.turn([10]);
  c.selected='other';c.pageTrail=[];c.items=[];c.generation++;
  r[0].resolve({events:[{id:'wrong-account'}],has_more:true,next_before:5});
  await pending;
  assert.deepEqual(plain(c.pageTrail), []);
  assert.deepEqual(plain(c.items), []);
}
{
  const {context:c,requests:r} = fixture();
  c.tab='results';
  const pending=c.loadItems();
  assert.deepEqual(plain(c.items), [], 'old inbox rows cannot act on the results tab while loading');
  assert.equal(c.hasMore,false,'the previous tab cursor cannot be clicked during the new request');
  assert.equal(c.nextBefore,0);
  r[0].resolve({results:[{id:'reply'}],has_more:false,next_before:0});
  await pending;
  assert.deepEqual(plain(c.items), [{id:'reply'}]);
}
console.log('paging: success, refresh, failure, response order, account and tab switch pass');
