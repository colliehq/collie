# Chrome Web Store release checklist

The Web Store and local Power builds deliberately have different permission envelopes. Run
`installer/package_browser_extension.ps1` for the store build. Add `-Power` only for the sideloaded
Power build; do not zip the live directory by hand.

## Permission rationale

- The store build installs with `activeTab` and `scripting`, plus loopback host access. Invoking the
  extension grants the current tab temporarily. Persistent `http://*/*` and `https://*/*` reach is an
  optional runtime grant behind **Enable across websites**; it is never an install-time grant.
- The store build does not request `tabs`, `<all_urls>`, or all-page content-script access. Its
  presence sensor is injected only into a granted, controlled tab.
- Both builds request `debugger`: no-focus trusted input, cross-origin-frame support, and full-page
  capture are core browser-control features, not dormant future access. Chrome does not permit this
  permission to be optional, so the store listing must explain the visible debugging banner and the
  extension must attach only around an active browser session/action. Canceling the banner hard-pauses
  the controlled tab.
- Both builds request `downloads` so a download action returns Chrome's concrete started/completed/
  interrupted receipt. A DOM click alone is never reported as a finished download.
- Both builds request `webNavigation` so OAuth and `noopener` child tabs can be correlated with the
  exact Collie-controlled source tab. The extension does not use it to collect browsing history.
- `storage` persists the bridge token, input-fidelity overrides, browser site-access preference, and
  per-session tab ownership. `alarms` keeps the Manifest V3 bridge poll recoverable after suspension.
- `sidePanel` and `contextMenus` provide Ask Collie for the current page or selected text.

No remotely hosted code is loaded. The extension communicates only with loopback Collie services and
with pages the user/agent opens. `token.txt` and legacy `auth.js` must never be present in a release zip.

## Before upload

1. Run `node tests/browser_ext_test.js` and `python -m pytest tests/test_browserbridge.py -q`.
2. Run the package script and inspect the zip listing. Confirm `manifest.json` has the justified
   `debugger`, `downloads`, and `webNavigation` permissions but no `tabs`, `<all_urls>`, required broad host permission,
   token, auth file, source map, log, or user data.
3. Load the staged zip unpacked in a clean Chrome profile. Verify popup connection state, side chat,
   selection context menu, optional website grant, cursor/presence pill, Stop, physical takeover,
   and Resume. Test debugger-cancel pause behavior and a complete/interrupted download receipt in
   both builds.
4. Exercise the three navigation policies. Under the recommended policy, an ordinary documentation
   site opens silently and a bank/payment/crypto site still asks. Verify that clicking Send/Buy/Delete
   still produces an action-time approval on both kinds of site.
5. Verify tab lifecycle: a completed/canceled/crashed Web run releases control; user tabs never close;
   `finalize close=true` closes only a Collie-owned tab.
6. Publish the repository privacy-policy URL and use the exact permission rationale above in the
   store privacy form. Store signing/upload remains a human publisher action.

## Release assets

The package contains `manifest.json`, the popup, side panel, background/content scripts, and the
16/48/128 px icons. Store screenshots should show (a) side chat with current-page context, (b) the
visible Acting/Paused pill and cursor, and (c) the separated Website access and Input fidelity controls.
