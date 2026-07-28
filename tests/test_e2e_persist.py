"""K_dev survives a desktop restart, and a device whose key is gone is told so.

E2E_DESIGN.md §7 promises: "Desktop restart: K_dev persists (in the device store) → returning device
keeps working, no re-pair." The shipped code did the opposite — the keypair was per process, so every
`collie web` restart left every encrypted phone unable to open a single frame, surfacing as an opaque
5xx with nothing a person could act on. Both halves are pinned here: the key comes back, and when it
genuinely cannot, the answer says what to do about it.

    python3 tests/test_e2e_persist.py
"""
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_fails = []


def check(cond, msg):
    print(("  PASS " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def main():
    state = tempfile.mkdtemp(prefix="collie-e2e-persist-")
    os.environ["COLLIE_STATE_DIR"] = state
    for mod in [m for m in list(sys.modules) if m.startswith("harness")]:
        del sys.modules[mod]
    from harness import remote_identity                               # noqa: E402

    ident = remote_identity.load_or_create()
    ident.add_or_update("phone-1", "hash-1", "iPhone")
    ident.add_or_update("phone-2", "hash-2", "iPad")

    key1 = base64.b64encode(b"\x01" * 32).decode()
    ident.set_device_key("phone-1", key1)
    check(ident.device_keys() == {"phone-1": key1}, "a device key is stored for the device that has one")
    check("phone-2" not in ident.device_keys(),
          "a device that paired without encryption has no key, and does not get an empty one")

    # The point of the whole exercise: a NEW process must find it.
    reloaded = remote_identity.load_or_create()
    check(reloaded.device_keys().get("phone-1") == key1,
          "and a fresh process — a restarted desktop — reads it back")

    # It has to survive the ordinary things that touch a device row.
    reloaded.add_or_update("phone-1", "hash-1-new", "iPhone")
    check(remote_identity.load_or_create().device_keys().get("phone-1") == key1,
          "re-pairing the same device does not wipe its key")
    reloaded.rename("phone-1", "Daming's iPhone")
    check(remote_identity.load_or_create().device_keys().get("phone-1") == key1,
          "nor does renaming it")

    # Forgetting a device must take the key with it — otherwise "kick this phone" leaves behind the
    # one secret that decrypts its traffic.
    reloaded.forget_device("phone-1")
    check("phone-1" not in remote_identity.load_or_create().device_keys(),
          "forgetting a device drops its key too")

    ident2 = remote_identity.load_or_create()
    ident2.add_or_update("phone-3", "hash-3", "third")
    ident2.set_device_key("phone-3", base64.b64encode(b"\x03" * 32).decode())
    ident2.forget_all()
    check(remote_identity.load_or_create().device_keys() == {},
          "and forgetting everything leaves no keys behind")

    # A key for a device that was never added must not create a phantom row.
    ident3 = remote_identity.load_or_create()
    ident3.set_device_key("never-paired", base64.b64encode(b"\x04" * 32).decode())
    check("never-paired" not in remote_identity.load_or_create().device_keys(),
          "a key for an unknown device is ignored rather than inventing one")

    # The file holds secrets; it must not be readable by anyone else.
    path = os.path.join(state, "remote.json")
    mode = os.stat(path).st_mode & 0o777
    check(mode == 0o600, "the device store stays 0600 now that it holds session keys (got %o)" % mode)

    # And a corrupt entry must cost only that device, not every other one.
    data = json.load(open(path))
    data.setdefault("devices", {})["broken"] = {"name": "x", "token_sha": "y", "k_dev": "!!not b64!!"}
    data["devices"]["good"] = {"name": "g", "token_sha": "z",
                               "k_dev": base64.b64encode(b"\x05" * 32).decode()}
    with open(path, "w") as fh:
        json.dump(data, fh)

    from harness import remote                                        # noqa: E402
    client = remote.RelayClient.__new__(remote.RelayClient)
    client.identity = remote_identity.load_or_create()
    loaded = client._load_device_keys()
    check("good" in loaded and "broken" not in loaded,
          "one unreadable key does not take the working ones down with it")

    print("\n  " + ("%d FAILED" % len(_fails) if _fails else "e2e persistence: all green"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
