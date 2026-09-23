// Regression suite for the web UI's automation budget roundtrip (showAutomationEditor /
// automationEditorSpec).
//
// The editor shows four budget keys and the stored budget has more. Saving used to rebuild the
// whole budget from literals, so opening someone's 400-turn automation and changing the cost cap
// silently reset the turn cap to 50 and the action cap to 100 — a full-replace upsert, no merge
// anywhere behind it. These tests extract the ACTUAL shipped functions out of
// harness/webui/index.html by brace-matching (as tests/render_test.js does) and run them against
// hand-built form fields (as tests/browser_ext_test.js does for the injected page functions), so
// the check cannot quietly disagree with what ships.
//
//   node tests/automation_budget_test.js        (exit 0 = all pass)
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'harness', 'webui', 'index.html'), 'utf8')
               .replace(/\r\n/g, '\n');
const lines = html.split('\n');

function grab(startPat) {
  const start = lines.findIndex(l => l.includes(startPat));
  if (start < 0) throw new Error('not found in index.html: ' + startPat);
  let depth = 0, started = false;
  const out = [];
  for (let j = start; j < lines.length; j++) {
    out.push(lines[j]);
    const code = lines[j].split('//')[0];
    for (const ch of code) { if (ch === '{') { depth++; started = true; } else if (ch === '}') depth--; }
    if (started && depth <= 0) break;
  }
  return out.join('\n');
}

// A form field. `.value` stringifies on assignment exactly as a real input does — the editor
// writes numbers into it and then reads them back with .trim(), so a plain property would make
// the stub behave differently from the browser.
function field() {
  let value = '';
  return {get value() { return value; }, set value(v) { value = String(v == null ? '' : v); },
          checked: false, textContent: '', onclick: null};
}

function harness() {
  const nodes = {};
  const editor = {className: '', innerHTML: '', get id() { return ''; }};
  const document = {
    createElement: () => editor,
    querySelectorAll: () => [],
  };
  const grid = {prepend: () => {}};
  nodes.activityGrid = grid;
  // Every id the editor writes to or reads from. A missing one is a real break, not a stub gap:
  // the page would throw the same way.
  for (const id of ['autoId', 'autoTask', 'autoProvider', 'autoTarget', 'autoWorkspace', 'autoRuns',
                    'autoTokens', 'autoCost', 'autoWall', 'autoReadRoots', 'autoHosts',
                    'autoEnabled', 'autoExternal', 'autoPreview', 'autoSave', 'autoBudgetHelp']) {
    nodes[id] = field();
  }
  const src = [grab('function controlList(value)'),
               grab('  function automationEditorSpec()'),
               grab('  function showAutomationEditor(spec)')].join('\n');
  const mod = {exports: {}};
  new Function('module', '$', 'document', 't', 'activityPost', 'activityNotice', 'loadActivity',
               src + '\nmodule.exports = {automationEditorSpec, showAutomationEditor};')(
    mod, (id) => { if (!nodes[id]) throw new Error('unknown element id: ' + id); return nodes[id]; },
    document, (en) => en, () => Promise.resolve({}), () => {}, () => {});
  return {nodes, api: mod.exports};
}

let pass = 0, fail = 0;
function eq(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) console.log('  FAIL ' + name + '\n        got:  ' + JSON.stringify(got) +
                       '\n        want: ' + JSON.stringify(want));
  ok ? pass++ : fail++;
}
function t(name, cond) {
  if (cond) { pass++; return; }
  fail++;
  console.log('  FAIL ' + name);
}

// An automation somebody configured outside this panel: a deliberate 400-turn cap, a tightened
// action cap, a custom retry count, and a key this build of the editor has never heard of.
const STORED = {
  automation_id: 'nightly', task: 'keep the release notes current', enabled: true,
  trigger: {provider: 'timer', every_s: 3600}, workspace: {mode: 'isolated'},
  permissions: {read_roots: ['/srv/notes'], network_hosts: []},
  budget: {max_turns: 400, max_actions: 7, max_retries: 4, max_runs_per_day: 24,
           max_model_tokens: 200000, max_cost_usd: 25, max_wall_s: 1800,
           max_future_key: 11},
};

{
  const {nodes, api} = harness();
  api.showAutomationEditor(JSON.parse(JSON.stringify(STORED)));
  // The person changes only what the form shows.
  nodes.autoCost.value = '40';
  nodes.autoWall.value = '7200';
  const budget = api.automationEditorSpec().budget;

  eq('edited cost is applied', budget.max_cost_usd, 40);
  eq('edited wall is applied', budget.max_wall_s, 7200);
  eq('untouched visible keys keep their stored values',
     [budget.max_runs_per_day, budget.max_model_tokens], [24, 200000]);
  eq('a stored turn cap survives an unrelated edit', budget.max_turns, 400);
  eq('a stored action cap survives an unrelated edit', budget.max_actions, 7);
  eq('a stored retry count survives an unrelated edit', budget.max_retries, 4);
  eq('a budget key this editor does not know is carried through', budget.max_future_key, 11);
  t('the existing turn cap is explained rather than hidden',
    nodes.autoBudgetHelp.textContent.includes('budgets runs out') &&
    nodes.autoBudgetHelp.textContent.includes('model turns'));
}

{
  const {nodes, api} = harness();
  api.showAutomationEditor(null);              // "New automation"
  nodes.autoId.value = 'digest';
  nodes.autoTask.value = 'summarize new issues';
  const spec = api.automationEditorSpec();

  eq('a new automation has no hard turn ceiling', spec.budget.max_turns, 0);
  t('a new automation still carries positive metered budgets',
    spec.budget.max_wall_s > 0 && spec.budget.max_model_tokens > 0 &&
    spec.budget.max_cost_usd > 0 && spec.budget.max_runs_per_day > 0);
  t('a new automation invents no action or retry cap of its own',
    !('max_actions' in spec.budget) && !('max_retries' in spec.budget));
  t('the help says the visible budget is what bounds the work',
    nodes.autoBudgetHelp.textContent.includes('budgets runs out') &&
    !nodes.autoBudgetHelp.textContent.includes('model turns'));
}

{
  // Opening a second automation must not leak the first one's hidden budget into it.
  const {api} = harness();
  api.showAutomationEditor(JSON.parse(JSON.stringify(STORED)));
  api.automationEditorSpec();
  api.showAutomationEditor({automation_id: 'other', task: 'x', trigger: {provider: 'timer'},
                            budget: {max_wall_s: 600}, permissions: {}});
  const budget = api.automationEditorSpec().budget;

  eq('a different automation does not inherit the previous turn cap', budget.max_turns, 0);
  t('nor its hidden action cap', !('max_actions' in budget));
}

console.log((fail ? 'FAILED ' : 'ok ') + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
