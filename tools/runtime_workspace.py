"""Build and audit the single Collie runtime; run bounded installed smoke probes.

Default builds use reviewed source/resource paths and never read generated
clients, user memory or account state. No internal distributions are involved.
"""
from __future__ import annotations

import argparse
import ast
import datetime
import fnmatch
import hashlib
import importlib.metadata
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / '.runtime-build'
RESOURCE_MANIFEST = ROOT / 'tools/runtime-resources.json'
CLIENT = 'harness/google_oauth_client.json'
DENIED = ('harness/browser_ext/token.txt', 'harness/browser_ext/auth.js',
          'harness/browser_ext/*.txt', 'harness/oauth_proxy.py', 'harness/oauth_ext/**')


def digest(raw): return hashlib.sha256(raw).hexdigest()
def utc(): return datetime.datetime.now(datetime.timezone.utc).isoformat()
def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
def version():
    for node in ast.walk(ast.parse((ROOT/'harness/__init__.py').read_text(encoding='utf-8'))):
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == '__version__' for t in node.targets):
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str): return node.value.value
    raise ValueError('Missing literal harness.__version__')
def resource_manifest():
    return json.loads(RESOURCE_MANIFEST.read_text(encoding='utf-8'))
def source_inventory():
    manifest = resource_manifest()
    allowed = set(manifest['harness_assets']) | set(manifest['bench_resources'])
    allowed.update(path.relative_to(ROOT).as_posix() for path in (ROOT/'harness').rglob('*.py')
                   if '__pycache__' not in path.parts)
    found = {}
    for name in sorted(allowed):
        if name == CLIENT or any(fnmatch.fnmatchcase(name, p) for p in DENIED): continue
        path = ROOT/name
        resolved = path.resolve()
        if not resolved.is_relative_to(ROOT) or not path.is_file(): raise ValueError('Missing/unsafe approved source: '+name)
        found[name] = digest(path.read_bytes())
    return found


def support_files():
    names=['pyproject.toml','MANIFEST.in','README.md','LICENSE','tools/runtime_workspace.py',
           'tools/runtime-resources.json','installer/write_google_oauth_client.py',
           '.agents/plugins/marketplace.json','.claude-plugin/marketplace.json']
    fixed={'README.md','INSTALL_LOCAL.md','config.example.json','marketplace.example.json',
           '.mcp.json','.codex-plugin/plugin.json','.claude-plugin/plugin.json'}
    for leaf in ('codex','claude-code','codex-memory','claude-code-memory'):
        base=ROOT/'plugins'/leaf
        for path in base.rglob('*'):
            if path.is_file():
                relative=path.relative_to(base).as_posix()
                if relative in fixed or relative.startswith('skills/') and relative.endswith('/SKILL.md'):
                    names.append(path.relative_to(ROOT).as_posix())
    return sorted(set(names))


def google_client(path):
    if path.resolve() != (ROOT/CLIENT).resolve() or not path.resolve().is_relative_to(ROOT):
        raise ValueError('Only the explicit repository release-client path is accepted')
    raw = path.read_bytes()
    if len(raw) > 65536: raise ValueError('Release client exceeds size limit')
    value = json.loads(raw)
    if not isinstance(value, dict) or set(value) != {'installed'} or not isinstance(value['installed'], dict):
        raise ValueError('Expected a Desktop-app installed client, not account credentials')
    client = value['installed']
    allowed = {'client_id','project_id','auth_uri','token_uri','auth_provider_x509_cert_url','client_secret','redirect_uris'}
    if set(client)-allowed or not {'client_id','client_secret'} <= set(client):
        raise ValueError('Unexpected OAuth account/token fields or missing client schema')
    if not all(isinstance(client[k],str) and client[k] for k in ('client_id','client_secret')):
        raise ValueError('Invalid Desktop client fields')
    if any(k in client and (not isinstance(client[k],str) or not client[k].startswith('https://')) for k in ('auth_uri','token_uri')):
        raise ValueError('Client authorization/token endpoints must use HTTPS')
    if 'redirect_uris' in client and (not isinstance(client['redirect_uris'],list) or not client['redirect_uris'] or not all(isinstance(u,str) and u.startswith(('http://localhost','http://127.0.0.1')) for u in client['redirect_uris'])):
        raise ValueError('Only installed-client loopback redirects are accepted')
    if (ROOT/'.git').exists():
        tracked=subprocess.run(['git','ls-files','--error-unmatch','--',CLIENT],cwd=ROOT,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        if tracked.returncode==0:raise ValueError('Release-generated clients must never be Git tracked')
    return raw


def execute(command, cwd, env, log, timeout=300):
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('w', encoding='utf-8') as stream:
        stream.write(json.dumps({'command':command,'cwd':str(cwd),'started_at':utc()})+'\n'); stream.flush()
        process = subprocess.Popen(command,cwd=cwd,env=env,stdout=stream,stderr=subprocess.STDOUT,
                                   creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        try: code=process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name=='nt': subprocess.run(['taskkill.exe','/PID',str(process.pid),'/T','/F'],stdout=stream,stderr=subprocess.STDOUT)
            else: process.kill()
            process.wait(); code=None
    return {'status':'passed' if code==0 else 'timeout' if code is None else 'failed',
            'exit_code':code,'log':str(log),'log_sha256':digest(log.read_bytes())}


def audit_artifact(path, expected, client=False):
    if path.name.endswith('.whl'):
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            members={name:archive.read(name) for name in archive.namelist() if not name.endswith('/')}
            assert len(members)==len(archive.namelist())
    else:
        with tarfile.open(path,'r:gz') as archive:
            members={}
            for member in archive.getmembers():
                if member.name.startswith('/') or re.match(r'^[A-Za-z]:',member.name) or '..' in Path(member.name).parts or '\\' in member.name:
                    raise ValueError('Unsafe archive member')
                if member.issym() or member.islnk(): raise ValueError('Source archive links are not allowed')
                if member.isfile():
                    name=member.name.split('/',1)[1]
                    if name in members:raise ValueError('Duplicate source archive member')
                    members[name]=archive.extractfile(member).read()
    for name in members:
        if name.startswith('/') or re.match(r'^[A-Za-z]:',name) or '..' in Path(name).parts or '\\' in name: raise ValueError('Unsafe archive member')
        if any(fnmatch.fnmatchcase(name,pattern) for pattern in DENIED): raise ValueError('Credential/retired asset in artifact: '+name)
        if name==CLIENT and not client: raise ValueError('Implicit generated client in artifact')
    actual={name:digest(raw) for name,raw in members.items() if name.startswith(('harness/','bench/'))}
    if actual != expected: raise ValueError('Artifact source/resource byte mismatch: '+str(sorted(set(actual)^set(expected))))
    if path.name.endswith('.whl'):
        from email.parser import BytesParser
        metadata=BytesParser().parsebytes(next(raw for name,raw in members.items() if name.endswith('.dist-info/METADATA')))
        mandatory=[r for r in metadata.get_all('Requires-Dist',[]) if 'extra ==' not in r and 'extra==' not in r]
        if mandatory: raise ValueError('Default runtime dependencies must be empty')
        if any(re.match(r'collie[-_]',r,re.I) for r in metadata.get_all('Requires-Dist',[])): raise ValueError('Internal component dependency is forbidden')
        entry=next(raw.decode() for name,raw in members.items() if name.endswith('.dist-info/entry_points.txt'))
        if 'collie = harness.cli:main' not in entry: raise ValueError('Unexpected console owner')
    return {'path':str(path),'sha256':digest(path.read_bytes()),'runtime_resource_files':len(actual),'status':'passed'}


def build(include_client=False):
    expected=source_inventory()
    attempt=BUILD/('build-'+uuid.uuid4().hex[:10]); stage=attempt/'source'; stage.mkdir(parents=True)
    for name in expected:
        target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    support=support_files()
    for name in support:
        if not (ROOT/name).resolve().is_relative_to(ROOT):raise ValueError('Support source escapes workspace')
        target=stage/name;target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(ROOT/name,target)
    release=None
    if include_client:
        raw=google_client(ROOT/CLIENT);(stage/CLIENT).write_bytes(raw);expected[CLIENT]=digest(raw)
        cfg=(stage/'pyproject.toml').read_text(encoding='utf-8')
        prefix,data=cfg.split('[tool.setuptools.package-data]',1)
        data=data.replace('harness = [','harness = ["google_oauth_client.json", ',1)
        before,exclude=data.split('[tool.setuptools.exclude-package-data]',1)
        exclude=exclude.replace('"google_oauth_client.json", ','').replace(', "google_oauth_client.json"','')
        (stage/'pyproject.toml').write_text(prefix+'[tool.setuptools.package-data]'+before+'[tool.setuptools.exclude-package-data]'+exclude,encoding='utf-8')
        mf=(stage/'MANIFEST.in').read_text(encoding='utf-8').replace('exclude harness/google_oauth_client.json','include harness/google_oauth_client.json')
        (stage/'MANIFEST.in').write_text(mf,encoding='utf-8')
        release={'client_sha256':digest(raw),'writer_sha256':digest((ROOT/'installer/write_google_oauth_client.py').read_bytes()),'schema':'installed-desktop-client','producer_execution_attested':False}
    result=execute([sys.executable,'-m','build','--no-isolation','--outdir',str(attempt/'dist'),str(stage)],attempt,os.environ.copy(),attempt/'build.log')
    receipt={'schema':'collie.single-runtime-build.v1','generated_at':utc(),'version':version(),'build':result,
             'source_manifest_sha256':digest(RESOURCE_MANIFEST.read_bytes()),'source_hashes':expected,
             'support_hashes':{name:digest((ROOT/name).read_bytes()) for name in support},
             'release_client':release,'artifacts':[],'complete':False}
    write(attempt/'receipt.json',receipt)
    if result['status']!='passed':raise ValueError('Runtime build failed; see '+result['log'])
    artifacts=sorted((attempt/'dist').iterdir())
    assert len(artifacts)==2 and sum(p.suffix=='.whl' for p in artifacts)==1 and sum(p.name.endswith('.tar.gz') for p in artifacts)==1
    receipt['artifacts']=[audit_artifact(path,expected,include_client) for path in artifacts]
    (ROOT/'dist').mkdir(exist_ok=True)
    for path in artifacts:shutil.copyfile(path,ROOT/'dist'/path.name)
    receipt.update(status='passed',complete=True,attempt_directory=str(attempt))
    write(attempt/'receipt.json',receipt);write(BUILD/'build-receipt.json',receipt)
    return receipt


def isolated_environment(base):
    for leaf in ('home','state','tmp','appdata','localappdata','notes','hf'):(base/leaf).mkdir(parents=True,exist_ok=True)
    env={k:v for k,v in os.environ.items() if not re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH|PROXY',k,re.I) and not k.startswith(('COLLIE_','CODEX_','CLAUDE_')) and k not in ('PYTHONPATH','PYTHONHOME')}
    env.update(HOME=str(base/'home'),USERPROFILE=str(base/'home'),APPDATA=str(base/'appdata'),LOCALAPPDATA=str(base/'localappdata'),
        TEMP=str(base/'tmp'),TMP=str(base/'tmp'),TMPDIR=str(base/'tmp'),COLLIE_STATE_DIR=str(base/'state'),COLLIE_DATA_DIR=str(base/'state/data'),
        COLLIE_SETTINGS_PATH=str(base/'state/settings.json'),COLLIE_SESSIONS_DIR=str(base/'state/sessions'),COLLIE_NOTES_DIR=str(base/'notes'),
        COLLIE_PROVIDER='mock',COLLIE_EMBED='bm25',COLLIE_BROWSER='off',COLLIE_BROWSER_BRIDGE='0',COLLIE_BROWSER_LIVE='0',COLLIE_DESKTOP_CONTROL='0',
        COLLIE_BROWSER_BRIDGE_NOSPAWN='1',COLLIE_NO_APPLE_EVENTS='1',HF_HUB_OFFLINE='1',HF_HOME=str(base/'hf'),PIP_NO_INDEX='1',PIP_DISABLE_PIP_VERSION_CHECK='1',
        PYTHONUNBUFFERED='1',PYTHONDONTWRITEBYTECODE='1')
    return env


GUARD = r'''import os,pathlib,re,socket,sys
_root=pathlib.Path(os.environ['COLLIE_RUNTIME_TEST_ROOT']).resolve()
_runtime=[pathlib.Path(sys.prefix).resolve(),pathlib.Path(sys.base_prefix).resolve(),pathlib.Path(os.environ.get('SystemRoot','/usr')).resolve()]
def _audit(event,args):
    if event=='open' and isinstance(args[0],(str,bytes,os.PathLike)):
        raw=os.fsdecode(args[0])
        if raw.upper() in ('NUL','/DEV/NULL'):return
        path=pathlib.Path(raw).resolve();flags=args[2] if len(args)>2 and isinstance(args[2],int) else 0
        mode=args[1] if len(args)>1 and isinstance(args[1],str) else ''
        writing=bool(flags&(os.O_WRONLY|os.O_RDWR|os.O_APPEND|os.O_CREAT|os.O_TRUNC)) or any(c in mode for c in 'wa+')
        roots=[_root] if writing else [_root,*_runtime]
        if not any(path==root or path.is_relative_to(root) for root in roots):raise PermissionError('runtime probe denies access outside owned fixture/runtime roots')
    if event=='subprocess.Popen' and re.fullmatch(r'(codex|claude|wrangler|ssh|curl|wget)(\.exe|\.cmd|\.bat)?',pathlib.Path(str(args[0])).name.lower()):raise PermissionError('runtime probe denies live vendor executable')
sys.addaudithook(_audit)
def _deny(*a,**k):raise PermissionError('runtime probe denies network')
socket.socket.connect=_deny;socket.socket.connect_ex=_deny
'''

PROBE = r'''import importlib.metadata as md,json,os,sys,subprocess
from pathlib import Path
home=Path(os.environ['COLLIE_RUNTIME_TEST_ROOT']);site=Path(SITE)
assert {d.metadata['Name'] for d in md.distributions()} <= {'pip','collie-harness'},'Fresh runtime inherited third-party dependencies'
from harness import cli,sessions
try:cli.main(['--help'])
except SystemExit as exc:assert exc.code in (None,0)
(home/'fixture.txt').write_text('installed runtime fixture',encoding='utf-8')
h=cli.make_harness(str(home),provider='mock',project='installed-runtime',embed='bm25',code_search=False,web_search=False,exec_code=False,delegate=False)
answer=h.run('installed-runtime','read fixture.txt')
assert not answer.error and 'installed runtime fixture' in answer.answer
sessions.save('installed-runtime',answer.messages,cwd=str(home));assert sessions.load_checked('installed-runtime')['status']=='ok'
h.memory.close();h.recorder.close()
from bench.normalized_collie_regression import load_baseline
assert len(load_baseline()['selected_results'])==8
from harness.evidence_bridge import EvidenceBridge
from harness.memory_bridge import MemoryBridge
assert EvidenceBridge and MemoryBridge
workspace=home/'workspace';workspace.mkdir();(workspace/'fixture.txt').write_text('bounded MCP fixture',encoding='utf-8')
evidence={'version':1,'state_dir':str(home/'evidence-state'),'workspaces':{'sample':{'root':str(workspace),'goal':'Read the owned fixture','scope':['fixture.txt'],'checks':{'unit':{'command':subprocess.list2cmdline([sys.executable,'-I','-c','print(1)']),'timeout_s':10}}}}}
memory={'version':1,'store_id':'runtime-probe','db_path':str(home/'memory.sqlite'),'max_results':5,'workspaces':{'sample':{'namespace':'workspace:00000000-0000-4000-8000-000000000001'}}}
stdio=[]
requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'owned-runtime-probe','version':'1'}}},
          {'jsonrpc':'2.0','method':'notifications/initialized'}, {'jsonrpc':'2.0','id':2,'method':'tools/list','params':{}}]
for module,config in [('evidence_bridge',evidence),('memory_bridge',memory)]:
    config_path=home/(module+'.json');config_path.write_text(json.dumps(config),encoding='utf-8')
    result=subprocess.run([sys.executable,'-I','-m','harness.'+module,'--config',str(config_path)],
        input=''.join(json.dumps(request)+'\n' for request in requests),text=True,capture_output=True,timeout=30)
    assert result.returncode==0,(module,result.returncode)
    responses=[json.loads(line) for line in result.stdout.splitlines()]
    assert next(row for row in responses if row.get('id')==1)['result']['serverInfo']
    tools=next(row for row in responses if row.get('id')==2)['result']['tools'];assert tools
    stdio.append({'bridge':module,'passed':True,'tool_count':len(tools)})
loaded=[]
for name,module in list(sys.modules.items()):
    if name=='harness' or name.startswith(('harness.','bench.')):
        path=getattr(module,'__file__',None)
        if path:assert Path(path).resolve().is_relative_to(site);loaded.append(name)
print(json.dumps({'passed':True,'loaded_modules':loaded,'cli_mock_read_journal':True,'real_baseline_cells':8,'stdio':stdio}))
'''


def verify(wheel_override=None, receipt_name='installed-verification.json'):
    receipt=json.loads((BUILD/'build-receipt.json').read_text());assert receipt['complete']
    wheel=Path(wheel_override or next(row['path'] for row in receipt['artifacts'] if row['path'].endswith('.whl')))
    if wheel_override:audit_artifact(wheel,receipt['source_hashes'],bool(receipt['release_client']))
    else:assert digest(wheel.read_bytes())==next(row['sha256'] for row in receipt['artifacts'] if row['path']==str(wheel))
    base=BUILD/('installed-'+uuid.uuid4().hex[:10]);base.mkdir(parents=True)
    env=isolated_environment(base/'isolation');env['COLLIE_RUNTIME_TEST_ROOT']=str(base)
    creation=execute([sys.executable,'-m','venv',str(base/'venv')],base,env,base/'create.log')
    assert creation['status']=='passed'
    python=base/'venv'/('Scripts/python.exe' if os.name=='nt' else 'bin/python')
    install=execute([str(python),'-I','-m','pip','install','--no-index','--no-deps',str(wheel)],base,env,base/'install.log');assert install['status']=='passed'
    site=Path(subprocess.check_output([str(python),'-I','-c','import sysconfig;print(sysconfig.get_path("purelib"))'],cwd=base,env=env,text=True).strip())
    (site/'sitecustomize.py').write_text(GUARD,encoding='utf-8')
    gate=execute([str(python),'-I','-c',PROBE.replace('SITE',repr(str(site)))],base,env,base/'functional.log')
    result={'status':gate['status'],'attempt_directory':str(base),'python':str(python),'wheel_sha256':digest(wheel.read_bytes()),
        'scope':'Fresh stdlib-only installation; actual CLI mock read, journal, real eight-cell public baseline and two MCP stdio initialize/tools-list round trips. Host registration and deeper behavioral fixtures are separate receipts.',
        'shared_third_party_runtime':False,'real_model_or_network_calls':False,'functional':gate,'install':install,'source_hashes':receipt['source_hashes']}
    write(base/'receipt.json',result);write(BUILD/receipt_name,result)
    return result


def verify_source_archive():
    receipt=json.loads((BUILD/'build-receipt.json').read_text());assert receipt['complete']
    row=next(row for row in receipt['artifacts'] if row['path'].endswith('.tar.gz'))
    original=Path(row['path']);assert digest(original.read_bytes())==row['sha256']
    base=BUILD/('source-archive-'+uuid.uuid4().hex[:10]);base.mkdir(parents=True)
    archive=base/original.name;shutil.copyfile(original,archive)
    env=isolated_environment(base/'isolation')
    # Building the source archive requires only explicitly available build tools;
    # neither Git nor a source checkout is on the subprocess path.
    env['PATH']=os.pathsep.join([str(Path(sys.executable).parent),str(Path(os.environ.get('SystemRoot','/usr'))/'System32')])
    env.update(GIT_CONFIG_NOSYSTEM='1',GIT_CONFIG_GLOBAL=os.devnull,GIT_TERMINAL_PROMPT='0')
    built=execute([sys.executable,'-m','pip','wheel','--no-index','--no-deps','--no-build-isolation',
                   '--wheel-dir',str(base/'wheels'),str(archive)],base,env,base/'source-wheel.log')
    if built['status']!='passed':raise ValueError('Source archive build failed: '+built['log'])
    wheels=list((base/'wheels').glob('*.whl'));assert len(wheels)==1
    audited=audit_artifact(wheels[0],receipt['source_hashes'],bool(receipt['release_client']))
    installed=verify(wheels[0],'source-installed-verification.json')
    result={'status':installed['status'],'source_archive_sha256':row['sha256'],'archive_build':built,
        'rebuilt_wheel':audited,'installed_probe':installed,'no_git_on_build_path':True,
        'build_dependencies':'Explicit existing local build backend only; application probe is a second clean venv without shared third-party libraries.'}
    write(base/'receipt.json',result);write(BUILD/'source-archive-verification.json',result)
    return result


def install(extras='', include_client=False, upgrade=False):
    selected=[item for item in extras.split(',') if item]
    if any(not re.fullmatch(r'[a-z][a-z0-9-]*',item) for item in selected):raise ValueError('Invalid extra name')
    receipt=build(include_client)
    wheel=Path(next(row['path'] for row in receipt['artifacts'] if row['path'].endswith('.whl')))
    requirement='collie-harness'+('['+','.join(selected)+']' if selected else '')+' @ '+wheel.resolve().as_uri()
    command=[sys.executable,'-m','pip','install','--no-build-isolation','--no-warn-script-location']
    if upgrade:command.append('--upgrade')
    command.append(requirement)
    result=execute(command,ROOT,os.environ.copy(),Path(receipt['attempt_directory'])/'install-runtime.log',timeout=1800)
    write(BUILD/'installation-receipt.json',{'status':result['status'],'extras':selected,'wheel_sha256':digest(wheel.read_bytes()),'install':result})
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    builder=sub.add_parser('build');builder.add_argument('--include-generated-google-client',action='store_true')
    installer=sub.add_parser('install');installer.add_argument('--extras',default='');installer.add_argument('--upgrade',action='store_true');installer.add_argument('--include-generated-google-client',action='store_true')
    sub.add_parser('verify');sub.add_parser('verify-source');sub.add_parser('inventory')
    options=parser.parse_args(argv)
    if options.command=='build':result=build(options.include_generated_google_client)
    elif options.command=='install':result=install(options.extras,options.include_generated_google_client,options.upgrade)
    elif options.command=='verify':result=verify()
    elif options.command=='verify-source':result=verify_source_archive()
    else:result={'version':version(),'sources':source_inventory()}
    print(json.dumps({k:v for k,v in result.items() if k not in ('source_hashes','sources')},ensure_ascii=False))
    return 1 if result.get('status') in ('failed','timeout') else 0


if __name__=='__main__':raise SystemExit(main())
