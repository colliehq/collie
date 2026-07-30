// Make CLOSED shadow roots reachable — without changing what the page sees.
//
// The snapshot walks `el.shadowRoot` to look inside web components, and that property is null for
// any root created with `attachShadow({mode:"closed"})`. Component-heavy sites use closed roots for
// exactly the controls an agent needs (Reddit's new post editor, its flair dialog), so those
// controls simply do not appear in the snapshot — and "not in the snapshot" is indistinguishable
// from "not on the page". Collie reported a required flair picker as impossible to reach on that
// basis and stopped a launch.
//
// The fix is to record each closed root as it is created, in a WeakMap only collie's own injected
// functions read. Note what this deliberately does NOT do: it does not force the root open, does not
// change the `mode` the page requested, and does not make `el.shadowRoot` non-null. Encapsulation
// stays exactly as the page built it — sites that branch on `el.shadowRoot === null` keep working —
// and the visibility is one-way, ours. The WeakMap holds no strong reference, so a discarded
// component is still collectable.
//
// Runs at document_start in the MAIN world so the patch is in place before the page defines its
// first component. It cannot see roots attached before it ran (a page loaded prior to the extension
// being installed) — reloading the tab fixes that.
(function () {
  if (window.__collieClosedRoots) return;                  // already patched this document
  var proto = window.Element && window.Element.prototype;
  var real = proto && proto.attachShadow;
  if (typeof real !== "function") return;                  // nothing to patch; leave the page alone

  var roots = new WeakMap();
  try {
    Object.defineProperty(window, "__collieClosedRoots",
                          { value: roots, writable: false, enumerable: false, configurable: false });
  } catch (e) { return; }                                  // can't stash it -> don't patch at all

  function attachShadow(init) {
    var root = real.apply(this, arguments);
    try { if (init && init.mode === "closed") roots.set(this, root); } catch (e) {}
    return root;
  }
  // Keep the patch invisible to feature-detection and to sites that fingerprint native functions.
  try { Object.defineProperty(attachShadow, "name", { value: "attachShadow" }); } catch (e) {}
  try { attachShadow.toString = function () { return real.toString(); }; } catch (e) {}

  try {
    Object.defineProperty(proto, "attachShadow",
                          { value: attachShadow, writable: true, enumerable: false, configurable: true });
  } catch (e) { /* a frozen prototype just means no closed-root visibility here */ }
})();
