"""Trusted local extension packages for Collie's Library.

The Library is deliberately a package *lifecycle*, not another directory that the model may read.
An extension is inert after installation.  It becomes visible to Collie only after the exact
package digest and its declared authority have been reviewed, and any byte or scope change returns
it to pending review.  Packages may currently contribute data-plane-neutral Skills, trusted hook
definitions, connection descriptors, templates, and assets; they cannot ship arbitrary Python,
workers, tools, credentials, or risk overrides.

Package layout::

    collie-extension.json
    skills/release/SKILL.md
    hooks.json
    ... every other file explicitly listed in manifest.files

The deterministic SHA-256 printed by ``collie library validate`` is the provenance pin used for
private/team distribution.  Public discovery and publisher signatures remain a distribution-layer
concern; the runtime never treats an unpinned download as trusted merely because it installed.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import json
import os
import posixpath
import re
import shutil
import sys
import tempfile
import threading
import time
import unicodedata
from urllib.parse import urlparse

from . import __version__


MANIFEST = "collie-extension.json"
SCHEMA_VERSION = 1
_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9._-]{0,126}[a-z0-9])?$")
_VERSION_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)
_SECRET_REF_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")
_TOP_KEYS = {
    "schema_version", "id", "name", "version", "publisher", "description", "license",
    "collie", "platforms", "files", "components", "permissions", "data", "verification",
}
_COMPONENT_KEYS = {"skills", "hooks", "connections", "templates", "assets"}
_PERMISSION_KEYS = {
    "filesystem", "network", "credentials", "browser", "desktop", "external_actions",
    "host_hooks",
}
_DATA_KEYS = {"retention", "export", "uninstall"}
_REGISTRY_SCHEMA = 1
_PROCESS_LOCK = threading.RLock()
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_FILES = 512
_MAX_FILE_BYTES = 32 * 1024 * 1024
_MAX_PACKAGE_BYTES = 128 * 1024 * 1024


class ExtensionError(ValueError):
    """A reviewable package or lifecycle error."""


def _pid_alive(pid: int) -> bool:
    """Read-only process liveness check (``os.kill(pid, 0)`` is destructive on Windows)."""
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if sys.platform.startswith("win"):
        try:
            import ctypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
            kernel.OpenProcess.restype = ctypes.c_void_p
            handle = kernel.OpenProcess(0x1000, 0, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel.CloseHandle(ctypes.c_void_p(handle))
                return True
            return ctypes.get_last_error() == 5  # access denied still proves a process exists
        except Exception:
            return True  # uncertainty must not steal another process's lock
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def _json_bytes(value) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")


def _semver(value: str) -> tuple:
    raw = str(value or "")
    if len(raw) > 128:
        raise ExtensionError("version must be at most 128 characters")
    match = _VERSION_RE.fullmatch(raw)
    if not match:
        raise ExtensionError("version must be semantic MAJOR.MINOR.PATCH")
    pre = match.group(4)
    parts = []
    for item in (pre.split(".") if pre else []):
        if item.isdigit():
            if len(item) > 1 and item.startswith("0"):
                raise ExtensionError("numeric pre-release identifiers may not have leading zeros")
            parts.append((0, int(item)))
        else:
            parts.append((1, item))
    # A release sorts after its pre-release. Build metadata is not precedence under SemVer, but is
    # a final deterministic tie-breaker because the registry can hold both byte-distinct labels.
    return (int(match.group(1)), int(match.group(2)), int(match.group(3)),
            1 if pre is None else 0, tuple(parts), match.group(5) or "")


def _platform_name() -> str:
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def _relative(value: str) -> str:
    raw = str(value or "").replace("\\", "/").strip()
    clean = posixpath.normpath(raw)
    if (not raw or raw.startswith("/") or clean in ("", ".", "..")
            or clean.startswith("../") or ":" in clean or "\x00" in clean):
        raise ExtensionError("package path must stay relative: %r" % value)
    reserved = {"con", "prn", "aux", "nul", "clock$",
                *("com%d" % index for index in range(1, 10)),
                *("lpt%d" % index for index in range(1, 10))}
    for segment in clean.split("/"):
        stem = segment.split(".", 1)[0].casefold()
        if (not segment or segment[-1:] in (" ", ".") or stem in reserved
                or any(ord(char) < 32 for char in segment)):
            raise ExtensionError("package path is not portable: %r" % value)
    return clean


def _inside(root: str, rel: str) -> str:
    path = os.path.abspath(os.path.join(root, *rel.split("/")))
    base = os.path.abspath(root)
    if path != base and not path.startswith(base + os.sep):
        raise ExtensionError("package path escapes its root: %s" % rel)
    return path


def _file_sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(128 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _walk_package(root: str) -> list[str]:
    base_real = os.path.realpath(root)

    def _guard(path: str, label: str):
        resolved = os.path.realpath(path)
        if resolved != base_real and not resolved.startswith(base_real + os.sep):
            raise ExtensionError("package %s resolves outside its root: %s" % (label, path))

    found = []
    for current, dirs, files in os.walk(root, followlinks=False):
        _guard(current, "directory")
        for name in list(dirs):
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise ExtensionError("package may not contain symlinked directories: %s" % name)
            _guard(path, "directory")  # also catches Windows junctions/reparse-point escapes
        for name in files:
            path = os.path.join(current, name)
            if os.path.islink(path):
                raise ExtensionError("package may not contain symlinks: %s" % name)
            _guard(path, "file")
            if not os.path.isfile(path):
                raise ExtensionError("package entry is not a regular file: %s" % name)
            found.append(os.path.relpath(path, root).replace(os.sep, "/"))
    return sorted(found)


def package_digest(root: str, files: list[str] | None = None) -> str:
    """Hash paths and bytes so renames and content changes both invalidate approval."""
    root = os.path.abspath(root)
    files = sorted(files or _walk_package(root))
    digest = hashlib.sha256(b"collie-extension-package-v1\0")
    for rel in files:
        name = _relative(rel).encode("utf-8")
        # Length-prefix both the path and content digest.  Without a content boundary, an
        # adversarial file tail could be confused with the following path record even though the
        # ordinary happy-path hash looked deterministic.
        content = bytes.fromhex(_file_sha256(_inside(root, rel)))
        digest.update(len(name).to_bytes(4, "big")); digest.update(name)
        digest.update(len(content).to_bytes(4, "big")); digest.update(content)
    return digest.hexdigest()


def _string_list(value, label: str) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ExtensionError("%s must be an array of strings" % label)
    values = [str(item).strip() for item in value if str(item).strip()]
    if any("\x00" in item or len(item) > 2048 for item in values):
        raise ExtensionError("%s contains an invalid or overlong value" % label)
    return sorted(set(values))


def _check_for_embedded_secrets(value, path="manifest"):
    if isinstance(value, dict):
        for key, item in value.items():
            word = str(key).lower().replace("-", "_")
            if (any(token in word for token in ("password", "private_key", "api_key", "token",
                                                "secret"))
                    and word not in ("secret_ref", "credentials")):
                raise ExtensionError("%s.%s may reference a secret, never embed one" % (path, key))
            _check_for_embedded_secrets(item, "%s.%s" % (path, key))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_for_embedded_secrets(item, "%s[%d]" % (path, index))


def _strict_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ExtensionError("JSON contains a duplicate key: %s" % key)
        value[key] = item
    return value


def _read_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(
            fh,
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ExtensionError("JSON contains a non-finite number: %s" % value)),
        )


def _normal_permissions(raw) -> dict:
    if not isinstance(raw, dict):
        raise ExtensionError("permissions must be an object")
    unknown = sorted(set(raw) - _PERMISSION_KEYS)
    if unknown:
        raise ExtensionError("unsupported permission fields: %s" % ", ".join(unknown))
    out = {}
    for key in sorted(_PERMISSION_KEYS - {"host_hooks"}):
        out[key] = _string_list(raw.get(key, []), "permissions.%s" % key)
    if not isinstance(raw.get("host_hooks", False), bool):
        raise ExtensionError("permissions.host_hooks must be true or false")
    out["host_hooks"] = bool(raw.get("host_hooks", False))
    for ref in out["credentials"]:
        if not _SECRET_REF_RE.fullmatch(ref):
            raise ExtensionError("credential references must be uppercase names, not values: %s" % ref)
    return out


def _normal_components(raw, declared_files: set[str], permissions: dict) -> dict:
    if not isinstance(raw, dict):
        raise ExtensionError("components must be an object")
    unknown = sorted(set(raw) - _COMPONENT_KEYS)
    if unknown:
        raise ExtensionError(
            "unsupported executable component types: %s; packages cannot inject tools or workers"
            % ", ".join(unknown))
    out = {}
    for kind in ("skills", "hooks", "templates", "assets"):
        paths = [_relative(item) for item in _string_list(raw.get(kind, []),
                                                           "components.%s" % kind)]
        missing = sorted(set(paths) - declared_files)
        if missing:
            raise ExtensionError("%s paths are not declared files: %s" % (kind, ", ".join(missing)))
        out[kind] = paths
    for path in out["skills"]:
        if posixpath.basename(path) != "SKILL.md":
            raise ExtensionError("skill entry must point to a SKILL.md: %s" % path)
    for path in out["hooks"]:
        if not path.lower().endswith(".json"):
            raise ExtensionError("hook entry must be a JSON definition: %s" % path)
    if out["hooks"] and not permissions["host_hooks"]:
        raise ExtensionError("packages with hooks must declare permissions.host_hooks=true")

    connections = raw.get("connections", [])
    if not isinstance(connections, list):
        raise ExtensionError("components.connections must be an array")
    names, normalized = set(), []
    network = set(permissions["network"])
    credentials = set(permissions["credentials"])
    for index, item in enumerate(connections):
        if not isinstance(item, dict):
            raise ExtensionError("connection %d must be an object" % index)
        allowed = {"name", "url", "description", "auth", "secret_ref"}
        unknown = sorted(set(item) - allowed)
        if unknown:
            raise ExtensionError("connection %d has unsupported fields: %s" %
                                 (index, ", ".join(unknown)))
        name, url = str(item.get("name") or "").strip(), str(item.get("url") or "").strip()
        if not _ID_RE.fullmatch(name) or name in names:
            raise ExtensionError("connection names must be unique stable ids")
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ExtensionError("connection %s must use an uncredentialed https URL" % name)
        host = parsed.hostname.lower()
        host_allowed = host in network or any(
            entry.startswith("*.") and host.endswith(entry[1:].lower()) for entry in network)
        if not host_allowed:
            raise ExtensionError("connection %s host %s is absent from permissions.network" %
                                 (name, host))
        auth = str(item.get("auth") or "none").strip().lower()
        if auth not in ("none", "oauth", "api_key_env"):
            raise ExtensionError("connection %s auth must be none, oauth, or api_key_env" % name)
        secret_ref = str(item.get("secret_ref") or "").strip()
        if auth == "api_key_env" and (not secret_ref or secret_ref not in credentials):
            raise ExtensionError("connection %s secret_ref must be declared in permissions.credentials"
                                 % name)
        if auth != "api_key_env" and secret_ref:
            raise ExtensionError("connection %s uses secret_ref only with api_key_env" % name)
        names.add(name)
        normalized.append({"name": name, "url": url,
                           "description": str(item.get("description") or "")[:500],
                           "auth": auth, "secret_ref": secret_ref})
    out["connections"] = sorted(normalized, key=lambda row: row["name"])
    return out


def _compatibility(raw) -> dict:
    if not isinstance(raw, dict):
        raise ExtensionError("collie compatibility must be an object")
    unknown = sorted(set(raw) - {"min_version", "max_version"})
    if unknown:
        raise ExtensionError("unsupported collie compatibility fields: %s" % ", ".join(unknown))
    minimum = str(raw.get("min_version") or "0.0.0")
    maximum = str(raw.get("max_version") or "")
    current = _semver(__version__)
    # Build metadata is a deterministic registry tie-breaker, never SemVer precedence.
    if current[:-1] < _semver(minimum)[:-1]:
        raise ExtensionError("extension needs Collie >= %s (running %s)" % (minimum, __version__))
    if maximum and current[:-1] >= _semver(maximum)[:-1]:
        raise ExtensionError("extension needs Collie < %s (running %s)" % (maximum, __version__))
    return {"min_version": minimum, "max_version": maximum}


def _normal_verification(raw) -> list[dict]:
    """Normalize declared evidence without executing package-provided commands at install time."""
    if not isinstance(raw, list):
        raise ExtensionError("verification must be an array of evidence declarations")
    allowed = {"kind", "description", "command", "path"}
    supported = {"command", "pytest", "artifact", "manual", "url"}
    out = []
    for index, row in enumerate(raw):
        if not isinstance(row, dict) or set(row) - allowed:
            raise ExtensionError("verification %d has unsupported fields" % index)
        kind = str(row.get("kind") or "").strip()
        description = str(row.get("description") or "").strip()
        command = str(row.get("command") or "").strip()
        path = str(row.get("path") or "").strip()
        if kind not in supported or not (description or command or path):
            raise ExtensionError("verification %d needs a supported kind and evidence detail" % index)
        if path:
            path = _relative(path)
        if any(len(value) > 4096 or "\x00" in value
               for value in (description, command, path)):
            raise ExtensionError("verification %d contains an invalid or overlong value" % index)
        out.append({"kind": kind, "description": description,
                    "command": command, "path": path})
    return out


def validate_package(source: str) -> dict:
    """Validate without installing and return the exact review material."""
    root = os.path.abspath(os.path.expanduser(str(source or "")))
    if not os.path.isdir(root):
        raise ExtensionError("extension source must be a local directory")
    path = os.path.join(root, MANIFEST)
    try:
        if os.path.getsize(path) > _MAX_MANIFEST_BYTES:
            raise ExtensionError("manifest exceeds %d bytes" % _MAX_MANIFEST_BYTES)
        manifest = _read_json(path)
    except (OSError, ValueError) as exc:
        raise ExtensionError("cannot read %s: %s" % (MANIFEST, exc)) from exc
    if not isinstance(manifest, dict):
        raise ExtensionError("manifest must be a JSON object")
    unknown = sorted(set(manifest) - _TOP_KEYS)
    if unknown:
        raise ExtensionError("unknown manifest fields: %s" % ", ".join(unknown))
    _check_for_embedded_secrets(manifest)
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise ExtensionError("schema_version must be %d" % SCHEMA_VERSION)
    ext_id = str(manifest.get("id") or "").strip()
    if not _ID_RE.fullmatch(ext_id):
        raise ExtensionError("id must contain lowercase letters, numbers, dot, dash, or underscore")
    name = str(manifest.get("name") or "").strip()
    publisher = str(manifest.get("publisher") or "").strip()
    description = str(manifest.get("description") or "").strip()
    if not name or not publisher or not description:
        raise ExtensionError("name, publisher, and description are required")
    if (len(name) > 160 or len(publisher) > 160 or len(description) > 1000
            or any("\x00" in value for value in (name, publisher, description))):
        raise ExtensionError("name, publisher, or description is invalid or overlong")
    version = str(manifest.get("version") or "").strip(); _semver(version)
    license_name = str(manifest.get("license") or "").strip()
    if not license_name or len(license_name) > 160 or "\x00" in license_name:
        raise ExtensionError("license is required")
    compatibility = _compatibility(manifest.get("collie"))
    platforms = _string_list(manifest.get("platforms"), "platforms")
    if not platforms or any(item not in ("windows", "macos", "linux") for item in platforms):
        raise ExtensionError("platforms must contain windows, macos, and/or linux")
    if _platform_name() not in platforms:
        raise ExtensionError("extension does not support this platform (%s)" % _platform_name())

    actual_files = _walk_package(root)
    if len(actual_files) > _MAX_FILES:
        raise ExtensionError("package has more than %d files" % _MAX_FILES)
    sizes = [os.path.getsize(_inside(root, rel)) for rel in actual_files]
    if any(size > _MAX_FILE_BYTES for size in sizes) or sum(sizes) > _MAX_PACKAGE_BYTES:
        raise ExtensionError("package exceeds the per-file or total size limit")
    declared = [_relative(item) for item in _string_list(manifest.get("files"), "files")]
    if MANIFEST in declared:
        raise ExtensionError("files lists package content only; do not include %s" % MANIFEST)
    expected = sorted([MANIFEST] + declared)
    folded = [unicodedata.normalize("NFC", item).casefold() for item in expected]
    if len(folded) != len(set(folded)):
        raise ExtensionError("package paths must also be unique on case-insensitive filesystems")
    if actual_files != expected:
        missing = sorted(set(expected) - set(actual_files))
        extra = sorted(set(actual_files) - set(expected))
        parts = []
        if missing: parts.append("missing: %s" % ", ".join(missing))
        if extra: parts.append("undeclared: %s" % ", ".join(extra))
        raise ExtensionError("package file inventory differs (%s)" % "; ".join(parts))

    permissions = _normal_permissions(manifest.get("permissions"))
    components = _normal_components(manifest.get("components"), set(declared), permissions)
    # Skill discovery is recursive below every exported SKILL.md directory.  Therefore every
    # SKILL.md anywhere in the package must itself be an explicit Skill component; merely listing
    # a nested file in ``files`` must not smuggle an extra trusted instruction set under the parent.
    undeclared_skills = sorted(
        rel for rel in declared
        if posixpath.basename(rel) == "SKILL.md" and rel not in set(components["skills"])
    )
    if undeclared_skills:
        raise ExtensionError("SKILL.md files must be explicit components.skills entries: %s" %
                             ", ".join(undeclared_skills))
    if components["hooks"]:
        from .hooks import validate_config
        for rel in components["hooks"]:
            hook_path = _inside(root, rel)
            try:
                _read_json(hook_path)  # reject duplicate keys before the lenient Hook loader sees it
            except (OSError, ValueError) as exc:
                raise ExtensionError("invalid hook %s: %s" % (rel, exc)) from exc
            errors = validate_config(hook_path)
            if errors:
                raise ExtensionError("invalid hook %s: %s" % (rel, "; ".join(errors[:10])))
    data = manifest.get("data")
    if not isinstance(data, dict) or set(data) - _DATA_KEYS:
        raise ExtensionError("data must declare only retention, export, and uninstall")
    retention = str(data.get("retention") or "").strip()
    uninstall = str(data.get("uninstall") or "").strip()
    if retention not in ("none", "package", "user_state"):
        raise ExtensionError("data.retention must be none, package, or user_state")
    if uninstall not in ("remove", "retain_user_state") or not isinstance(data.get("export"), bool):
        raise ExtensionError("data must define export:boolean and uninstall policy")
    verification = _normal_verification(manifest.get("verification"))
    untracked_evidence = sorted(row["path"] for row in verification
                                if row["path"] and row["path"] not in declared)
    if untracked_evidence:
        raise ExtensionError("verification paths are not declared files: %s" %
                             ", ".join(untracked_evidence))
    normalized = {
        "schema_version": SCHEMA_VERSION, "id": ext_id, "name": name, "version": version,
        "publisher": publisher, "description": description[:1000], "license": license_name,
        "collie": compatibility, "platforms": platforms, "files": declared,
        "components": components, "permissions": permissions,
        "data": {"retention": retention, "export": data["export"], "uninstall": uninstall},
        "verification": verification,
    }
    file_hashes = {rel: _file_sha256(_inside(root, rel)) for rel in expected}
    digest = package_digest(root, expected)
    # A data file becoming an active Skill/Hook is an authority change even when its bytes and
    # coarse host permissions are unchanged, so the full component mapping is scope material.
    scope_material = {"permissions": permissions, "components": components}
    return {"root": root, "manifest": normalized, "digest": digest,
            "scope_hash": hashlib.sha256(_json_bytes(scope_material)).hexdigest(),
            "file_hashes": file_hashes, "files": expected}


def scaffold_package(destination: str, ext_id: str, name: str, publisher: str) -> dict:
    """Create a minimal data-only extension without overwriting an existing path."""
    destination = os.path.abspath(os.path.expanduser(str(destination or "")))
    ext_id, name, publisher = (str(ext_id or "").strip(), str(name or "").strip(),
                               str(publisher or "").strip())
    if not destination or os.path.exists(destination):
        raise ExtensionError("scaffold destination must not already exist")
    if not _ID_RE.fullmatch(ext_id) or not name or not publisher:
        raise ExtensionError("scaffold requires a valid --id, --name, and --publisher")
    parent = os.path.dirname(destination)
    os.makedirs(parent, exist_ok=True)
    temp = tempfile.mkdtemp(prefix=os.path.basename(destination) + "-", dir=parent)
    slug = re.sub(r"[^a-z0-9-]+", "-", ext_id.rsplit(".", 1)[-1].lower()).strip("-") \
        or "extension"
    rel = "skills/%s/SKILL.md" % slug
    try:
        skill = _inside(temp, rel)
        os.makedirs(os.path.dirname(skill), exist_ok=True)
        with open(skill, "x", encoding="utf-8", newline="\n") as fh:
            fh.write("---\nname: %s\ndescription: Describe when Collie should use this skill.\n"
                     "---\n\n# %s\n\nWrite the extension workflow here.\n" % (slug, name))
        manifest = {
            "schema_version": SCHEMA_VERSION, "id": ext_id, "name": name,
            "version": "0.1.0", "publisher": publisher,
            "description": "Describe the outcome this extension adds to Collie.",
            "license": "UNLICENSED",
            "collie": {"min_version": __version__, "max_version": ""},
            "platforms": ["windows", "macos", "linux"], "files": [rel],
            "components": {"skills": [rel], "hooks": [], "connections": [],
                           "templates": [], "assets": []},
            "permissions": {"filesystem": [], "network": [], "credentials": [],
                            "browser": [], "desktop": [], "external_actions": [],
                            "host_hooks": False},
            "data": {"retention": "none", "export": True, "uninstall": "remove"},
            "verification": [],
        }
        with open(os.path.join(temp, MANIFEST), "x", encoding="utf-8", newline="\n") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        report = validate_package(temp)
        os.replace(temp, destination); temp = ""
        report["root"] = destination
        return report
    finally:
        if temp and os.path.isdir(temp):
            shutil.rmtree(temp)


def _scope_diff(old: dict | None, new: dict) -> dict:
    old = old or {}
    before = old.get("manifest") or {}
    old_files = old.get("file_hashes") or {}
    new_files = new.get("file_hashes") or {}
    components_before = before.get("components") or {}
    components_after = new["manifest"]["components"]
    identity_before = {key: before.get(key) for key in ("name", "publisher", "description")}
    identity_after = {key: new["manifest"].get(key) for key in
                      ("name", "publisher", "description")}
    return {
        "from_version": before.get("version"),
        "to_version": new["manifest"]["version"],
        "added_files": sorted(set(new_files) - set(old_files)),
        "removed_files": sorted(set(old_files) - set(new_files)),
        "changed_files": sorted(path for path in set(old_files) & set(new_files)
                                if old_files[path] != new_files[path]),
        "permissions_before": before.get("permissions") or {},
        "permissions_after": new["manifest"]["permissions"],
        "permissions_changed": ((before.get("permissions") or {})
                                != new["manifest"]["permissions"]),
        "components_before": components_before,
        "components_after": components_after,
        "components_changed": components_before != components_after,
        "connections_before": components_before.get("connections") or [],
        "connections_after": components_after["connections"],
        "identity_before": identity_before,
        "identity_after": identity_after,
        "identity_changed": identity_before != identity_after,
    }


@dataclass
class _Lock:
    path: str
    timeout: float = 5.0

    def __enter__(self):
        deadline = time.monotonic() + self.timeout
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                self._token = "%d %.6f %d\n" % (
                    os.getpid(), time.time(), threading.get_ident())
                os.write(fd, self._token.encode("ascii"))
                os.close(fd); return self
            except FileExistsError:
                try:
                    with open(self.path, encoding="ascii", errors="ignore") as fh:
                        observed = fh.read()
                    owner = int((observed.split() or ["0"])[0])
                    age = time.time() - os.path.getmtime(self.path)
                    alive = _pid_alive(owner)
                    # A live copy/install must never lose its lock because it ran for a long time.
                    # PID reuse after a crash is safer to resolve explicitly than by letting two
                    # live writers race and allowing the older one to overwrite newer state.
                    stale = not alive
                except OSError:
                    stale = False
                except (ValueError, IndexError):
                    try: stale = time.time() - os.path.getmtime(self.path) > 60
                    except OSError: stale = False
                if stale:
                    try:
                        with open(self.path, encoding="ascii", errors="ignore") as fh:
                            unchanged = fh.read() == observed
                        if unchanged: os.unlink(self.path)
                    except OSError: pass
                    continue
                if time.monotonic() >= deadline:
                    raise ExtensionError("another Library operation is still running")
                time.sleep(.05)

    def __exit__(self, *_):
        try:
            with open(self.path, encoding="ascii", errors="ignore") as fh:
                owned = fh.read() == getattr(self, "_token", None)
            if owned: os.unlink(self.path)
        except OSError:
            pass


class ExtensionStore:
    """Atomic install and activation state under ``~/.collie/extensions``."""

    def __init__(self, state_dir: str | None = None):
        state = os.path.abspath(os.path.expanduser(
            state_dir or os.environ.get("COLLIE_STATE_DIR") or "~/.collie"))
        self.root = os.path.join(state, "extensions")
        self.packages = os.path.join(self.root, "packages")
        self.registry_path = os.path.join(self.root, "registry.json")
        self.lock_path = os.path.join(self.root, ".lock")

    def _empty(self) -> dict:
        return {"schema_version": _REGISTRY_SCHEMA, "extensions": {},
                "revocations": {}, "audit": []}

    def _load(self) -> dict:
        try:
            with open(self.registry_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            return self._empty()
        except (OSError, ValueError) as exc:
            raise ExtensionError("Library registry is unreadable: %s" % exc) from exc
        if not isinstance(data, dict) or data.get("schema_version") != _REGISTRY_SCHEMA:
            raise ExtensionError("Library registry has an unsupported schema")
        data.setdefault("extensions", {}); data.setdefault("revocations", {}); data.setdefault("audit", [])
        if (not isinstance(data["extensions"], dict)
                or not isinstance(data["revocations"], dict)
                or not isinstance(data["audit"], list)):
            raise ExtensionError("Library registry has invalid collection types")
        for ext_id, ext in data["extensions"].items():
            if (not isinstance(ext_id, str) or not isinstance(ext, dict)
                    or not isinstance(ext.get("versions", {}), dict)
                    or not isinstance(ext.get("activation_history", []), list)
                    or not isinstance(ext.get("active_version", ""), str)):
                raise ExtensionError("Library registry has an invalid extension record")
            if any(not isinstance(version, str) or not isinstance(record, dict)
                   for version, record in ext.get("versions", {}).items()):
                raise ExtensionError("Library registry has an invalid version record")
            for record in ext.get("versions", {}).values():
                if (not isinstance(record.get("manifest", {}), dict)
                        or not isinstance(record.get("file_hashes", {}), dict)
                        or not isinstance(record.get("version", ""), str)
                        or not isinstance(record.get("digest", ""), str)):
                    raise ExtensionError("Library registry has invalid version metadata")
        if (any(not isinstance(key, str) or not isinstance(value, dict)
                for key, value in data["revocations"].items())
                or any(not isinstance(row, dict) for row in data["audit"])):
            raise ExtensionError("Library registry has an invalid lifecycle record")
        return data

    def _save(self, data: dict):
        os.makedirs(self.root, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix="registry-", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.write("\n"); fh.flush(); os.fsync(fh.fileno())
            try: os.chmod(tmp, 0o600)
            except OSError: pass
            os.replace(tmp, self.registry_path)
        finally:
            try:
                if os.path.exists(tmp): os.unlink(tmp)
            except OSError: pass

    @contextmanager
    def _mutating(self):
        with _PROCESS_LOCK, _Lock(self.lock_path):
            data = self._load()
            yield data
            self._save(data)

    @staticmethod
    def _audit(data: dict, action: str, ext_id: str, version: str = "",
               digest: str = "", detail: dict | None = None):
        data.setdefault("audit", []).append({
            "at": time.time(), "action": action, "id": ext_id, "version": version,
            "digest": digest, "detail": detail or {},
        })
        data["audit"] = data["audit"][-1000:]

    def _package_path(self, ext_id: str, version: str) -> str:
        if not _ID_RE.fullmatch(ext_id):
            raise ExtensionError("invalid extension id")
        _semver(version)
        path = os.path.abspath(os.path.join(self.packages, ext_id, version))
        root = os.path.abspath(self.packages)
        if not path.startswith(root + os.sep):
            raise ExtensionError("invalid extension package path")
        return path

    def plan(self, source: str) -> dict:
        report = validate_package(source)
        data = self._load()
        ext = data["extensions"].get(report["manifest"]["id"]) or {}
        current = (ext.get("versions") or {}).get(ext.get("active_version"))
        return {"id": report["manifest"]["id"], "name": report["manifest"]["name"],
                "version": report["manifest"]["version"], "digest": report["digest"],
                "scope_hash": report["scope_hash"], "diff": _scope_diff(current, report),
                "permissions": report["manifest"]["permissions"],
                "components": report["manifest"]["components"]}

    def install(self, source: str, *, expected_digest: str = "", approve: bool = False) -> dict:
        report = validate_package(source)
        expected = str(expected_digest or "").lower().removeprefix("sha256:")
        if expected and (not re.fullmatch(r"[0-9a-f]{64}", expected)
                         or expected != report["digest"]):
            raise ExtensionError("package digest does not match the expected provenance pin")
        manifest = report["manifest"]
        ext_id, version = manifest["id"], manifest["version"]
        destination = self._package_path(ext_id, version)
        with self._mutating() as data:
            if report["digest"] in data.get("revocations", {}):
                raise ExtensionError("this package digest is revoked")
            ext = data["extensions"].setdefault(ext_id, {
                "id": ext_id, "name": manifest["name"], "publisher": manifest["publisher"],
                "description": manifest["description"], "enabled": False,
                "active_version": "", "activation_history": [], "versions": {},
            })
            existing = ext["versions"].get(version)
            if existing:
                if existing.get("digest") != report["digest"]:
                    raise ExtensionError("version %s is already installed with different bytes" % version)
                if not self._integrity(ext_id, existing):
                    raise ExtensionError(
                        "installed package fails integrity; disable and uninstall it before reinstalling")
                changed = False
                if expected and existing.get("trust_state") != "digest_pinned":
                    existing["trust_state"] = "digest_pinned"; changed = True
                if approve and not existing.get("approved"):
                    existing["approved"] = True; changed = True
                    if not expected and existing.get("trust_state") == "unreviewed":
                        existing["trust_state"] = "locally_reviewed"
                if changed:
                    self._audit(data, "review", ext_id, version, report["digest"],
                                {"approved": bool(existing.get("approved")),
                                 "pinned": bool(expected)})
                result = self._public_extension(ext, data)
                result["installed_version"] = version
                result["installed_digest"] = report["digest"]
                return result
            known_publisher = str(ext.get("publisher") or "")
            if known_publisher and manifest["publisher"] != known_publisher:
                raise ExtensionError(
                    "publisher for %s is already %s; a new id is required for %s" %
                    (ext_id, known_publisher, manifest["publisher"]))
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            if os.path.exists(destination):
                try:
                    same = (_walk_package(destination) == report["files"]
                            and package_digest(destination, report["files"]) == report["digest"])
                except (OSError, ExtensionError):
                    same = False
                if not same:
                    raise ExtensionError(
                        "an untracked package directory already occupies %s; inspect it before removal"
                        % destination)
            temp = tempfile.mkdtemp(prefix=version + "-", dir=os.path.dirname(destination))
            try:
                if not os.path.exists(destination):
                    for rel in report["files"]:
                        target = _inside(temp, rel)
                        os.makedirs(os.path.dirname(target), exist_ok=True)
                        shutil.copyfile(_inside(report["root"], rel), target)
                    if package_digest(temp, report["files"]) != report["digest"]:
                        raise ExtensionError("source changed while it was being installed")
                    os.replace(temp, destination)
                    temp = ""
            finally:
                if temp and os.path.isdir(temp): shutil.rmtree(temp)
            record = {
                "version": version, "digest": report["digest"],
                "scope_hash": report["scope_hash"], "manifest": manifest,
                "file_hashes": report["file_hashes"], "installed_at": time.time(),
                "source": os.path.abspath(report["root"]),
                "trust_state": ("digest_pinned" if expected else
                                "locally_reviewed" if approve else "unreviewed"),
                "approved": bool(approve),
            }
            ext["versions"][version] = record
            ext.update(name=manifest["name"], publisher=manifest["publisher"],
                       description=manifest["description"])
            self._audit(data, "install", ext_id, version, report["digest"],
                        {"approved": bool(approve), "pinned": bool(expected)})
        result = self.get(ext_id)
        # Operation metadata prevents callers from guessing which version this invocation handled.
        # In particular, an older reviewed package must never cause a newer pending version to be
        # selected merely because it is the registry's SemVer maximum.
        result["installed_version"] = version
        result["installed_digest"] = report["digest"]
        return result

    def _integrity(self, ext_id: str, record: dict) -> bool:
        path = self._package_path(ext_id, record.get("version") or "")
        try:
            expected = sorted((record.get("file_hashes") or {}).keys())
            # Exact inventory is part of the approval boundary.  Skill discovery walks below the
            # declared SKILL.md directory, so hashing only listed files would let a later, unlisted
            # nested skill join a previously approved package without changing its digest.
            return (_walk_package(path) == expected
                    and package_digest(path, expected) == record.get("digest"))
        except (OSError, ExtensionError):
            return False

    def _public_extension(self, ext: dict, data: dict) -> dict:
        versions = []
        for version, record in sorted((ext.get("versions") or {}).items(),
                                      key=lambda item: _semver(item[0]), reverse=True):
            row = {key: record.get(key) for key in
                   ("version", "digest", "scope_hash", "installed_at", "trust_state", "approved")}
            review = record.get("manifest") or {}
            review_components = review.get("components") or {}
            row["permissions"] = review.get("permissions") or {}
            row["components"] = {
                key: len(review_components.get(key) or []) for key in sorted(_COMPONENT_KEYS)}
            row["connections"] = review_components.get("connections") or []
            row["data"] = review.get("data") or {}
            row["revoked"] = record.get("digest") in data.get("revocations", {})
            row["integrity_ok"] = self._integrity(ext["id"], record)
            versions.append(row)
        active = (ext.get("versions") or {}).get(ext.get("active_version")) or {}
        # A first install is intentionally inactive, but its authority still has to be reviewable
        # before enable.  Fall back to the newest version for top-level presentation while keeping
        # every version's exact review material above.
        if not active and versions:
            active = (ext.get("versions") or {}).get(versions[0]["version"]) or {}
        manifest = active.get("manifest") or {}
        components = manifest.get("components") or {}
        history = [version for version in reversed(ext.get("activation_history") or [])
                   if version in (ext.get("versions") or {})
                   and version != ext.get("active_version")]
        return {
            "id": ext.get("id"), "name": manifest.get("name") or ext.get("name"),
            "publisher": manifest.get("publisher") or ext.get("publisher"),
            "description": manifest.get("description") or ext.get("description"),
            "enabled": bool(ext.get("enabled")),
            "active_version": ext.get("active_version") or "", "versions": versions,
            "rollback_version": history[0] if history else "",
            "permissions": manifest.get("permissions") or {},
            "components": {key: len(components.get(key) or []) for key in sorted(_COMPONENT_KEYS)},
        }

    def list(self) -> list[dict]:
        data = self._load()
        return [self._public_extension(ext, data) for _, ext in
                sorted(data["extensions"].items())]

    def get(self, ext_id: str) -> dict:
        data = self._load(); ext = data["extensions"].get(ext_id)
        if not ext: raise ExtensionError("extension is not installed: %s" % ext_id)
        return self._public_extension(ext, data)

    def enable(self, ext_id: str, version: str = "", *, approve: bool = False) -> dict:
        with self._mutating() as data:
            ext = data["extensions"].get(ext_id)
            if not ext: raise ExtensionError("extension is not installed: %s" % ext_id)
            if not version:
                version = max(ext["versions"], key=_semver)
            record = ext["versions"].get(version)
            if not record: raise ExtensionError("extension version is not installed: %s" % version)
            digest = record.get("digest") or ""
            if digest in data.get("revocations", {}):
                raise ExtensionError("this package digest is revoked")
            if not self._integrity(ext_id, record):
                raise ExtensionError("installed package bytes changed; reinstall and review them")
            if not record.get("approved") and not approve:
                raise ExtensionError("extension scopes are not approved; review plan then enable --approve")
            if approve:
                record["approved"] = True
                if record.get("trust_state") == "unreviewed":
                    record["trust_state"] = "locally_reviewed"
            # Hooks are host policy, not model tools.  Exact file bytes join the HookTrustStore only
            # after the package digest and scopes were approved here.
            hooks = (record.get("manifest") or {}).get("components", {}).get("hooks", [])
            if hooks:
                from .hooks import HookTrustStore
                trust = HookTrustStore(os.path.join(os.path.dirname(self.root), "hook_trust.json"))
                package = self._package_path(ext_id, version)
                for rel in hooks: trust.set(_inside(package, rel), True)
            previous = ext.get("active_version") or ""
            if previous and previous != version:
                history = ext.setdefault("activation_history", [])
                history.append(previous)
                ext["activation_history"] = history[-100:]
            ext["enabled"], ext["active_version"] = True, version
            self._audit(data, "enable", ext_id, version, digest,
                        {"permissions": record["manifest"]["permissions"]})
        return self.get(ext_id)

    def disable(self, ext_id: str) -> dict:
        with self._mutating() as data:
            ext = data["extensions"].get(ext_id)
            if not ext: raise ExtensionError("extension is not installed: %s" % ext_id)
            version = ext.get("active_version") or ""
            ext["enabled"] = False
            self._audit(data, "disable", ext_id, version)
        return self.get(ext_id)

    def rollback(self, ext_id: str, *, approve: bool = False) -> dict:
        data = self._load(); ext = data["extensions"].get(ext_id)
        if not ext: raise ExtensionError("extension is not installed: %s" % ext_id)
        active = ext.get("active_version")
        history = [version for version in reversed(ext.get("activation_history") or [])
                   if version in ext.get("versions", {}) and version != active]
        if not history:
            raise ExtensionError("no previously active version is available for rollback")
        return self.enable(ext_id, history[0], approve=approve)

    def uninstall(self, ext_id: str, version: str = "", *, force: bool = False) -> dict:
        removed = []
        with self._mutating() as data:
            ext = data["extensions"].get(ext_id)
            if not ext: raise ExtensionError("extension is not installed: %s" % ext_id)
            targets = [version] if version else list(ext.get("versions", {}))
            if ext.get("enabled") and ext.get("active_version") in targets and not force:
                raise ExtensionError("disable the active extension before uninstalling it")
            for item in targets:
                record = ext.get("versions", {}).get(item)
                if not record: raise ExtensionError("extension version is not installed: %s" % item)
                path = self._package_path(ext_id, item)
                if os.path.isdir(path): shutil.rmtree(path)
                ext["versions"].pop(item, None); removed.append(item)
                self._audit(data, "uninstall", ext_id, item, record.get("digest") or "")
            if ext.get("active_version") in removed:
                ext["enabled"], ext["active_version"] = False, ""
            ext["activation_history"] = [item for item in ext.get("activation_history", [])
                                         if item not in removed]
            if not ext.get("versions"):
                data["extensions"].pop(ext_id, None)
        return {"id": ext_id, "removed_versions": sorted(removed, key=_semver)}

    def revoke(self, ext_id: str, digest: str, reason: str) -> dict:
        digest = str(digest or "").lower().removeprefix("sha256:")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or not str(reason or "").strip():
            raise ExtensionError("revoke requires a SHA-256 digest and reason")
        with self._mutating() as data:
            ext = data["extensions"].get(ext_id)
            if not ext:
                raise ExtensionError("extension is not installed: %s" % ext_id)
            matches = [record for record in (ext.get("versions") or {}).values()
                       if record.get("digest") == digest]
            if not matches:
                raise ExtensionError("digest is not installed for extension: %s" % ext_id)
            data["revocations"][digest] = {"at": time.time(), "reason": str(reason)[:1000]}
            active = (ext.get("versions") or {}).get(ext.get("active_version")) or {}
            if active.get("digest") == digest:
                ext["enabled"] = False
            self._audit(data, "revoke", ext_id, digest=digest, detail={"reason": str(reason)[:1000]})
        return {"id": ext_id, "digest": digest, "revoked": True}

    def audit(self, limit: int = 100) -> list[dict]:
        return list(self._load().get("audit", []))[-max(1, min(1000, int(limit))):]

    def active_records(self) -> list[tuple[str, dict, str]]:
        """Integrity-checked active records: (id, registry record, package path)."""
        data, out = self._load(), []
        for ext_id, ext in sorted(data["extensions"].items()):
            if not ext.get("enabled"): continue
            record = (ext.get("versions") or {}).get(ext.get("active_version"))
            if (not record or not record.get("approved")
                    or record.get("digest") in data.get("revocations", {})
                    or not self._integrity(ext_id, record)):
                continue
            out.append((ext_id, record, self._package_path(ext_id, record["version"])))
        return out

    def component_paths(self, kind: str) -> list[str]:
        if kind not in ("skills", "hooks", "templates", "assets"):
            raise ExtensionError("unsupported component kind: %s" % kind)
        out = []
        for _, record, package in self.active_records():
            for rel in record["manifest"]["components"].get(kind, []):
                path = _inside(package, rel)
                # Skill discovery takes a directory; hook/template/asset consumers take files.
                out.append(os.path.dirname(path) if kind == "skills" else path)
        return sorted(set(out))

    def connections(self) -> list[dict]:
        out = []
        for ext_id, record, _ in self.active_records():
            for item in record["manifest"]["components"].get("connections", []):
                out.append(dict(item, extension_id=ext_id, extension_version=record["version"]))
        return sorted(out, key=lambda row: (row["extension_id"], row["name"]))


def enabled_component_paths(kind: str, state_dir: str | None = None) -> list[str]:
    """Best-effort runtime integration: a broken registry disables extensions, never Collie."""
    try:
        return ExtensionStore(state_dir).component_paths(kind)
    except Exception:
        return []


def registry_generation(state_dir: str | None = None) -> str:
    """Content generation for long-lived consumers that cache Library-derived state.

    Registry bytes cover lifecycle actions; active integrity bits also cover out-of-band package
    changes that do not rewrite the registry.  Computing those bits is intentionally exact rather
    than trusting timestamps: this value guards executable hooks and model-visible instructions.
    """
    store = ExtensionStore(state_dir)
    digest = hashlib.sha256(b"collie-extension-runtime-generation-v1\0")
    try:
        digest.update(bytes.fromhex(_file_sha256(store.registry_path)))
    except OSError:
        digest.update(b"missing-registry")
    try:
        data = store._load()
    except ExtensionError:
        digest.update(b"unreadable-registry")
        return digest.hexdigest()
    for ext_id, ext in sorted(data.get("extensions", {}).items()):
        if not ext.get("enabled"):
            continue
        version = str(ext.get("active_version") or "")
        record = (ext.get("versions") or {}).get(version) or {}
        digest.update(_json_bytes([ext_id, version, record.get("digest") or "",
                                  bool(store._integrity(ext_id, record))]))
    return digest.hexdigest()
