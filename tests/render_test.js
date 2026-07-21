// Regression suite for the collie web UI markdown renderer (esc / hlCode / md / mdStream).
// Extracts the ACTUAL shipped functions from webui/index.html by brace-matching, then runs a
// battery covering basics, code blocks, tables, lists, XSS, streaming partial-fences, edge cases.
//   node tests/render_test.js        (exit 0 = all pass)
const fs = require('fs');
const path = require('path');
const html = fs.readFileSync(path.join(__dirname, '..', 'harness', 'webui', 'index.html'), 'utf8');
const lines = html.split('\n');

function grab(startPat) {
  let start = lines.findIndex(l => l.includes(startPat));
  if (start < 0) throw new Error('not found: ' + startPat);
  let depth = 0, started = false, out = [];
  for (let j = start; j < lines.length; j++) {
    out.push(lines[j]);
    const code = lines[j].split('//')[0];
    for (const ch of code) { if (ch === '{') { depth++; started = true; } else if (ch === '}') depth--; }
    if (started && depth <= 0) break;
  }
  return out.join('\n');
}
const src = [grab('var esc = function'), grab('function hlCode(code)'),
             grab('function md(src)'), grab('function mdStream(src)')].join('\n');
const mod = { exports: {} };
new Function('module', src + '\nmodule.exports = {esc, md, mdStream, hlCode};')(mod);
const R = mod.exports;

let pass = 0, fail = 0, fails = [];
function t(name, got, want) {
  const ok = (typeof want === 'function') ? want(got) : got.includes(want);
  if (!ok) { fails.push(name); console.log('  FAIL ' + name + '\n        got: ' + JSON.stringify(got).slice(0, 200)); }
  ok ? pass++ : fail++;
}

// basics
t('bold', R.md('a **b** c'), '<strong>b</strong>');
t('italic', R.md('a *b* c'), '<em>b</em>');
t('inline code', R.md('use `x=1` here'), '<code>x=1</code>');
t('headings', R.md('# A\n## B\n### C'), s => s.includes('<h2>A</h2>') && s.includes('<h3>B</h3>') && s.includes('<h4>C</h4>'));
t('link', R.md('[go](https://x.com)'), 'href="https://x.com"');
t('paragraph', R.md('hello world'), '<p>hello world</p>');
// code blocks
t('fenced code', R.md('```\ndef f(): pass\n```'), s => s.includes('<pre class="cb">') && s.includes('<code>'));
t('code lang+highlight', R.md('```python\ndef f():\n  return 1\n```'), s => s.includes('hl-kw'));
t('code preserves <', R.md('```\nif a < b:\n```'), '&lt;');
// tables
t('github table', R.md('| a | b |\n|---|---|\n| 1 | 2 |'), s => s.includes('<table>') && s.includes('<th>a</th>') && s.includes('<td>1</td>'));
t('non-table pipes', R.md('a | b no sep'), s => !s.includes('<table>'));
t('table cell formatting', R.md('| **a** | `c` |\n|---|---|\n| 1 | 2 |'), s => s.includes('<strong>a</strong>') && s.includes('<code>c</code>'));
// lists
t('ul list', R.md('- one\n- two'), s => s.includes('<ul>') && s.includes('<li>one</li>'));
t('ol list', R.md('1. one\n2. two'), s => s.includes('<ol>') && s.includes('<li>one</li>'));
t('bold in list', R.md('- **x** y'), s => s.includes('<li>') && s.includes('<strong>x</strong>'));
// security
t('XSS script escaped', R.md('<script>alert(1)</script>'), s => !s.includes('<script>') && s.includes('&lt;script&gt;'));
t('XSS img onerror', R.md('<img src=x onerror=alert(1)>'), s => !s.includes('<img '));
t('XSS href breakout', R.md('[x](https://a"onmouseover="alert(1))'), s => !s.includes('onmouseover="'));
t('XSS javascript link', R.md('[x](javascript:alert(1))'), s => !s.includes('href="javascript'));
t('link text special chars', R.md('[a & b](https://x.com)'), s => s.includes('href="https://x.com"') && s.includes('a &amp; b'));
// placeholder-leak regression (the old "B0" bug)
t('placeholder no leak', R.md('text\n\n```\ncode\n```\n\nmore'), s => !/\bB\d+\b/.test(s) && s.includes('code'));
t('multi code blocks', R.md('```\nAAA\n```\ntext\n\n```\nBBB\n```'), s => s.includes('AAA') && s.includes('BBB'));
// streaming partial-fence safety
t('mdStream open fence', R.mdStream('code:\n```python\ndef f('), s => s.includes('<pre class="cb">'));
t('mdStream even fence', R.mdStream('```\ndone\n```'), s => s.includes('done'));
t('mdStream plain', R.mdStream('streaming words'), s => s.includes('streaming words'));
// edge cases
t('empty', R.md(''), s => s === '');
t('null-safe', R.md(null), s => s === '');
t('unicode', R.md('héllo 世界 🌍'), s => s.includes('世界') && s.includes('🌍'));
t('ampersand escaped', R.md('a & b'), '&amp;');

console.log('== RENDERER: ' + pass + '/' + (pass + fail) + ' passed ==' + (fails.length ? (' FAILS: ' + fails.join(', ')) : ' — ALL GREEN'));
process.exit(fail ? 1 : 0);
