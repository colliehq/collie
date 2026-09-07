"""Two-stage code tasks through the actual UI, then a same-session read-only followup."""
import copy, importlib.util, json, os, random, subprocess, sys, threading, time
from pathlib import Path
from http.server import ThreadingHTTPServer

ROOT=Path(os.environ['COLLIE_BENCH_RUN_ROOT'])
task,worker=sys.argv[1:3]
run=ROOT/('coding-'+task+'-'+worker);state,project=run/'state',run/'project'
state.mkdir(parents=True,exist_ok=True);project.mkdir(exist_ok=True)
for key in list(os.environ):
    if key.startswith(('ANTHROPIC_','CLAUDE_CODE_USE_')) or key in ('CLAUDECODE','CLAUDE_CODE_OAUTH_TOKEN'):
        os.environ.pop(key,None)
os.environ.update(COLLIE_STATE_DIR=str(state),COLLIE_DATA_DIR=str(state/'data'),
    COLLIE_SETTINGS_PATH=str(state/'settings.json'),COLLIE_SESSIONS_DIR=str(state/'sessions'),
    COLLIE_MCP_CONFIG=str(state/'mcp.json'),COLLIE_EMBED='bm25',COLLIE_LANG='en',
    COLLIE_PROVIDER='claude-agent-sdk',COLLIE_MODEL='claude-opus-5',
    COLLIE_RUNNER=worker,COLLIE_RUNNER_POOL=worker,COLLIE_BROWSER_BRIDGE='0',COLLIE_REMOTE='off')
(state/'settings.json').write_text(json.dumps({'PROVIDER':'claude-agent-sdk','MODEL':'claude-opus-5',
    'RUNNER':worker,'RUNNER_POOL':worker,'LANG':'en','EMBED':'bm25'}),encoding='utf-8')
(state/'mcp.json').write_text('{"mcpServers":{}}',encoding='utf-8')
if task=='intervals':
    code='def merge_intervals(intervals):\n    return list(intervals)\n'
    visible='from solution import merge_intervals\nimport unittest\nclass Tests(unittest.TestCase):\n def test_overlap(self): self.assertEqual(merge_intervals([(3,5),(1,4)]),[(1,5)])\n def test_empty(self): self.assertEqual(merge_intervals([]),[])\n'
    primary='Implement solution.merge_intervals(intervals): input is an iterable of finite numeric (start,end) pairs; reject booleans, non-numeric endpoints, non-finite endpoints, reversed intervals, and pairs of the wrong length with ValueError. Return a new sorted list of merged tuples, merging overlap and touching endpoints; drop zero-length intervals. Preserve inputs and support generators. Use Python standard library only, add useful tests and document behavior in README.md.'
    followup='Extend merge_intervals with keyword-only merge_touching=True, preserving the default. When False merge only strict overlaps; touching intervals remain separate. Validate this flag as a real bool and reject other types with ValueError. Preserve all previous validation, zero-length behavior, generator support, and input immutability. Update tests and README.md.'
else:
    code='def merge_config(base, override):\n    return dict(base, **override)\n'
    visible='from solution import merge_config\nimport unittest\nclass Tests(unittest.TestCase):\n def test_nested(self): self.assertEqual(merge_config({"a":{"x":1}},{"a":{"y":2}}),{"a":{"x":1,"y":2}})\n def test_scalar(self): self.assertEqual(merge_config({"a":1},{"a":2}),{"a":2})\n'
    primary='Implement solution.merge_config(base, override): both top-level inputs must be dicts, otherwise TypeError. Recursively merge values only when both are dicts; otherwise replace with a deep copy of the override value. Lists are replaced, not concatenated. The returned structure must share no mutable containers with either input, including lists of dicts. Preserve key order (base keys first, new override keys afterward). None is a normal replacement value. Use standard library only, add meaningful tests and document README.md.'
    followup='Add keyword-only delete_none=False to merge_config, preserving old default behavior. Validate it is a bool, otherwise TypeError. With True, a None override value deletes that key at every dictionary depth, including nested dictionaries replacing scalar/missing base values; do not interpret None elements inside lists as deletions. Preserve input immutability, deep-copy isolation, key order and list replacement. Update tests and README.md.'
(project/'solution.py').write_text(code,encoding='utf-8')
(project/'test_solution.py').write_text(visible,encoding='utf-8')
(project/'README.md').write_text('Small local utility.\n',encoding='utf-8')
sys.path.insert(0,'C:/workspace/collie');os.chdir(project)
from harness import sessions,session_owner,webapp
from playwright.sync_api import sync_playwright,expect

def acceptance(phase):
    spec=importlib.util.spec_from_file_location('bench_solution_'+str(time.time_ns()),project/'solution.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    checks=0
    if task=='intervals':
        f=module.merge_intervals; rng=random.Random(937)
        for touching in ([True] if phase==1 else [True,False]):
            for _ in range(100):
                rows=[tuple(sorted([rng.randint(-20,20),rng.randint(-20,20)])) for _ in range(12)]
                original=copy.deepcopy(rows); expected=[]
                for start,end in sorted(rows):
                    if start==end:continue
                    if expected and (start<=expected[-1][1] if touching else start<expected[-1][1]):
                        expected[-1]=(expected[-1][0],max(expected[-1][1],end))
                    else:expected.append((start,end))
                args={} if phase==1 else {'merge_touching':touching}
                assert f(iter(rows),**args)==expected and rows==original
                checks+=1
        for rows in [[(True,3)],[(1,float('nan'))],[(1,float('inf'))],[(3,1)],[(1,2,3)],[('a',2)]]:
            try:f(rows)
            except ValueError:checks+=1
            else:raise AssertionError('invalid interval accepted: '+repr(rows))
        if phase==2:
            try:f([],merge_touching=1)
            except ValueError:checks+=1
            else:raise AssertionError('non-bool flag accepted')
    else:
        f=module.merge_config
        base={'a':{'x':1,'drop':2},'b':[{'v':[1]}],'c':8}
        override={'a':{'y':3,'drop':None},'b':[{'v':[2]}],'new':{'p':None,'q':4}}
        before=(copy.deepcopy(base),copy.deepcopy(override))
        expected={'a':{'x':1,'drop':None,'y':3},'b':[{'v':[2]}],'c':8,'new':{'p':None,'q':4}}
        out=f(base,override);assert out==expected and list(out)==['a','b','c','new'];checks+=2
        out['b'][0]['v'].append(99);out['a']['x']=12;assert (base,override)==before;checks+=1
        retained=f(base,{});retained['b'][0]['v'].append(55);assert (base,override)==before;checks+=1
        for x,y in [([],{}),({},None),(1,{})]:
            try:f(x,y)
            except TypeError:checks+=1
            else:raise AssertionError('invalid top-level input accepted')
        if phase==2:
            out=f(base,override,delete_none=True)
            assert out=={'a':{'x':1,'y':3},'b':[{'v':[2]}],'c':8,'new':{'q':4}};checks+=1
            assert f({'a':1},{'a':{'b':None,'c':{'d':None,'e':2}}},delete_none=True)=={'a':{'c':{'e':2}}};checks+=1
            assert f({}, {'list':[None,{'v':None}]},delete_none=True)=={'list':[None,{'v':None}]};checks+=1
            assert (base,override)==before;checks+=1
            try:f({}, {},delete_none='yes')
            except TypeError:checks+=1
            else:raise AssertionError('non-bool flag accepted')
    return checks

server=ThreadingHTTPServer(('127.0.0.1',0),webapp.Handler)
threading.Thread(target=server.serve_forever,daemon=True).start()
result={'task':task,'worker':worker,'phases':[],'errors':[]};start=time.monotonic()
def wait(page,predicate,timeout=420):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if predicate():return
        page.wait_for_timeout(150)
    raise AssertionError('scenario timed out')
try:
 with sync_playwright() as pw:
    browser=pw.chromium.launch();page=browser.new_page(viewport={'width':1380,'height':960})
    page.on('pageerror',lambda e:result['errors'].append(str(e)))
    page.goto('http://127.0.0.1:%d'%server.server_port,wait_until='domcontentloaded')
    page.locator('#newChat').click()
    page.locator('#modeTrigger').click();page.locator('[data-axis="verification"][data-val="required"]').click()
    page.locator('#verifyCommand').fill('python -m unittest -q');page.locator('#modeTrigger').click()
    sid=''
    for phase,prompt in enumerate([primary,followup],1):
        prompt+=' Work only in this project. An optional cloud style guide is unavailable and has no connected account; omit it without asking. Choose reasonable defaults. Use file tools; Collie runs the host verification command.'
        began=time.monotonic();page.locator('#input').fill(prompt);page.locator('#input').press('Enter')
        def current_sid():
            with webapp.Handler._runs_lock:
                rows=list(webapp.Handler._runs)
            return rows[-1] if rows else ''
        wait(page,current_sid);sid=current_sid()
        wait(page,lambda:len((sessions.load(sid) or {}).get('run_receipts',[]))>=phase and not session_owner.probe_busy(sid))
        journal=sessions.load(sid);receipt=journal['run_receipts'][-1]
        row={'phase':phase,'elapsed':round(time.monotonic()-began,3),'receipt':receipt,'answer':journal.get('last_answer','')}
        try:row['independent_checks']=acceptance(phase);row['passed']=True
        except Exception as exc:row['passed']=False;row['failure']=repr(exc)
        result['phases'].append(row)
        print(json.dumps({'task':task,'worker':worker,**{k:v for k,v in row.items() if k not in ('receipt','answer')}},ensure_ascii=False),flush=True)
        page.reload(wait_until='domcontentloaded')
        expect(page.locator('#input')).to_be_visible()
        if not row['passed']:break
    if len(result['phases'])==2 and all(x['passed'] for x in result['phases']):
        page.locator('#modeTrigger').click();page.locator('[data-axis="verification"][data-val="auto"]').click();page.locator('#modeTrigger').click()
        page.locator('#input').fill('Do not edit files or use tools. Reply in one short sentence confirming the optional guide was omitted, then the exact marker BENCH_FOLLOWUP_OK.')
        page.locator('#input').press('Enter')
        wait(page,lambda:len((sessions.load(sid) or {}).get('run_receipts',[]))>=3 and not session_owner.probe_busy(sid))
        journal=sessions.load(sid);result['followup_ok']='BENCH_FOLLOWUP_OK' in journal.get('last_answer','')
        result['native_sessions']=[(r.get('runner') or {}).get('native_session',{}).get('locator') for r in journal['run_receipts']]
    result['session']=sid
    page.reload(wait_until='domcontentloaded');page.wait_for_timeout(600)
    page.screenshot(path=str(run/'completed-desktop.png'),full_page=True)
    page.set_viewport_size({'width':390,'height':844});page.screenshot(path=str(run/'completed-mobile.png'),full_page=True)
    result['mobile_overflow']=page.evaluate('document.documentElement.scrollWidth>document.documentElement.clientWidth+1')
    result['passed']=len(result['phases'])==2 and all(x['passed'] for x in result['phases']) and result.get('followup_ok') and not result['errors'] and not result['mobile_overflow']
    browser.close()
except Exception as exc:
    result['failure']=repr(exc);result['passed']=False
finally:
    result['elapsed']=round(time.monotonic()-start,3)
    (run/'result.json').write_text(json.dumps(result,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
    print(json.dumps({'task':task,'worker':worker,'passed':result.get('passed'),'elapsed':result['elapsed'],'failure':result.get('failure')}),flush=True)
    server.shutdown();server.server_close()
