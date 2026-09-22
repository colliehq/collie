"""Measure the executable actually selected by the SDK, separately from native CLI."""
import datetime as dt
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess

def sdk_cli():
    from claude_agent_sdk._internal.transport.subprocess_cli import SubprocessCLITransport
    return Path(SubprocessCLITransport.__new__(SubprocessCLITransport)._find_cli()).resolve()

def describe(path):
    path = Path(path).resolve()
    sha = hashlib.sha256()
    with path.open('rb') as binary:
        for block in iter(lambda: binary.read(1024*1024), b''):
            sha.update(block)
    return {'filename': path.name,
            'version': subprocess.check_output([str(path),'--version'],text=True).strip(),
            'sha256': sha.hexdigest(), 'bytes': path.stat().st_size,
            'modified_utc': dt.datetime.fromtimestamp(path.stat().st_mtime,dt.timezone.utc).isoformat()}

def inventory(native_cli):
    return {'observed_at_utc': dt.datetime.now(dt.timezone.utc).isoformat(),
            'sdk_package': importlib.metadata.version('claude-agent-sdk'),
            'sdk_cli': describe(sdk_cli()), 'native_cli': describe(native_cli)}

if __name__ == '__main__':
    import os
    root = Path(__file__).resolve().parent
    native = Path(os.environ['APPDATA'])/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    data = inventory(native)
    data['interpretation'] = ('First-window native CLI and SDK use different installations. '
        'This is an inventory made after those runs, not a per-call runtime attestation. '
        'Historical manifests labelled only the native CLI version.')
    (root/'first-window-runtime-audit.json').write_text(json.dumps(data,indent=2),encoding='utf-8')
    print(json.dumps(data))
