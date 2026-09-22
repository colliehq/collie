import concurrent.futures, json, os, subprocess, sys, time
from pathlib import Path
from native_jobs import environment, DEADLINE

ROOT=Path(__file__).resolve().parent
source=Path(r'C:\workspace\collie-evolution-2026-09-07\coding_benchmark.py').read_text(encoding='utf-8')
source=source.replace('ROOT=Path(__file__).resolve().parent',"ROOT=Path(os.environ['COLLIE_BENCH_RUN_ROOT'])")
script=ROOT/'workflow_attempt.py';script.write_text(source,encoding='utf-8')

def attempt(task,worker,rep):
    parent=ROOT/'workflows'/f'repetition-{rep}';parent.mkdir(parents=True,exist_ok=True)
    log=parent/f'{task}-{worker}.log';env=environment();env['COLLIE_BENCH_RUN_ROOT']=str(parent)
    with log.open('x',encoding='utf-8') as output:
        proc=subprocess.Popen([sys.executable,str(script),task,worker],env=env,cwd=parent,
            stdout=output,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({'start':'workflow','task':task,'worker':worker,'rep':rep,'pid':proc.pid}),flush=True)
        try:code=proc.wait(timeout=min(1100,max(1,DEADLINE-time.time()-15)))
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True);proc.wait();code='timeout'
    resultfile=parent/f'coding-{task}-{worker}'/'result.json'
    result=json.loads(resultfile.read_text(encoding='utf-8')) if resultfile.exists() else {}
    row={'task':task,'worker':worker,'repetition':rep,'exit':code,'passed':result.get('passed'),
         'elapsed':result.get('elapsed'),'followup_ok':result.get('followup_ok'),
         'errors':result.get('errors'),'mobile_overflow':result.get('mobile_overflow'),
         'phases':[{'phase':p['phase'],'passed':p['passed'],'independent_checks':p.get('independent_checks'),
                    'verified':p['receipt'].get('verified')} for p in result.get('phases',[])]}
    print(json.dumps(row),flush=True);return row

if __name__=='__main__':
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futures=[pool.submit(attempt,t,w,r) for r in range(2) for t in ('intervals','config') for w in ('collie','claude-code')]
        rows=[]
        for f in concurrent.futures.as_completed(futures):
            try:rows.append(f.result())
            except Exception as e:rows.append({'orchestrator_error':type(e).__name__})
            (ROOT/'workflow-results.json').write_text(json.dumps(rows,indent=2),encoding='utf-8')
