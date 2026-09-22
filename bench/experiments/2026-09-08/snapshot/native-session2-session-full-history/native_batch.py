"""Restricted native product benchmark, with host-held grading and resumable jobs."""
import argparse, concurrent.futures, datetime, hashlib, json, os, random, subprocess, sys, time
from pathlib import Path
from native_jobs import environment, CLI, DEADLINE

ROOT=Path(__file__).resolve().parent
sys.path.insert(0,'C:\\workspace\\collie-benchmark-2026-09-07-evening\\session2-transport-worktree')
from bench.subscription_rank_tasks import materialize_task, _run_hidden_grader, TASKS, task_sha256

PREFIX=('Implement the requested behavior in source files in this project. Work only inside the current project; '
        'do not inspect any parent, sibling, credential, evaluator or benchmark-runner files. '
        'Do not edit existing tests. Use only the available local file tools. The host will run the tests after you finish. '
        'An optional online style guide is unavailable and not connected: skip it without asking. '
        'Preserve required functionality and make reasonable choices where the contract is silent.\n\n')

def write_json(path,data):
    tmp=path.with_suffix(path.suffix+'.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2,default=str),encoding='utf-8');os.replace(tmp,path)

def git(work,*args):
    return subprocess.run(['git','-C',str(work),*args],capture_output=True,timeout=30)

def grade(task,work,folder,label):
    try:
        grader=folder/(label+'-grader.py')
        grader.write_text('import sys\nsys.path.insert(0, '+repr(str(work))+')\n'+task['hidden_grader'],encoding='utf-8')
        proc=subprocess.run([sys.executable,'-I',str(grader)],cwd=work,capture_output=True,
                            text=True,encoding='utf-8',errors='replace',timeout=60)
        (folder/(label+'.txt')).write_text(proc.stdout+'\n'+proc.stderr,encoding='utf-8')
        return {'exit':proc.returncode,'passed':proc.returncode==0,'timeout':False}
    except subprocess.TimeoutExpired:
        return {'exit':None,'passed':False,'timeout':True}

def preflight(tasks,suite):
    evidence=[]
    for task in tasks:
        folder=suite/'preflight'/task['task_id'];folder.mkdir(parents=True,exist_ok=True)
        outcomes=[]
        for gold in (False,True):
            work=folder/('gold' if gold else 'baseline');materialize_task(task,work,gold=gold)
            outcomes.append(grade(task,work,folder,'gold' if gold else 'baseline'))
        valid=not outcomes[0]['passed'] and outcomes[1]['passed'] and not outcomes[0]['timeout']
        evidence.append({'task':task['task_id'],'sha256':task_sha256(task),'valid':valid,'baseline':outcomes[0],'gold':outcomes[1]})
    write_json(suite/'preflight.json',evidence)
    return {r['task'] for r in evidence if r['valid']}

def attempt(task,arm,rep,suite):
    job=f"{task['task_id']}--{arm}--{rep:02d}"
    if time.time()>=DEADLINE-60 or (ROOT/'STOP').exists():
        return {'job':job,'task':task['task_id'],'arm':arm,'repetition':rep,'status':'not_started','reason':'deadline_or_quota'}
    folder=suite/'attempts'/job;folder.mkdir(parents=True,exist_ok=False)
    work=folder/'workspace';materialize_task(task,work)
    prompt=PREFIX+task['prompt'];(folder/'prompt.txt').write_text(prompt,encoding='utf-8')
    git(work,'init','--quiet');git(work,'config','user.name','Benchmark');git(work,'config','user.email','benchmark@localhost')
    git(work,'config','core.autocrlf','false');git(work,'add','-A');git(work,'commit','--quiet','-m','Frozen fixture')
    began=time.monotonic();started=datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps({'event':'start','job':job,'at':started}),flush=True)
    if arm=='claude-code':
        allowed='Read,Edit,Write,Grep,Glob'
        args=[str(CLI),'-p','--model','claude-opus-5','--effort','high','--output-format','stream-json','--verbose',
              '--permission-mode','acceptEdits','--safe-mode','--tools',allowed,'--allowedTools',allowed,
              '--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--setting-sources','',
              '--no-session-persistence','--no-chrome','--max-turns','48']
    else:
        args=[sys.executable,str(ROOT/'native_attempt.py'),str(folder)]
    error='';returncode=None
    with (folder/'trace.jsonl').open('x',encoding='utf-8') as out,(folder/'stderr.txt').open('x',encoding='utf-8') as err:
        proc=subprocess.Popen(args,cwd=work,env={**environment(), "CLAUDE_CODE_MAX_RETRIES": "0"},stdin=subprocess.PIPE,stdout=out,stderr=err,
                              text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
        write_json(folder/'process.json',{'pid':proc.pid,'started':started,'arm':arm})
        if arm=='claude-code':proc.stdin.write(prompt)
        proc.stdin.close()
        try:returncode=proc.wait(timeout=min(900,max(1,DEADLINE-time.time()-15)))
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
            proc.wait();error='wall_timeout'
    ended=datetime.datetime.now(datetime.timezone.utc).isoformat()
    worker={};usage={};reported_error='';model=None
    if arm=='claude-code':
        for line in (folder/'trace.jsonl').read_text(encoding='utf-8',errors='replace').splitlines():
            try:event=json.loads(line)
            except ValueError:continue
            if event.get('type')=='system' and event.get('subtype')=='init':model=event.get('model')
            if event.get('type')=='result':worker=event
        usage=worker.get('usage',{})
        if worker.get('is_error'):reported_error=str(worker.get('result',''))
        if not worker:error=error or 'missing_worker_result'
        (folder/'answer.md').write_text(worker.get('result',''),encoding='utf-8')
    else:
        result_path=folder/'worker-result.json'
        if result_path.exists():
            worker=json.loads(result_path.read_text(encoding='utf-8'))
            usage={k:worker[k] for k in ('input_tokens','output_tokens','cache_read','cache_creation','cost_usd','turns') if k in worker}
            reported_error=str(worker.get('error') or '')
            model='claude-opus-5'
        else:error=error or 'missing_worker_result'
    diagnostics=reported_error+'\n'+(folder/'stderr.txt').read_text(encoding='utf-8',errors='replace')[-8000:]
    if any(x in diagnostics.lower() for x in ('rate limit','rate_limit','usage limit','weekly limit','session limit','overloaded','429')):
        error='provider_capacity'
    elif reported_error:error=error or 'worker_error'
    elif returncode not in (0,None):error=error or 'worker_exit'
    git(work,'add','-A','--intent-to-add')
    patch=git(work,'diff','--binary','HEAD').stdout;(folder/'patch.diff').write_bytes(patch)
    graded=grade(task,work,folder,'hidden-grade')
    result={'job':job,'task':task['task_id'],'task_sha256':task_sha256(task),'arm':arm,'repetition':rep,
        'started':started,'ended':ended,'elapsed_seconds':round(time.monotonic()-began,3),
        'process_exit':returncode,'error':error,'grader':graded,'patch_bytes':len(patch),'patch_sha256':hashlib.sha256(patch).hexdigest(),
        'usage':usage,'model':model,'prompt_sha256':hashlib.sha256(prompt.encode()).hexdigest(),
        'api_equivalent_usd':worker.get('total_cost_usd',worker.get('cost_usd')),
        'status':'invalid_infrastructure' if error else ('resolved' if graded['passed'] else 'unresolved'),
        'claim':'restricted native product comparison; not pure harness effect; no OS filesystem sandbox'}
    write_json(folder/'result.json',result)
    print(json.dumps({'event':'end',**result}),flush=True)
    return result

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--name',required=True);parser.add_argument('--tasks')
    parser.add_argument('--repetitions',type=int,default=3);parser.add_argument('--concurrency',type=int,default=10)
    args=parser.parse_args();suite=ROOT/args.name;suite.mkdir(exist_ok=True)
    tasks=json.loads(Path(args.tasks).read_text(encoding='utf-8')) if args.tasks else list(TASKS)
    valid=preflight(tasks,suite);tasks=[t for t in tasks if t['task_id'] in valid]
    write_json(suite/'tasks.json',tasks)
    plans=[(task,arm,rep) for rep in range(args.repetitions) for task in tasks for arm in ('collie',)]
    random.Random(20260907).shuffle(plans)
    write_json(suite/'manifest.json',{'source_commit':'31d0b0c73e4f7e066915644de313686f9a28bdb0','model':'claude-opus-5','effort':'high',
        'tools':'local file read/edit/write/glob/grep; no shell/network','concurrency':args.concurrency,
        'deadline_utc':'2026-09-08T07:19:39.096465+00:00','attempt_seconds':900,'native_turn_ceiling':48,
        'claude_cli_version':'2.1.228 (Claude Code)','seed':20260907,'planned':[{'task':t['task_id'],'arm':a,'rep':r} for t,a,r in plans]})
    pending=[]
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        for task,arm,rep in plans:
            if time.time()>DEADLINE-60:break
            pending.append(pool.submit(attempt,task,arm,rep,suite))
        results=[]
        for future in concurrent.futures.as_completed(pending):
            try:results.append(future.result())
            except Exception as exc:results.append({'status':'orchestrator_error','error':type(exc).__name__+': '+str(exc)})
            write_json(suite/'results.json',results)
    print(json.dumps({'suite':args.name,'finished':len(results),'counts':{s:sum(r['status']==s for r in results) for s in ('resolved','unresolved','invalid_infrastructure','orchestrator_error')}}),flush=True)

if __name__=='__main__':main()
