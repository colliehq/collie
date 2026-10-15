"""Single-runtime packaging boundaries, using synthetic owned files only."""
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import zipfile

import pytest

ROOT=Path(__file__).resolve().parents[1]


def tool():
    spec=importlib.util.spec_from_file_location('runtime_workspace_fixture',ROOT/'tools/runtime_workspace.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def fixture_source(tmp_path,monkeypatch):
    module=tool()
    (tmp_path/'harness/browser_ext').mkdir(parents=True)
    (tmp_path/'bench/results').mkdir(parents=True)
    (tmp_path/'tools').mkdir()
    files={'harness/__init__.py':'__version__ = "0.0.1"\n','harness/cli.py':'def main(): pass\n',
           'harness/browser_ext/popup.html':'<html>fixture</html>','bench/results/frozen.json':'{"fixture":true}'}
    for path,body in files.items():(tmp_path/path).write_text(body,encoding='utf-8')
    (tmp_path/'tools/runtime-resources.json').write_text(json.dumps({'harness_assets':['harness/browser_ext/popup.html'],
        'bench_resources':['bench/results/frozen.json']}),encoding='utf-8')
    monkeypatch.setattr(module,'ROOT',tmp_path);monkeypatch.setattr(module,'RESOURCE_MANIFEST',tmp_path/'tools/runtime-resources.json')
    return module,files


def test_inventory_never_reads_live_client_or_unapproved_state(tmp_path,monkeypatch):
    module,files=fixture_source(tmp_path,monkeypatch)
    private=['harness/google_oauth_client.json','harness/browser_ext/token.txt','harness/browser_ext/auth.js','bench/results/new-live-run.json']
    for name in private:(tmp_path/name).write_text('synthetic private fixture',encoding='utf-8')
    original=Path.read_bytes
    def bounded(path):
        if path.relative_to(tmp_path).as_posix() in private:raise AssertionError('Unapproved private fixture was read')
        return original(path)
    monkeypatch.setattr(Path,'read_bytes',bounded)
    assert set(module.source_inventory())==set(files)


def test_generated_client_requires_explicit_desktop_schema(tmp_path,monkeypatch):
    module,_=fixture_source(tmp_path,monkeypatch)
    path=tmp_path/'harness/google_oauth_client.json'
    path.write_text(json.dumps({'installed':{'client_id':'synthetic.apps.googleusercontent.com','client_secret':'synthetic-public-fixture'}}))
    assert module.google_client(path)==path.read_bytes()
    path.write_text(json.dumps({'installed':{'client_id':'synthetic','client_secret':'fixture','refresh_token':'synthetic-account-data'}}))
    with pytest.raises(ValueError,match='account/token'):module.google_client(path)
    path.write_text(json.dumps({'web':{'client_id':'synthetic'}}))
    with pytest.raises(ValueError,match='Desktop-app'):module.google_client(path)


def make_wheel(path,files):
    with zipfile.ZipFile(path,'w') as archive:
        for name,body in files.items():archive.writestr(name,body)
        archive.writestr('collie_harness-0.0.1.dist-info/METADATA','Metadata-Version: 2.4\nName: collie-harness\nVersion: 0.0.1\n')
        archive.writestr('collie_harness-0.0.1.dist-info/entry_points.txt','[console_scripts]\ncollie = harness.cli:main\n')


def test_artifact_checks_exact_bytes_and_rejects_bearer_payload(tmp_path):
    module=tool();files={'harness/__init__.py':b'fixture','bench/results/frozen.json':b'{}'}
    expected={name:module.digest(body) for name,body in files.items()}
    path=tmp_path/'runtime.whl';make_wheel(path,files)
    assert module.audit_artifact(path,expected)['status']=='passed'
    make_wheel(path,{**files,'harness/browser_ext/token.txt':b'synthetic-owned-fixture'})
    with pytest.raises(ValueError,match='Credential/retired'):module.audit_artifact(path,expected)
    make_wheel(path,{**files,'harness/__init__.py':b'changed'})
    with pytest.raises(ValueError,match='byte mismatch'):module.audit_artifact(path,expected)


def test_sdist_rejects_links_and_missing_runtime(tmp_path):
    module=tool();path=tmp_path/'runtime.tar.gz'
    with tarfile.open(path,'w:gz') as archive:
        member=tarfile.TarInfo('runtime/harness/link.py');member.type=tarfile.SYMTYPE;member.linkname='../../outside';archive.addfile(member)
    with pytest.raises(ValueError,match='links'):module.audit_artifact(path,{})
    with tarfile.open(path,'w:gz') as archive:
        member=tarfile.TarInfo('/harness/file.py');member.size=1;archive.addfile(member,io.BytesIO(b'x'))
    with pytest.raises(ValueError,match='Unsafe'):module.audit_artifact(path,{})


def test_source_plugin_support_is_an_allowlist_not_live_host_state(tmp_path,monkeypatch):
    module,_=fixture_source(tmp_path,monkeypatch)
    leaf=tmp_path/'plugins/codex';leaf.mkdir(parents=True)
    for name in ('config.example.json','.mcp.json','config.json','credentials.json'):(leaf/name).write_text('{}')
    allowed=module.support_files()
    assert 'plugins/codex/config.example.json' in allowed and 'plugins/codex/.mcp.json' in allowed
    assert 'plugins/codex/config.json' not in allowed and 'plugins/codex/credentials.json' not in allowed


def test_manifest_is_bound_to_public_base_and_exact_resource_paths():
    module=tool();manifest=module.resource_manifest()
    assert manifest['public_source_commit']=='a0d6522ebef39304f8eee0d72cbd68d37a883783'
    assert len(manifest['bench_resources'])==700
    assert len([p for p in manifest['bench_resources'] if p.startswith('bench/results/normalized-harness-v1-886831826162/') and p.endswith('.json')])==39
    assert all(not any(part in p for part in ('token.txt','auth.js','google_oauth_client.json')) for p in manifest['harness_assets'])
    assert all('public_git_blob' in row and len(row['public_git_blob'])==40 for row in manifest['public_source_bindings'])
