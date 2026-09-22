"""Stage the pinned native Windows Claude CLI missing from SDK sdist installs.

Only the installer calls this helper. Downloads use an immutable npm release and
its checked-in SHA-512 integrity; no system installation or login is changed.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

VERSION = "2.1.278"
URL = ("https://registry.npmjs.org/@anthropic-ai/claude-code-win32-x64/-/"
       "claude-code-win32-x64-" + VERSION + ".tgz")
SHA512 = "8gwotnfAqgtuayi0m/Ajy5k+fochciv9Erdn7iqaCHM321cz5nOnJy0yCCkk4DsT0nG7kq67dOPXE9N4jdPz7Q=="
MAX_DOWNLOAD = 256 * 1024 * 1024
MAX_BINARY = 400 * 1024 * 1024


def install_archive(data: bytes, destination: Path, *, expected_sha512: str = SHA512):
    actual = base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")
    if not hmac.compare_digest(actual, expected_sha512):
        raise ValueError("Claude CLI archive integrity mismatch")
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
        # Never extract paths or links supplied by an archive. Copy only these
        # known ordinary files, after checking every requested member first.
        selected = []
        for name, limit in (("claude.exe", MAX_BINARY), ("LICENSE.md", 1024 * 1024)):
            member = archive.getmember("package/" + name)
            if not member.isfile() or not 0 < member.size <= limit:
                raise ValueError("Invalid Claude CLI archive member: " + name)
            selected.append((name, member))
        for name, member in selected:
            with archive.extractfile(member) as source:
                fd, temporary = tempfile.mkstemp(prefix=name + ".", dir=destination)
                try:
                    with os.fdopen(fd, "wb") as target:
                        shutil.copyfileobj(source, target)
                    os.replace(temporary, destination / name)
                finally:
                    if os.path.exists(temporary):
                        os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--site-packages", required=True, type=Path)
    args = parser.parse_args()
    sdk = args.site_packages.resolve() / "claude_agent_sdk"
    if not (sdk / "__init__.py").is_file():
        parser.error("staged claude_agent_sdk package is missing")
    with urllib.request.urlopen(URL, timeout=120) as response:
        data = response.read(MAX_DOWNLOAD + 1)
    if len(data) > MAX_DOWNLOAD:
        raise ValueError("Claude CLI archive exceeds its download limit")
    destination = sdk / "_bundled"
    install_archive(data, destination)
    result = subprocess.run([str(destination / "claude.exe"), "--version"],
                            capture_output=True, text=True, timeout=30,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if result.returncode or not result.stdout.strip().startswith(VERSION + " "):
        raise RuntimeError("Staged Claude CLI version check failed")
    print("Claude native CLI " + VERSION + " staged and verified")


if __name__ == "__main__":
    main()
