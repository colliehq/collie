/**
 * Static checks over harness/webui/daily_brief.html.
 *
 * There is no browser here and no pretend DOM: a fake DOM would only prove that the
 * fake behaves. What this file checks are the properties that are decidable from the
 * source itself and that keep failing silently in real pages -- a button wired to an
 * id that does not exist, a string dropped into markup instead of textContent, a
 * Chinese label that was never written, a background poll nobody asked for, a fetch
 * to somewhere other than this host.
 *
 * The second half of the file is about the morning-email panel, where the stakes are
 * different: that panel writes a preference which can put a private summary into a
 * mailbox. So it is checked for the shape of the single request it sends, for the absence
 * of anywhere to type an address or a credential, for never writing without a press, and
 * for the copy that has to be true -- a running computer, one email a local day, a missed
 * day skipped rather than queued, and acceptance by a provider not called delivery.
 *
 * The behavioural half (does the brief render, does Hide survive a refresh) belongs in
 * the parent's real-browser run; these are the checks worth having before that exists.
 *
 *   node tests/daily_brief_ui_test.mjs
 */
import { Script } from "node:vm";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const path = join(here, "..", "harness", "webui", "daily_brief.html");
const page = readFileSync(path, "utf8");
const script = page.slice(page.indexOf("<script>"), page.lastIndexOf("</script>"));
const markup = page.slice(0, page.indexOf("<script>"));

const failures = [];
function check(value, message) {
  console.log((value ? "  PASS " : "  FAIL ") + message);
  if (!value) failures.push(message);
}
const all = (re, text = script) => [...text.matchAll(re)].map((m) => m[1]);

// ── the script at least parses ──────────────────────────────────────────────────
// Compiled, never run: a page whose script throws on load is a blank surface, and a
// stray typo in a one-file UI is otherwise invisible until someone opens it.
let compiled = "";
try {
  new Script(script.replace("<script>", "").replace("</script>", ""), { filename: "daily_brief.html" });
} catch (exc) {
  compiled = exc.message;
}
check(compiled === "", `the page script parses (${compiled || "ok"})`);

// ── untrusted strings never become markup ───────────────────────────────────────
for (const sink of ["innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"]) {
  check(!page.includes(sink), `no ${sink}: source strings stay text`);
}
check(/function text\(el,value\)\{el\.textContent=/.test(script), "text() assigns textContent");
check(!/\+\s*["'`]<[a-z]/i.test(script), "no hand-built HTML fragments");

// ── every wired id exists, so no button is quietly dead ─────────────────────────
const ids = new Set(all(/\sid="([A-Za-z0-9_-]+)"/g, markup));
const wired = new Set(all(/\$\("([A-Za-z0-9_-]+)"\)/g));
const missing = [...wired].filter((id) => !ids.has(id));
check(missing.length === 0, `every $(id) exists in the markup (missing: ${missing.join(", ") || "none"})`);

// ── bilingual: nothing ships with an English-only label ─────────────────────────
const tagsWithEn = [...markup.matchAll(/<[^>]*\sdata-en="[^"]*"[^>]*>/g)].map((m) => m[0]);
const untranslated = tagsWithEn.filter((tag) => !/\sdata-zh="/.test(tag));
check(tagsWithEn.length > 20, `the page carries real copy (${tagsWithEn.length} translated nodes)`);
check(untranslated.length === 0, `every data-en has a data-zh (missing on ${untranslated.length})`);
check(/document\.documentElement\.lang=/.test(script), "the language toggle updates <html lang>");
const trPairs = [...script.matchAll(/tr\(\s*("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')\s*,/g)].length;
check(trPairs >= 20, `runtime strings go through tr() (${trPairs} pairs)`);

// ── accessible, touch-sized controls ────────────────────────────────────────────
const buttons = [...markup.matchAll(/<button[^>]*>([^<]*)</g)];
const unlabelled = buttons.filter(([tag, inner]) =>
  !inner.trim() && !/aria-label=/.test(tag) && !/data-en=/.test(tag));
check(unlabelled.length === 0, `every static button has a label (${unlabelled.length} without)`);
check(/min-height:40px/.test(page), "buttons keep a touch-sized target");
check(/name="viewport"[^>]*width=device-width/.test(page), "mobile viewport is declared");
check(/@media\(max-width:560px\)/.test(page), "a small-screen layout exists");
check(/aria-live="polite"/.test(markup) && /role="alert"/.test(markup),
  "status and error regions are announced");
const rowButtons = all(/node\("button",tr\([^)]*\)\);\s*\n\s*([a-z]+)\.setAttribute\("aria-label"/g);
check(rowButtons.length >= 3, `per-row buttons name the item they act on (${rowButtons.length})`);

// ── one host, one surface, no background polling ────────────────────────────────
const fetched = all(/fetch\(\s*(?:url\()?["'`]([^"'`]+)/g);
check(fetched.every((target) => target.startsWith("/")),
  `every request is same-origin (${fetched.join(" ")})`);
check(fetched.every((t) => t.startsWith("/api/brief") || t === "/api/session-token"),
  "the page talks only to its own route and the token refresh");
const called = all(/\bapi\("([^"]+)"/g);
const routes = ["/api/brief", "/api/brief/preferences"];
check(called.length >= 3 && called.every((t) => routes.includes(t)),
  `every api() call is the brief or its preferences (${[...new Set(called)].join(" ")})`);
for (const noisy of ["setInterval", "EventSource", 'addEventListener("focus"', 'addEventListener("blur"',
  "visibilitychange", "location.reload"]) {
  check(!script.includes(noisy), `no ${noisy}: the brief refreshes when asked, not on its own`);
}
check(/\$\("refresh"\)\.addEventListener\("click"/.test(script), "there is an explicit Refresh");

// ── auth and error handling ─────────────────────────────────────────────────────
check(/meta\[name="collie-token"\]/.test(script), "the page reads the injected token");
check(/res\.status===403&&retry/.test(script) && /\/api\/session-token/.test(script),
  "a 403 refreshes the session token once and retries");
check(/catch\(e\)\{notice\(e\.message,true,target\)/.test(script),
  "every guarded action shows its own failure");
const clickHandlers = [...script.matchAll(/addEventListener\("click",\s*(\(\)=>|async)[^\n]*/g)].map((m) => m[0]);
const unguarded = clickHandlers.filter((line) => /(\bapi\(|\bact\()/.test(line) && !/busy\(/.test(line));
check(clickHandlers.length >= 5 && unguarded.length === 0,
  `every request-making click is wrapped so errors surface (${unguarded.length} unguarded)`);
check(/payload\.error\|\|tr\(/.test(script), "a server error message is shown verbatim when there is one");

// ── the product promises this surface makes ─────────────────────────────────────
check(/data-zh="下面每一条都是该来源在所示时间的记录，不是实时状态。/.test(markup) &&
  /not a live check/.test(markup),
  "the brief says its rows are what a source reported, not a live status");
check(/It is not finished, it stays in Needs You and Missions/.test(markup),
  "hiding is explained as a brief preference, not completion");
check(/item\.kind==="approval"/.test(script) && /cannot be hidden/.test(script),
  "a waiting decision offers no Hide control and says why");
check(/read ","读取于 /.test(script) && /reported ","记录时间 /.test(script),
  "each row shows when its source was read and when the row was reported");
check(/could not be read/.test(script) || /row\.reason/.test(script),
  "an unreadable source is rendered with its reason, never as empty");
check(/sendable/.test(script) === false && /email\.reason/.test(script),
  "the email panel previews and copies text; no send button is offered yet");
check(/navigator\.clipboard\.writeText/.test(script) && /Copying was blocked/.test(script),
  "copy has a working fallback when the clipboard is refused");
check(!/password|token=|credential|secret/i.test(markup),
  "no credential or connection detail is printed in the page body");

// ── the morning email: what the panel may and may not do ────────────────────────
// This panel writes a preference that can put a private summary in a mailbox, so the
// checks here are about consent and about not inventing a destination: the shape of the
// one POST it makes, the absence of anywhere to type an address, and the copy that says
// what the schedule can and cannot promise.
const posted = all(/([a-z_]+):/g, (script.match(/return \{enabled:true,[\s\S]*?\};/) || [""])[0]);
check(posted.length === 6 && ["enabled", "connection", "timezone", "at", "language", "grace_minutes"]
  .every((key) => posted.includes(key)),
  `the saved settings are exactly the six the server accepts (${posted.join(" ")})`);
check(!/recipient|收件人/i.test(markup) && !/(destination|to|address|email):/.test(
  (script.match(/return \{enabled:true,[\s\S]*?\};/) || [""])[0]),
  "there is nowhere to type a destination, and none is ever posted");
const inputs = [...markup.matchAll(/<input[^>]*>/g)].map((m) => m[0]);
check(inputs.length > 0 && inputs.every((tag) => /\sid="(schedAt|schedZone|schedGrace)"/.test(tag)),
  `the only inputs are the schedule's own (${inputs.length})`);
check(!/type="(email|password|tel|url)"/.test(markup),
  "no field asks for an address, a password or a phone number");
check(/destination_masked/.test(script) && !/\.owner\b/.test(script),
  "the destination is shown masked, and the raw address is never read by the page");
check(/api\("\/api\/brief\/preferences"\)/.test(script) &&
  /api\("\/api\/brief\/preferences",schedBody\(/.test(script),
  "preferences are read with a GET and written only from the one save body");
check(/loadPrefs\(\);\s*<\/script>|loadPrefs\(\);\s*$/m.test(script.trim()) || /\nloadPrefs\(\);/.test(script),
  "the panel reads its settings on load");
const writes = [...script.matchAll(/saveSched\((true|false)\)/g)].map((m) => m[0]);
check(writes.length === 2 && /event\.preventDefault\(\)/.test(script),
  `nothing is saved or enabled except from the two explicit controls (${writes.join(" ")})`);
check(/busy\(\$\("schedSave"\),\(\)=>saveSched\(true\)/.test(script) &&
  /busy\(\$\("schedOff"\),\(\)=>saveSched\(false\)/.test(script),
  "saving and turning off both surface their own failure");
check(/Nothing here is saved, and nothing is turned on, until you press a button/.test(markup),
  "the panel says it never saves or enables by itself");
check(/Collie must be running on this computer/.test(markup) &&
  /that day is skipped and said so — never sent later as a backlog/.test(markup),
  "the morning window needs a running computer, and a missed day is skipped, not queued");
check(/At most one email a local day/.test(markup), "one email a local day is stated");
check(/separate from a connection's auto-reply/.test(markup),
  "this consent is stated as separate from a connection's auto-reply");
check(/If you save while this morning's window is already open/.test(markup) &&
  /may go out within a few minutes/.test(script),
  "enabling inside today's window warns that today's brief may go out soon");
check(/That is not proof it arrived/.test(markup) && /not proof it arrived/.test(script),
  "acceptance by the mail account is never reported as delivery");
check(/will not be sent again/.test(script),
  "an unknown outcome is never retried, and says so");
check(/href="\/communications"/.test(markup) && /No email account is connected yet/.test(markup),
  "with no account connected the panel points at where to connect one, and offers nothing else");
check(/paused_reason/.test(script) && /Choose an account and save again/.test(script),
  "a paused schedule shows its reason and what to do");
check(/p\.available===false/.test(script) && /This page could not read the settings/.test(script) &&
  !/Nothing was sent and nothing was changed/.test(script),
  "settings that cannot be read are visible, and claim no state of their own");

// ── the panel cannot break the brief, and cannot lose what you typed ─────────────
check(/let prefs=null/.test(script) && !/\bprefs\b/.test(script.slice(script.indexOf("function render()"),
  script.indexOf("let briefSeq"))),
  "rendering the brief never reads the schedule, so unreadable settings keep the brief usable");
check(/catch\(e\)\{failed=e\.message;\}/.test(script) && /if\(!failed\)adopt/.test(script),
  "a failed preferences read is reported in its own panel, not thrown at the brief");
check(/if\(prefsDirty\)return;/.test(script) &&
  /addEventListener\("input",\(\)=>\{prefsDirty=true;\}\)/.test(script),
  "a half-typed setting survives a refresh and a language switch");
const dropped = [...script.matchAll(/[;\s]prefsDirty=false/g)].length;   // not the declaration
check(dropped === 2 && /\$\("schedRevert"\)\.addEventListener\("click",\(\)=>\{\s*prefsDirty=false;fillFields\(\)/.test(script),
  `unsaved settings are dropped only by saving and by the named undo (${dropped} places)`);
check(/\$\("schedRevert"\)\.hidden=!prefsDirty/.test(script) && /were kept/.test(script),
  "re-reading the status says the unsaved changes were kept, and offers the undo");
check(/const want=select\.value\|\|saved/.test(script),
  "rebuilding the account list for a new language keeps the chosen account");

// ── stale answers and hostile storage ───────────────────────────────────────────
check(/const mine=\+\+briefSeq/.test(script) && /if\(mine!==briefSeq\)return/.test(script) &&
  /const mine=\+\+prefsSeq/.test(script) && /if\(mine!==prefsSeq\)return/.test(script),
  "each load is numbered so a late answer cannot repaint a newer one");
check(/catch\(e\)\{if\(mine!==briefSeq\)return;throw e;\}/.test(script),
  "a superseded request's error is dropped with it, not shown over a newer load");
check(/\$\("lang"\)\.addEventListener\("click",\(\)=>\{[^}]*load\(\);\}\)/.test(script),
  "switching language always reloads the brief, even mid-request");
check(!/loading=/.test(script), "no early-return flag can swallow a language switch");
const storage = all(/(localStorage\.\w+)/g);
check(storage.length === 2 && /try\{return localStorage\.getItem/.test(script) &&
  /try\{localStorage\.setItem/.test(script),
  `every localStorage access is guarded (${storage.join(" ")})`);
check(/String\(store\.get\("collie-brief-lang"\)\|\|navigator\.language\|\|"en"\)/.test(script),
  "a missing or hostile storage still resolves a language");
check(/\$\("previewText"\)\.value=email\.text/.test(script) && !/action:"preview"/.test(script),
  "the preview shows the snapshot already on screen and rebuilds nothing");

console.log(failures.length ? `\n${failures.length} FAILED` : "\nall checks passed");
process.exit(failures.length ? 1 : 0);
