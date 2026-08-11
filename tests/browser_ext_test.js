// Regression suite for the collie browser extension's page-side logic — the accessibility snapshot,
// the wait/scroll steps, the script step machinery, and the space/frame ref parsing.
//
// It extracts the ACTUAL shipped functions out of harness/browser_ext/background.js by brace-matching
// and runs them against a hand-built DOM. That is the only honest way to test them: they are injected
// into the page by chrome.scripting, so they must be self-contained, and slicing them out of the real
// file is what proves they still are. (Normalise CRLF first — the repo checks out CRLF on Windows and
// the slicer silently returns nothing otherwise.)
//
//   node tests/browser_ext_test.js        (exit 0 = all pass)
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'harness', 'browser_ext', 'background.js'), 'utf8')
              .replace(/\r\n/g, '\n');
const lines = src.split('\n');

function grab(startPat) {
  const start = lines.findIndex(l => l.includes(startPat));
  if (start < 0) throw new Error('not found in background.js: ' + startPat);
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

// Taken from the file rather than restated here, so the test cannot quietly disagree with it.
function constant(name) {
  const line = lines.find(l => l.startsWith('const ' + name + ' = '));
  if (!line) throw new Error('not found in background.js: const ' + name);
  return line;
}

const body = [
  constant('DEFAULT_SPACE'),
  grab('const KEYS = {'),
  grab('const MODBIT = {'),
  grab('function keySpec(name)'),
  grab('function modMask(mods)'),
  grab('function pageFrameBox(src, nth)'),
  grab('function pageSnapshot(maxN, opts)'),
  grab('function pageHas(text, selector)'),
  grab('function pageScroll(to, by, ref)'),
  grab('function stepSummary(r)'),
  grab('function stepFailed(r)'),
  grab('function splitFrameRef(ref)'),
  grab('function spaceOf(cmd)'),
].join('\n');

let pass = 0, fail = 0;
function t(name, cond) {
  if (cond) { pass++; return; }
  fail++;
  console.log('  FAIL ' + name);
}
function eq(name, got, want) {
  const ok = JSON.stringify(got) === JSON.stringify(want);
  if (!ok) console.log('  FAIL ' + name + '\n        got:  ' + JSON.stringify(got) +
                       '\n        want: ' + JSON.stringify(want));
  ok ? pass++ : fail++;
}

// --- the smallest DOM these functions actually touch ---------------------------------------------
const VIEW = { w: 1000, h: 800 };
function el(tag, opts) {
  opts = opts || {};
  const kids = opts.children || [];
  const node = {
    tagName: tag.toUpperCase(),
    _attrs: opts.attrs || {},
    children: kids,
    childNodes: (opts.text ? [{ nodeType: 3, nodeValue: opts.text }] : []),
    innerText: opts.innerText !== undefined ? opts.innerText : (opts.text || ''),
    value: opts.value,
    id: (opts.attrs && opts.attrs.id) || '',
    disabled: !!opts.disabled,
    tabIndex: opts.tabIndex === undefined ? -1 : opts.tabIndex,
    shadowRoot: opts.shadowRoot || null,
    contentDocument: opts.contentDocument || null,
    isConnected: true,
    _rect: opts.rect || { width: 120, height: 24, top: 40, left: 20, bottom: 64, right: 140 },
    _style: opts.style || { visibility: 'visible', display: 'block', opacity: '1' },
    getAttribute(n) { return this._attrs[n] === undefined ? null : this._attrs[n]; },
    hasAttribute(n) { return this._attrs[n] !== undefined; },
    getBoundingClientRect() { return this._rect; },
    scrollIntoView() {},
    closest() { return null; },
  };
  return node;
}
const OFFSCREEN = { width: 100, height: 20, top: 4000, left: 20, bottom: 4020, right: 120 };

function run(root, max, opts, frames) {
  const win = {};
  const doc = {
    documentElement: root,
    body: { innerText: 'page body text here', scrollHeight: 5000 },
    getElementById() { return null; },
    querySelector() { return null; },
    querySelectorAll() { return frames || []; },
  };
  const fn = new Function(
    'window', 'document', 'getComputedStyle', 'CSS', 'innerWidth', 'innerHeight', 'location',
    body + '\nreturn { pageSnapshot, pageHas, pageScroll, pageFrameBox, stepSummary, stepFailed, ' +
           'splitFrameRef, spaceOf, keySpec, modMask };'
  )(win, doc, (e) => e._style, { escape: (s) => s }, VIEW.w, VIEW.h, { href: 'https://example.test/p' });
  return { api: fn, win, doc, out: fn.pageSnapshot(max, opts) };
}

// --- snapshot: the basics -------------------------------------------------------------------------
{
  const root = el('html', { children: [el('body', { children: [
    el('button', { text: 'Save' }),
    el('a', { attrs: { href: '/next' }, text: 'Next page' }),
    el('input', { attrs: { type: 'text', 'aria-label': 'Email' } }),
  ] })] });
  const { out } = run(root, 200, {});
  t('control lines are ref-indexed', out.snapshot.includes('[e1] button "Save"'));
  t('links keep their name', out.snapshot.includes('link "Next page"'));
  t('inputs read as textbox', out.snapshot.includes('textbox "Email"'));
  eq('count is the number of refs', out.count, 3);
  eq('nothing dropped under the cap', out.truncated, false);
  t('url is reported', out.url === 'https://example.test/p');
}

// --- snapshot: it is a TREE, and landmarks/headings carry the page's meaning -----------------------
{
  const root = el('html', { children: [el('body', { children: [
    el('main', { children: [
      el('h1', { innerText: 'Checkout' }),
      el('button', { text: 'Pay now' }),
    ] }),
  ] })] });
  const { out } = run(root, 200, {});
  const ls = out.snapshot.split('\n');
  t('landmark is listed', ls.some(l => l.trim() === 'main'));
  t('heading is listed unnumbered', ls.some(l => l.trim() === 'h1 "Checkout"'));
  t('control nests under its landmark', ls.some(l => /^\s+\[e1\] button "Pay now"/.test(l)));
  t('nesting is indentation', ls.find(l => l.includes('[e1]')).startsWith('  '));
}

// --- snapshot: an empty landmark is not worth a line ----------------------------------------------
{
  const root = el('html', { children: [el('body', { children: [
    el('nav', { children: [] }),
    el('button', { text: 'Go' }),
  ] })] });
  const { out } = run(root, 200, {});
  t('landmark with nothing kept inside is dropped', !out.snapshot.includes('nav'));
  t('the real control survives', out.snapshot.includes('button "Go"'));
}

// --- snapshot: identical siblings collapse, and every ref still works ------------------------------
{
  const kids = [];
  for (let i = 0; i < 7; i++) kids.push(el('button', { text: 'Reply' }));
  const root = el('html', { children: [el('body', { children: kids })] });
  const { out, win } = run(root, 200, {});
  t('a run of identical controls is one line', out.snapshot.split('\n').length === 1);
  t('the line says how many', out.snapshot.includes('×7'));
  t('the line says which refs it covers', out.snapshot.includes('identical siblings: e1–e7'));
  eq('every collapsed item still counted', out.count, 7);
  t('every collapsed item is still addressable', win.__collieRefs.get('e7') !== undefined);
  t('collapsed refs are distinct elements', win.__collieRefs.get('e1') !== win.__collieRefs.get('e7'));
}

// --- snapshot: when the cap bites, IMPORTANCE decides — not document order -------------------------
{
  const filler = [];
  for (let i = 0; i < 12; i++) filler.push(el('button', { text: 'Filler ' + i, rect: OFFSCREEN }));
  const dialog = el('div', { attrs: { role: 'dialog' }, children: [
    el('button', { text: 'Confirm flair' }),
  ] });
  const root = el('html', { children: [el('body', { children: filler.concat([dialog]) })] });
  const { out } = run(root, 4, {});
  t('the just-opened dialog survives the cut', out.snapshot.includes('Confirm flair'));
  t('the cut is reported', out.truncated === true && out.dropped > 0);
}

// --- snapshot: on-screen beats off-screen ----------------------------------------------------------
{
  const root = el('html', { children: [el('body', { children: [
    el('button', { text: 'Below the fold', rect: OFFSCREEN }),
    el('button', { text: 'On screen' }),
  ] })] });
  const { out } = run(root, 1, {});
  t('the visible control is the one kept', out.snapshot.includes('On screen'));
}

// --- snapshot: iframes ------------------------------------------------------------------------------
{
  const inner = el('html', { children: [el('body', { children: [el('button', { text: 'Inner pay' })] })] });
  const root = el('html', { children: [el('body', { children: [
    el('iframe', { contentDocument: { documentElement: inner } }),
  ] })] });
  const { out } = run(root, 200, {});
  t('same-origin iframe contents are walked', out.snapshot.includes('Inner pay'));
  eq('a readable frame is not reported as unreachable', out.frames, 0);
}
{
  const root = el('html', { children: [el('body', { children: [
    el('iframe', { attrs: { src: 'https://pay.example.com/f' } }),   // contentDocument null = cross-origin
  ] })] });
  const { out } = run(root, 200, {});
  t('cross-origin iframe is REPORTED, not silently skipped', out.snapshot.includes('cross-origin'));
  t('its src is shown', out.snapshot.includes('https://pay.example.com/f'));
  eq('and counted', out.frames, 1);
}

// --- snapshot: text is opt-in ----------------------------------------------------------------------
{
  const root = el('html', { children: [el('body', { children: [
    el('p', { text: 'Order total is $42.00' }),
    el('button', { text: 'Buy' }),
  ] })] });
  t('no prose by default', !run(root, 200, {}).out.snapshot.includes('42.00'));
  t('prose when asked for', run(root, 200, { text: true }).out.snapshot.includes('Order total is $42.00'));
}
{
  // The web writes its status lines in divs. A tag whitelist lost exactly the text that says whether
  // the last action worked, so text:true takes any element's own text — but never a script's.
  const root = el('html', { children: [el('body', { children: [
    el('div', { text: 'frame-clicked' }),
    el('script', { text: 'var secret = 1;' }),
  ] })] });
  const { out } = run(root, 200, { text: true });
  t('div text is included', out.snapshot.includes('frame-clicked'));
  t('script contents are not', !out.snapshot.includes('secret'));
}

// --- snapshot: invisible controls stay out ----------------------------------------------------------
{
  const root = el('html', { children: [el('body', { children: [
    el('button', { text: 'Hidden', style: { visibility: 'hidden', display: 'block', opacity: '1' } }),
    el('button', { text: 'Shown' }),
  ] })] });
  const { out } = run(root, 200, {});
  t('visibility:hidden is not offered as clickable', !out.snapshot.includes('Hidden'));
  eq('only the visible one is counted', out.count, 1);
}

// --- shadow DOM (open and the closed-root WeakMap shadow.js keeps) ----------------------------------
{
  const shadowKid = el('button', { text: 'In shadow' });
  const host = el('div', { shadowRoot: { children: [shadowKid] } });
  const root = el('html', { children: [el('body', { children: [host] })] });
  t('open shadow roots are walked', run(root, 200, {}).out.snapshot.includes('In shadow'));
}

// --- wait_for's page probe --------------------------------------------------------------------------
{
  const { api } = run(el('html', { children: [] }), 200, {});
  eq('text found in the body', api.pageHas('body text', ''), { found: true });
  eq('text genuinely absent', api.pageHas('nothing like this', ''), { found: false });
  t('neither text nor selector is an error, not a false negative',
     !!api.pageHas('', '').error);
}

// --- scroll -------------------------------------------------------------------------------------------
{
  const { api, win } = run(el('html', { children: [] }), 200, {});
  let scrolledTo = null;
  win.scrollTo = (x, y) => { scrolledTo = y; win.scrollY = y; };
  win.scrollBy = (x, y) => { win.scrollY = (win.scrollY || 0) + y; };
  win.scrollY = 0;
  const r = api.pageScroll('bottom', 0, '');
  t('scrolling to the bottom reports where it landed', r.scrolled === 'bottom' && scrolledTo === 5000);
}

// --- script step machinery ---------------------------------------------------------------------------
{
  const { api } = run(el('html', { children: [] }), 200, {});
  t('an error stops the script', api.stepFailed({ error: 'no such element' }) === true);
  t('a write that did not land stops the script', api.stepFailed({ typed: 'x', landed: false }) === true);
  t('a landed write does not', api.stepFailed({ typed: 'x', landed: true }) === false);
  t('a click whose inner result errored stops it', api.stepFailed({ click: { error: 'gone' }, page: '…' }) === true);
  t('a refused upload stops it', api.stepFailed({ attached: false, uploaded: 0 }) === true);
  t('plain text results are fine', api.stepFailed('the whole page text') === false);
  t('null is fine', api.stepFailed(null) === false);

  const sum = api.stepSummary({ click: { clicked: 'Submit', trusted: true, matches: 3 }, page: 'x'.repeat(9000) });
  t('click summary keeps the useful bits', sum.clicked === 'Submit' && sum.trusted === true && sum.matches === 3);
  t('click summary drops the page payload', JSON.stringify(sum).length < 200);
  const long = api.stepSummary('y'.repeat(5000));
  t('a long text step is summarised, not echoed', long.text.length <= 201);
  const bad = api.stepSummary({ error: 'nope' });
  t('a failed step summarises as not ok', bad.ok === false && bad.error === 'nope');
}

// --- where a cross-origin frame sits, so a click inside it can be placed for real ---------------------
{
  const BORDERED = { borderLeftWidth: '2px', borderTopWidth: '2px',
                     paddingLeft: '3px', paddingTop: '4px',
                     visibility: 'visible', display: 'block', opacity: '1' };
  const frame = el('iframe', {
    attrs: { src: 'https://pay.example.com/f' },
    style: BORDERED,
    rect: { width: 400, height: 200, top: 100, left: 50, bottom: 300, right: 450 },
  });
  frame.src = 'https://pay.example.com/f';
  const { api } = run(el('html', { children: [] }), 200, {}, [frame]);
  const box = api.pageFrameBox('https://pay.example.com/f', 0);
  eq('frame origin skips the border and padding', [box.x, box.y], [50 + 2 + 3, 100 + 2 + 4]);
  t('an on-screen frame reports so', box.inView === true);
  t('an unknown src still measures the only frame there is',
     api.pageFrameBox('https://other.example/x', 0).x === 55);
  t('no iframes at all is an error, not a wrong coordinate',
     !!run(el('html', { children: [] }), 200, {}, []).api.pageFrameBox('https://pay.example.com/f', 0).error);
}
{
  // Off-screen frames are scrolled to before being measured — a click can only land in the viewport.
  let scrolled = false;
  const frame = el('iframe', {
    attrs: { src: 'https://pay.example.com/f' },
    style: { borderLeftWidth: '0px', borderTopWidth: '0px', paddingLeft: '0px', paddingTop: '0px',
             visibility: 'visible', display: 'block', opacity: '1' },
    rect: { width: 300, height: 150, top: 4000, left: 10, bottom: 4150, right: 310 },
  });
  frame.src = 'https://pay.example.com/f';
  frame.scrollIntoView = () => {
    scrolled = true;
    frame._rect = { width: 300, height: 150, top: 200, left: 10, bottom: 350, right: 310 };
  };
  const { api } = run(el('html', { children: [] }), 200, {}, [frame]);
  const box = api.pageFrameBox('https://pay.example.com/f', 0);
  t('an off-screen frame is scrolled into view first', scrolled === true);
  eq('and measured where it ended up', [box.x, box.y, box.inView], [10, 200, true]);
}

// --- keys: the mapping CDP needs, and the modifier trap ------------------------------------------------
{
  const { api } = run(el('html', { children: [] }), 200, {});
  const esc = api.keySpec('Escape');
  eq('a named key carries its virtual-key code', [esc.key, esc.vk], ['Escape', 27]);
  t('names are case-insensitive', api.keySpec('escape').vk === 27 && api.keySpec('ESCAPE').vk === 27);
  t('the common aliases work', api.keySpec('esc').key === 'Escape' && api.keySpec('up').key === 'ArrowUp');
  const a = api.keySpec('a');
  eq('a single letter becomes a key with text', [a.key, a.code, a.text], ['a', 'KeyA', 'a']);
  eq('and the right code for a digit', api.keySpec('7').code, 'Digit7');
  eq('nonsense is refused rather than guessed', api.keySpec('Ctrl+Shift+Whatever'), null);
  eq('so is nothing', api.keySpec(''), null);
  t('Enter carries its text so it types a newline where that is meant', api.keySpec('Enter').text === '\r');
  t('Escape carries NO text — it is not a character', api.keySpec('Escape').text === undefined);

  eq('modifiers pack into CDP bits', api.modMask(['ctrl']), 2);
  eq('several combine', api.modMask(['ctrl', 'shift']), 10);
  eq('names vary in the wild', api.modMask(['Control', 'CMD', 'Alt']), 2 | 4 | 1);
  eq('unknown modifiers are ignored, not fatal', api.modMask(['hyper']), 0);
  eq('no modifiers is zero', api.modMask(undefined), 0);
}

// --- frame refs and spaces ----------------------------------------------------------------------------
{
  const { api } = run(el('html', { children: [] }), 200, {});
  eq('a frame ref splits into frame + element', api.splitFrameRef('f1e7'), { tag: 'f1', ref: 'e7' });
  eq('a two-digit frame ref splits', api.splitFrameRef('f12e345'), { tag: 'f12', ref: 'e345' });
  eq('an ordinary ref is not a frame ref', api.splitFrameRef('e7'), null);
  eq('junk is not a frame ref', api.splitFrameRef('f1'), null);
  eq('undefined is not a frame ref', api.splitFrameRef(undefined), null);

  eq('no space named means the default lane', api.spaceOf({}), 'default');
  eq('blank space means the default lane', api.spaceOf({ space: '   ' }), 'default');
  eq('a named space is used', api.spaceOf({ space: 'apply' }), 'apply');
  eq('a space name is trimmed', api.spaceOf({ space: ' apply ' }), 'apply');
  t('an absurd space name is capped', api.spaceOf({ space: 'z'.repeat(200) }).length === 40);
}

// --- native <select> ----------------------------------------------------------------------------
// The snapshot has always reported a <select> as `combobox`, so the model is told it can act on one.
// Nothing could: pagePick looked only for an EXPLICIT [role=combobox] (which a <select> never has),
// pageFields did not list selects at all, and pageTypeRef wrote through the HTMLInputElement value
// setter, which leaves a <select> untouched — every attempt came back "typed" with the old option
// still selected. A dropdown the agent is shown but cannot move is worse than one it cannot see.
// Rich editors (X, LinkedIn, Reddit) are contenteditable divs rather than input/textarea controls.
// They must be visible to browser_fields and writable by both a label and a snapshot ref.
{
  const company = el('button', { text: 'Company' });
  const publish = el('button', { text: 'Publish' });
  const captcha = el('button', { attrs: { 'aria-label': 'Complete CAPTCHA' }, text: 'Continue' });
  const win = { __collieRefs: new Map([['e1', company], ['e2', publish], ['e3', captcha]]) };
  const api = new Function('window', grab('function pageAdvanceInfo(ref)') +
    '\nreturn { pageAdvanceInfo };')(win);
  t('ordinary Company selection is a reversible advance', api.pageAdvanceInfo('e1').allowed === true);
  t('a final Publish control is refused before click', !!api.pageAdvanceInfo('e2').error);
  t('CAPTCHA controls are refused before click', !!api.pageAdvanceInfo('e3').error);
}

{
  const token = el('textarea', { attrs: { name: 'g-recaptcha-response' },
                                 value: '0cAF-live-secret' });
  const oauth = el('input', { attrs: { name: 'session_redirect' },
                              value: '/oauth?state=private' });
  const doc = {
    querySelectorAll: (q) => String(q).startsWith('input,textarea') ? [token, oauth] : [],
    querySelector: () => null,
  };
  const api = new Function('document', 'CSS', grab('function pageFormSnapshot()') +
    '\nreturn { pageFormSnapshot };')(doc, { escape: (s) => s });
  const field = api.pageFormSnapshot().fields[0];
  eq('CAPTCHA response tokens are redacted in CSP-safe snapshots', field && field.value, '[redacted]');
  t('the CAPTCHA field is marked sensitive', !!(field && field.sensitive));
  eq('OAuth redirect state is redacted in CSP-safe snapshots',
     api.pageFormSnapshot().fields[1].value, '[redacted]');
}

{
  const editor = el('div', { attrs: { contenteditable: 'true', role: 'textbox',
                                      'aria-label': 'Post text' } });
  editor.isContentEditable = true;
  editor.innerText = '';
  editor.textContent = '';
  editor.focus = () => {};
  editor.events = [];
  editor.dispatchEvent = function (e) {
    this.events.push(e.type);
    if (e.type === 'input') this.innerText = this.textContent;
    return true;
  };
  const win = { HTMLSelectElement: { prototype: {} }, HTMLInputElement: { prototype: {} },
                HTMLTextAreaElement: { prototype: {} }, __collieRefs: new Map([['e1', editor]]) };
  const doc = {
    querySelectorAll: (q) => String(q).includes('contenteditable') ? [editor] : [],
    querySelector: () => null,
    getElementById: () => null,
  };
  const Input = function (type) { return { type: type }; };
  const api = new Function(
    'window', 'document', 'CSS', 'Event', 'InputEvent', 'innerWidth', 'innerHeight',
    [grab('function pageFields()'), grab('function pageTypeLabel(labelText, text)'),
     grab('function pageTypeRef(ref, text, submit)'),
     grab('function pageFormSnapshot()')].join('\n') +
    '\nreturn { pageFields, pageTypeLabel, pageTypeRef, pageFormSnapshot };'
  )(win, doc, { escape: (s) => s }, Input, Input, VIEW.w, VIEW.h);
  const fields = api.pageFields();
  eq('a contenteditable editor is listed as richtext', fields[0] && fields[0].kind, 'richtext');
  eq('its accessible name survives as the field label', fields[0] && fields[0].label, 'Post text');
  api.pageTypeLabel('Post text', 'VocalCode by label');
  eq('label typing lands in a contenteditable editor', editor.innerText, 'VocalCode by label');
  api.pageTypeRef('e1', 'VocalCode by ref');
  eq('ref typing lands in a contenteditable editor', editor.innerText, 'VocalCode by ref');
  eq('CSP-safe form snapshot retains the full rich-editor value',
     api.pageFormSnapshot().fields[0].value, 'VocalCode by ref');
  t('rich editor typing emits an input event', editor.events.indexOf('input') >= 0);
}

// X and similar React apps keep stale composers mounted with the same accessible label. Label-based
// typing must choose the rendered/in-viewport editor, and high-fidelity mode must have a real label
// route instead of silently falling back to synthetic DOM events.
{
  function editorAt(rect) {
    const e = el('div', { attrs: { contenteditable: 'true', role: 'textbox',
                                   'aria-label': 'Post text' }, rect });
    e.isContentEditable = true; e.innerText = ''; e.textContent = ''; e.focus = () => {};
    e.dispatchEvent = function (ev) { if (ev.type === 'input') this.innerText = this.textContent; return true; };
    return e;
  }
  const stale = editorAt(OFFSCREEN);
  const active = editorAt({ width: 400, height: 100, top: 100, left: 100, bottom: 200, right: 500 });
  const doc = {
    querySelectorAll: () => [stale, active],
    querySelector: () => null,
    getElementById: () => null,
  };
  const Input = function (type) { return { type }; };
  const api = new Function(
    'document', 'Event', 'InputEvent', 'innerWidth', 'innerHeight',
    grab('function pageTypeLabel(labelText, text)') + '\n' +
    grab('function pagePointLabel(labelText)') +
    '\nreturn { pageTypeLabel, pagePointLabel };'
  )(doc, Input, Input, VIEW.w, VIEW.h);
  api.pageTypeLabel('Post text', 'active copy');
  eq('label typing ignores an off-screen stale composer', [stale.innerText, active.innerText], ['', 'active copy']);
  eq('trusted label targeting resolves the active editor point',
     [api.pagePointLabel('Post text').x, api.pagePointLabel('Post text').y], [300, 150]);
  t('trusted browser typing has a label-addressed CDP path',
    src.includes('await trustedTypeLabel(cmd.label, cmd.text, !!cmd.submit)'));
  t('trusted exact-ref clicks revalidate the same live node after the cursor delay',
    src.includes('await execMain(pagePointStillRef, [ref])') &&
    src.includes('refRevalidated: true'));
}

{
  function option(text, value) { return { text: text, value: value === undefined ? text : value }; }
  function select(opts, attrs) {
    const node = el('select', { attrs: attrs || {} });
    node.options = opts;
    node.value = opts.length ? opts[0].value : '';
    node.focus = () => {};
    node.events = [];
    node.dispatchEvent = function (e) { this.events.push(e.type); return true; };
    return node;
  }

  function runSelect(sel, extraDoc) {
    const win = { HTMLSelectElement: { prototype: {} }, HTMLInputElement: { prototype: {} },
                  HTMLTextAreaElement: { prototype: {} }, __collieRefs: new Map([['e1', sel]]) };
    const doc = Object.assign({
      querySelectorAll: (q) => (String(q).includes('select') ? [sel] : []),
      querySelector: () => null,
      getElementById: () => null,
    }, extraDoc || {});
    const fn = new Function(
      'window', 'document', 'CSS', 'Event',
      [grab('function pageFields()'), grab('async function pagePick(labelText, optionText)'),
       grab('function pageTypeRef(ref, text, submit)')].join('\n') +
      '\nreturn { pageFields, pagePick, pageTypeRef };'
    )(win, doc, { escape: (s) => s }, function (type) { return { type: type }; });
    return fn;
  }

  const opts = [option('Android'), option('Mac'), option('Windows')];
  {
    const sel = select(opts, { 'aria-label': 'Operating system' });
    const api = runSelect(sel);
    const fields = api.pageFields();
    eq('a native select is listed as a dropdown', fields.length && fields[0].kind, 'dropdown');
    eq('its options are reported, not just its existence', fields.length && fields[0].options,
       ['Android', 'Mac', 'Windows']);
  }
  {
    const sel = select(opts.map((o) => option(o.text)), { 'aria-label': 'Operating system' });
    const api = runSelect(sel);
    const r = api.pageTypeRef('e1', 'Windows');
    eq('typing an option name into a select picks it', r.picked, 'Windows');
    eq('and the value actually lands', sel.value, 'Windows');
    t('a change event is fired so frameworks notice', sel.events.indexOf('change') >= 0);
  }
  {
    const sel = select(opts.map((o) => option(o.text)), { 'aria-label': 'Operating system' });
    const api = runSelect(sel);
    const r = api.pageTypeRef('e1', 'Solaris');
    t('an option that does not exist is an error, not a silent no-op', !!r.error);
    eq('and the error says what was on offer', r.options, ['Android', 'Mac', 'Windows']);
  }
  {
    const sel = select(opts.map((o) => option(o.text)), { 'aria-label': 'Operating system' });
    const api = runSelect(sel);
    // The only async assertion in the suite, so it owns the tail: everything after this block is
    // hoisted declarations, and anything else placed there would never run.
    return api.pagePick('operating system', 'Mac').then((r) => {
      eq('pick finds a select by its aria-label', r.picked, 'Mac');
      eq('and moves it', sel.value, 'Mac');
      uploadTests();
      finish();
    });
  }
}

// --- upload read-back ---------------------------------------------------------------------------
// `attached` used to be `landed === transfer.files.length`, which is TRUE when both are zero — so an
// upload that attached nothing reported success, and the caller only ever checks `attached === false`.
// The read-back exists precisely to stop a refused upload looking like a successful one; comparing
// against the DataTransfer instead of against what was asked for reopened that hole one level up.
function uploadTests() {
  function runUpload(fileListAfter, transferCount) {
    const input = { tagName: 'INPUT', type: 'file', multiple: true, files: fileListAfter,
                    getAttribute: () => 'image/*', dispatchEvent: () => true };
    const win = {};
    const doc = { querySelector: () => input, querySelectorAll: () => [] };
    const DT = function () {
      this.items = { add: () => { this.files.length = Math.min(this.files.length + 1, transferCount); } };
      this.files = [];
      this.files.length = 0;
    };
    const fn = new Function(
      'window', 'document', 'DataTransfer', 'Blob', 'File', 'Uint8Array', 'atob', 'Event',
      grab('function pageUpload(selector, files, ref)') + '\nreturn pageUpload;'
    )(win, doc, DT, function () {}, function () {}, { from: () => [] }, () => '', function (t) { return { type: t }; });
    input.instanceCheck = true;
    return { fn: fn, input: input };
  }
  // The real function guards with `instanceof HTMLInputElement`, which a plain object cannot satisfy,
  // so assert the shape of the contract directly instead of re-running the guard.
  const src = grab('function pageUpload(selector, files, ref)');
  t('attached is not satisfied by an empty file list',
    /landed > 0 && landed === files\.length/.test(src));
  t('a failed attach carries an explanation, not just a false flag',
    /out\.error =/.test(src));
  t('the read-back still compares what the input actually holds',
    /input\.files \? input\.files\.length : 0/.test(src));
}

function finish() {
  console.log('\n== browser extension (page-side): ' + pass + '/' + (pass + fail) + ' passed ==' +
              (fail ? ' — ' + fail + ' FAILED' : ''));
  process.exit(fail ? 1 : 0);
}
