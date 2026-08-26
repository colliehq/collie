# Chrome Web Store release checklist

The store build is the same Manifest V3 extension used for local development, minus machine-local
credentials. Run `installer/package_browser_extension.ps1`; do not zip the live directory by hand.

## Permission rationale

- `tabs`, `activeTab`, `scripting`, and `<all_urls>` let Collie read and act in the tab the user handed
  over or in a tab Collie created. The host permission also lets the extension reach the loopback
  bridge. Site navigation permission is separate from action-time approval.
- `debugger` provides real `isTrusted` pointer and keyboard input on sites that reject synthetic
  events, reaches cross-origin frames, and supports file inputs. The user can cancel Chrome's debug
  banner; cancellation hard-pauses the controlled tab.
- `storage` persists the bridge token, input-fidelity overrides, browser site-access preference, and
  per-session tab ownership. `alarms` keeps the Manifest V3 bridge poll recoverable after suspension.
- `sidePanel` and `contextMenus` provide Ask Collie for the current page or selected text.

No remotely hosted code is loaded. The extension communicates only with loopback Collie services and
with pages the user/agent opens. `token.txt` and legacy `auth.js` must never be present in a release zip.

## Before upload

1. Run `node tests/browser_ext_test.js` and `python -m pytest tests/test_browserbridge.py -q`.
2. Run the package script and inspect the zip listing. Confirm there is no token, auth file, source
   map, log, or user data.
3. Load the staged zip unpacked in a clean Chrome profile. Verify popup connection state, side chat,
   selection context menu, cursor/presence pill, Stop, physical takeover, Resume, and debugger-cancel
   pause behavior.
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
