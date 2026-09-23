// Regression suite for the web UI's automation editor (showAutomationEditor / automationEditorSpec).
//
// `upsert` full-replaces the stored record, and this form shows a handful of an automation's
// fields. Saving used to rebuild the whole spec from literals, so opening someone's automation to
// change one number silently rewrote everything the form does not show: a 400-turn cap reset to
// 50, a plan-mode automation promoted to a writable project run, a continued session dropped, the
// tool allowlist and write roots emptied. These tests extract the ACTUAL shipped functions and
// their module state out of harness/webui/index.html by brace-matching (as tests/render_test.js
// does) and run them in STRICT MODE, the way the page runs them (index.html:3004), against
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
          checked: false, disabled: false, textContent: '', onclick: null};
}

function harness() {
  const nodes = {};
  const live = [];                                  // elements currently in the grid
  const document = {
    createElement: () => {
      const el = {className: '', innerHTML: '',
                  get isConnected() { return live.includes(el); },
                  querySelectorAll: (sel) => sel === 'input, select, textarea' ?
                    Object.entries(nodes).filter(([id]) => id.startsWith('auto') &&
                      !['autoPreview', 'autoSave', 'autoClose', 'autoBudgetHelp'].includes(id))
                      .map(([, node]) => node) : [],
                  remove: () => { const i = live.indexOf(el); if (i >= 0) live.splice(i, 1); }};
      return el;
    },
    querySelectorAll: (sel) => sel === '.control-editor' ?
      live.filter(el => el.className === 'control-editor') : [],
  };
  nodes.activityGrid = {prepend: (el) => live.unshift(el),
    querySelector: (sel) => sel === '.control-editor' ?
      live.find(el => el.className === 'control-editor') || null : null};
  // Every id the editor writes to or reads from. A missing one is a real break, not a stub gap:
  // the page would throw the same way.
  for (const id of ['autoId', 'autoTask', 'autoProvider', 'autoTarget', 'autoWorkspace', 'autoRuns',
                    'autoTokens', 'autoCost', 'autoWall', 'autoActions', 'autoReadRoots',
                    'autoHosts', 'autoEnabled', 'autoExternal', 'autoPreview', 'autoSave',
                    'autoClose', 'autoBudgetHelp']) {
    nodes[id] = field();
  }
  const posts = [];
  // Strict mode and the state declaration are both load-bearing: without them a deleted `var`
  // would create an implicit global here and pass, while the shipped page threw on every open.
  const src = '"use strict";\n' +
              [grab('  var currentControlTab'),
               grab('function controlList(value)'),
               grab('  var automationEditorBase'),
               grab('  function automationEditorSpec()'),
               grab('  function currentControlEditor()'),
               grab('  function lockControlFields(form)'),
               grab('  function unlockControlFields(fields)'),
               grab('  function showAutomationEditor(spec)')].join('\n');
  const mod = {exports: {}};
  new Function('module', '$', 'document', 't', 'activityPost', 'activityNotice', 'loadActivity', 'activityPanel',
               src + '\nmodule.exports = {automationEditorSpec, showAutomationEditor};')(
    mod, (id) => { if (!nodes[id]) throw new Error('unknown element id: ' + id); return nodes[id]; },
    document, (en) => en,
    (url, body) => { posts.push({url, body}); return Promise.resolve({}); }, () => {},
    () => Promise.resolve(true), {hidden: false});
  return {nodes, posts, live, api: mod.exports};
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
const clone = (v) => JSON.parse(JSON.stringify(v));

// An automation somebody configured outside this panel: a read-only plan run on a continued
// session, a deliberate 400-turn cap, a narrow tool allowlist, a trigger predicate, a custom
// notification choice, and a budget key this build of the editor has never heard of.
const STORED = {
  automation_id: 'nightly', task: 'keep the release notes current', enabled: true,
  trigger: {provider: 'timer', every_s: 3600, catch_up: false, fire_immediately: true},
  context: {policy: 'continued', session_id: 's-1104'},
  workspace: {mode: 'isolated', label: 'notes'},
  execution: {mode: 'plan', model: 'claude-sonnet-5'},
  notifications: ['success'],
  permissions: {read_roots: ['/srv/notes'], write_roots: ['/srv/notes/drafts'],
                network_hosts: [], tools: ['read_file', 'grep'], desktop_targets: ['notes.app'],
                external_writes: false, current_workspace: false, webhook_ingest: false},
  budget: {max_turns: 400, max_actions: 7, max_retries: 4, max_runs_per_day: 24,
           max_model_tokens: 200000, max_cost_usd: 25, max_wall_s: 1800,
           max_future_key: 11},
};

{
  // Editing one visible number must not rewrite anything the form does not show.
  const {nodes, api} = harness();
  api.showAutomationEditor(clone(STORED));
  nodes.autoCost.value = '40';
  nodes.autoWall.value = '7200';
  const spec = api.automationEditorSpec(), budget = spec.budget;

  eq('edited cost is applied', budget.max_cost_usd, 40);
  eq('edited wall is applied', budget.max_wall_s, 7200);
  eq('untouched visible keys keep their stored values',
     [budget.max_runs_per_day, budget.max_model_tokens], [24, 200000]);
  eq('a stored turn cap survives an unrelated edit', budget.max_turns, 400);
  eq('a stored retry count survives an unrelated edit', budget.max_retries, 4);
  eq('a budget key this editor does not know is carried through', budget.max_future_key, 11);
  eq('the stored action budget is shown in the form', nodes.autoActions.value, '7');
  eq('an untouched action budget saves back as stored', budget.max_actions, 7);

  eq('a plan automation is not promoted to a project run', spec.execution.mode, 'plan');
  eq('an execution option the form does not show is kept', spec.execution.model, 'claude-sonnet-5');
  eq('a continued context is not reset to fresh', spec.context, STORED.context);
  eq('notification choices survive the edit', spec.notifications, ['success']);
  eq('workspace options beyond the mode survive', spec.workspace,
     {mode: 'isolated', label: 'notes'});
  eq('write roots are not emptied', spec.permissions.write_roots, ['/srv/notes/drafts']);
  eq('the tool allowlist is not emptied', spec.permissions.tools, ['read_file', 'grep']);
  eq('desktop targets are not emptied', spec.permissions.desktop_targets, ['notes.app']);
  eq('trigger fields the form does not show survive',
     [spec.trigger.catch_up, spec.trigger.fire_immediately, spec.trigger.every_s],
     [false, true, 3600]);

  // Dry run then Save: the second read must not differ from the first.
  eq('reading the form twice yields the same spec', api.automationEditorSpec(), spec);

  t('the help names the action ceiling and the turn cap it actually carries',
    nodes.autoBudgetHelp.textContent.includes('tool-action') &&
    nodes.autoBudgetHelp.textContent.includes('400'));
}

{
  // What Save posts is what matters; every other assertion here reads the form directly.
  const {nodes, posts, api} = harness();
  api.showAutomationEditor(clone(STORED));
  nodes.autoActions.value = '250';
  nodes.autoSave.onclick();

  eq('save posts one upsert', [posts.length, posts.length && posts[0].url],
     [1, '/api/automations/upsert']);
  const sent = posts[0].body.spec;
  eq('the posted budget carries the edited action cap', sent.budget.max_actions, 250);
  eq('the posted budget keeps the stored turn cap', sent.budget.max_turns, 400);
  eq('the posted spec is still a plan run', sent.execution.mode, 'plan');
  eq('the posted permissions keep the tool allowlist', sent.permissions.tools,
     ['read_file', 'grep']);
}

{
  const {nodes, api} = harness();
  api.showAutomationEditor(null);              // "New automation"
  nodes.autoId.value = 'digest';
  nodes.autoTask.value = 'summarize new issues';
  const spec = api.automationEditorSpec();

  eq('a new automation has no hard turn ceiling', spec.budget.max_turns, 0);
  eq('a new automation starts from the default action budget', spec.budget.max_actions, 100);
  t('a new automation still carries positive metered budgets',
    spec.budget.max_wall_s > 0 && spec.budget.max_model_tokens > 0 &&
    spec.budget.max_cost_usd > 0 && spec.budget.max_runs_per_day > 0);
  t('a new automation invents no retry cap of its own', !('max_retries' in spec.budget));
  eq('a new automation is a fresh project run', [spec.execution.mode, spec.context.policy],
     ['project', 'fresh']);
  eq('a new automation keeps the default notification choices', spec.notifications,
     ['failure', 'needs_you']);
  t('a new automation claims no authority it was not given',
    spec.permissions.webhook_ingest === false && spec.permissions.current_workspace === false &&
    spec.permissions.external_writes === false);
  t('the help says what bounds the work and that no turn ceiling is set',
    nodes.autoBudgetHelp.textContent.includes('tool-action') &&
    nodes.autoBudgetHelp.textContent.includes('No model-turn ceiling'));
}

{
  // Sequential edits: opening a second automation must not leak the first one's hidden fields.
  const {live, api} = harness();
  api.showAutomationEditor(clone(STORED));
  api.automationEditorSpec();
  api.showAutomationEditor({automation_id: 'other', task: 'x', trigger: {provider: 'timer'},
                            budget: {max_wall_s: 600}, permissions: {}, enabled: false});
  const spec = api.automationEditorSpec();

  eq('only one editor is on the page at a time', live.length, 1);
  eq('a different automation does not inherit the previous turn cap', spec.budget.max_turns, 0);
  eq('nor its tightened action cap', spec.budget.max_actions, 100);
  t('nor its plan/continued execution', !spec.execution && !spec.context);
  t('nor its tools and write roots',
    !spec.permissions.tools && !spec.permissions.write_roots);
  eq('nor its trigger predicate fields', spec.trigger, {provider: 'timer', every_s: 3600});
}

{
  // Authority is not a display field. Leaving the trigger type alone must keep exactly what was
  // accepted; changing it may build a trigger for the new type but must not carry stale targets.
  const {nodes, api} = harness();
  const hook = {automation_id: 'triage', task: 'triage events', enabled: true,
                trigger: {provider: 'webhook', predicate: {type: 'contains', text: 'error'}},
                workspace: {mode: 'current'}, budget: {},
                permissions: {read_roots: [], write_roots: [], network_hosts: [], tools: [],
                              desktop_targets: [], external_writes: false,
                              current_workspace: true, webhook_ingest: true}};
  api.showAutomationEditor(clone(hook));
  nodes.autoCost.value = '9';
  const kept = api.automationEditorSpec();
  t('an unchanged webhook keeps its accepted ingest authority',
    kept.permissions.webhook_ingest === true);
  t('an unchanged current workspace keeps its accepted authority',
    kept.permissions.current_workspace === true && kept.workspace.mode === 'current');
  eq('an unchanged webhook keeps its trigger predicate', kept.trigger.predicate,
     {type: 'contains', text: 'error'});

  // The same automation moved off webhook: the ingest authority goes with the trigger.
  nodes.autoProvider.value = 'timer';
  nodes.autoTarget.value = '900';
  const moved = api.automationEditorSpec();
  t('dropping the webhook trigger drops the ingest authority',
    moved.permissions.webhook_ingest === false);
  eq('the new timer trigger carries no webhook leftovers', moved.trigger,
     {provider: 'timer', every_s: 900});
}

{
  const {nodes, api} = harness();
  api.showAutomationEditor({automation_id: 'watch', task: 'watch the page', enabled: false,
                            trigger: {provider: 'page', url: 'https://example.com',
                                      predicate: {type: 'changed'}},
                            workspace: {mode: 'isolated'}, budget: {},
                            permissions: {network_hosts: ['example.com'], webhook_ingest: false,
                                          current_workspace: false}});
  nodes.autoProvider.value = 'file';
  nodes.autoTarget.value = '/srv/notes/CHANGELOG.md';
  const spec = api.automationEditorSpec();

  eq('switching page to file builds a file trigger with no stale URL', spec.trigger,
     {provider: 'file', path: '/srv/notes/CHANGELOG.md', predicate: {type: 'changed'}});
  t('switching the trigger type grants no webhook authority',
    spec.permissions.webhook_ingest === false);

  // Turning the workspace back to isolated narrows the authority that granted it.
  nodes.autoWorkspace.value = 'current';
  t('choosing the current workspace authorizes it',
    api.automationEditorSpec().permissions.current_workspace === true);
}

{
  const {nodes, live, api} = harness();
  api.showAutomationEditor(clone(STORED));
  nodes.autoClose.onclick();
  eq('closing the editor takes it off the page', live.length, 0);
}

console.log((fail ? 'FAILED ' : 'ok ') + pass + ' passed, ' + fail + ' failed');
process.exit(fail ? 1 : 0);
