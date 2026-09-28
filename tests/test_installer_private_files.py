"""The actual Inno compiler must exclude credentials created after payload verification."""
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Inno Setup runs on Windows")
def test_installer_never_compiles_runtime_tokens_or_bytecode(tmp_path):
    compiler = shutil.which("iscc") or r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
    if not Path(compiler).is_file():
        pytest.skip("Inno Setup compiler is not installed")
    source = (Path(__file__).resolve().parents[1] / "installer/collie.iss").read_text('utf-8')
    entry = next(line for line in source.splitlines() if line.startswith('Source: "payload\\python\\*"'))
    public = ('Lib/site-packages/harness/cli.py', 'Lib/site-packages/harness/browser_ext/background.js',
              'Lib/site-packages/harness/webui/index.html', 'Lib/site-packages/unrelated/token.txt')
    private = ('Lib/site-packages/harness/browser_ext/token.txt',
               'Lib/site-packages/harness/browser_ext/auth.js',
               'Lib/site-packages/harness/__pycache__/cli.cpython-312.pyc',
               'Lib/site-packages/other/legacy.pyc')
    for name in public + private:
        path = tmp_path / 'payload/python' / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('fixture-' + name)
    script = tmp_path / 'excludes.iss'
    script.write_text('[Setup]\nAppName=Collie packaging fixture\nAppVersion=0.0.0\n'
                      'DefaultDirName={tmp}\\CollieFixture\nUninstallable=no\n'
                      'OutputDir=output\nOutputBaseFilename=excludes\n'
                      '[Files]\n' + entry + '\n')
    result = subprocess.run([compiler, str(script)], capture_output=True, text=True, errors="replace",
                            timeout=30, creationflags=0x08000000)
    assert result.returncode == 0, result.stdout + result.stderr
    compressed = [line.strip().lower().replace('\\', '/') for line in result.stdout.splitlines()
                  if 'Compressing:' in line]
    for name in public:
        assert any(line.endswith('/' + name.lower()) for line in compressed), name
    for name in private:
        assert not any(line.endswith('/' + name.lower()) for line in compressed), name
