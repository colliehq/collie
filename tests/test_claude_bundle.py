"""The Windows payload needs an authenticated native runtime, not an import-only pass."""
import base64
import hashlib
import importlib.util
import io
from pathlib import Path
import tarfile

import pytest

SPEC = importlib.util.spec_from_file_location("bundle_claude", Path(__file__).parents[1] / "installer" / "bundle_claude.py")
bundle = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bundle)


def archive(*, symlink=False):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in [("claude.exe", b"fixture executable"), ("LICENSE.md", b"fixture license")]:
            member = tarfile.TarInfo("package/" + name)
            member.size = len(data)
            if symlink and name == "claude.exe":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
            tar.addfile(member, io.BytesIO(data))
        member = tarfile.TarInfo("../../outside")
        member.size = 4
        tar.addfile(member, io.BytesIO(b"nope"))
    data = buf.getvalue()
    return data, base64.b64encode(hashlib.sha512(data).digest()).decode()


def test_bundle_copies_only_verified_native_files_and_license(tmp_path):
    data, digest = archive()
    target = tmp_path / "sdk" / "_bundled"
    bundle.install_archive(data, target, expected_sha512=digest)
    assert sorted(p.name for p in target.iterdir()) == ["LICENSE.md", "claude.exe"]
    assert (target / "claude.exe").read_bytes() == b"fixture executable"
    assert not (tmp_path / "outside").exists()


def test_integrity_failure_preserves_existing_runtime(tmp_path):
    target = tmp_path / "_bundled"
    target.mkdir()
    (target / "claude.exe").write_bytes(b"old")
    data, _ = archive()
    with pytest.raises(ValueError, match="integrity"):
        bundle.install_archive(data, target, expected_sha512="wrong")
    assert (target / "claude.exe").read_bytes() == b"old"


def test_native_executable_cannot_be_an_archive_link(tmp_path):
    data, digest = archive(symlink=True)
    with pytest.raises(ValueError, match="archive member"):
        bundle.install_archive(data, tmp_path / "_bundled", expected_sha512=digest)
