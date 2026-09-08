import concurrent.futures, datetime, json, os, subprocess, sys, time
from pathlib import Path

ROOT=Path(__file__).resolve().parent
CLI=Path('C:\\Users\\Sining Xu\\AppData\\Local\\Python\\pythoncore-3.14-64\\Lib\\site-packages\\claude_agent_sdk\\_bundled\\claude.exe')
DEADLINE=1788851979.088095

def environment():
    env=dict(os.environ)
    for key in list(env):
        if key.startswith(('ANTHROPIC_', 'CLAUDE_CODE_USE_')) or key in ('CLAUDECODE','CLAUDE_CODE_OAUTH_TOKEN'):
            env.pop(key,None)
    env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8')
    return env

def run(job):
    folder=ROOT/'design'/job['name'];folder.mkdir(parents=True,exist_ok=True)
    prompt=job['prompt'];(folder/'prompt.txt').write_text(prompt,encoding='utf-8')
    allowed='Read,Glob,Grep,Write,Edit' if job['kind']=='author' else 'Read,Glob,Grep'
    args=[str(CLI),'-p','--model','claude-opus-5','--effort','high','--output-format','stream-json',
          '--verbose','--permission-mode','acceptEdits','--tools',allowed,'--allowedTools',allowed,
          '--strict-mcp-config','--mcp-config','{"mcpServers":{}}','--setting-sources','',
          '--no-session-persistence','--no-chrome']
    began=time.monotonic()
    with (folder/'trace.jsonl').open('x',encoding='utf-8') as out,(folder/'stderr.txt').open('x',encoding='utf-8') as err:
        proc=subprocess.Popen(args,cwd=folder,env=environment(),stdin=subprocess.PIPE,stdout=out,stderr=err,
                              text=True,encoding='utf-8',creationflags=subprocess.CREATE_NO_WINDOW)
        print(json.dumps({'event':'start','job':job['name'],'pid':proc.pid}),flush=True)
        proc.stdin.write(prompt);proc.stdin.close()
        try:code=proc.wait(timeout=min(1200,max(1,DEADLINE-time.time()-60)))
        except subprocess.TimeoutExpired:
            subprocess.run(['taskkill','/PID',str(proc.pid),'/T','/F'],capture_output=True)
            proc.wait();code='timeout'
    result={'job':job['name'],'exit':code,'seconds':round(time.monotonic()-began,3)}
    final={}
    for line in (folder/'trace.jsonl').read_text(encoding='utf-8').splitlines():
        try:event=json.loads(line)
        except ValueError:continue
        if event.get('type')=='result':final=event
    result.update(is_error=final.get('is_error'),usage=final.get('usage'),api_equivalent_usd=final.get('total_cost_usd'))
    (folder/'answer.md').write_text(final.get('result',''),encoding='utf-8')
    (folder/'result.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    print(json.dumps(result),flush=True)
    return result

AUTHOR='''You are designing a difficult, deterministic coding benchmark, not solving the Collie application.
Create task.py in the current directory using file tools. It must be valid standalone Python defining TASKS, a tuple with ONE dict with exactly these keys:
task_id (unique kebab-case), prompt (complete user requirements), fixture_files (mapping relative paths to initial broken code strings), gold_files (mapping relative paths to correct reference code strings), hidden_grader (Python program string).
Use only Python standard library. Target Python 3.11+. The task must require understanding and changing 3-5 small modules, not just one trivial function. Build an authentic miniature library with 120-220 total initial source lines and subtle preexisting bugs. Ask for a specific behavior evolution and compatibility, detailed and unambiguous enough for external tests. No network, shell subprocess, real sleeping, credentials, absolute file paths or external dependencies. Tests use injected clocks and TemporaryDirectory when needed. Include public smoke tests in fixtures; at least one public test must fail baseline. The reference solution must pass public and hidden tests. Hidden grader imports library from cwd (evaluator prepends cwd to sys.path), uses unittest or assertions, exits nonzero on failure and prints a compact checks-passed line on success. Include at least 30 meaningful hidden assertions including randomized cases with a fixed seed, invalid inputs, backward compatibility, ordering, mutation isolation and the requested domain boundaries. Do not test any rule absent from prompt. Do not import gold implementation in grader. All file paths relative. No code execution tool is available: carefully reason about syntax and edge cases. Write the module, then describe the benchmark and any limitations. Task domain: '''

def jobs():
    domains={
      'journal':'durable JSONL event journal with atomic append semantics, truncated final record recovery, sequence validation and replay; injectable file errors; explicitly distinguish complete corruption from a torn last record',
      'scheduler':'dependency DAG task scheduler with priorities, retry eligibility, stable deterministic ordering, optional blocked dependencies, cancellation propagation and injected clock; no real concurrency required',
      'inbox':'durable multi-session request inbox with idempotency keys, claimed leases, expiration, acknowledgements and revision handling; a JSON state file and injected time',
      'config':'layered configuration loader with explicit deletion sentinel, environment interpolation escaping, deep-copy immutability, stable order and per-key provenance across nested dictionaries',
      'cache':'namespaced bounded LRU cache with TTL, stale reads, snapshot restore, canonical keys and injectable time; ensure meaningful expiration and capacity boundary interactions',
      'patch':'transactional directory patch application with expected content digests, binary files, add/update/delete, relative path validation and rollback on injected write errors; no git or subprocess',
    }
    result=[{'name':'author-'+name,'kind':'author','prompt':AUTHOR+domain} for name,domain in domains.items()]
    refs=Path(r'C:\workspace\collie-product-2026-09-06\references')
    for name in ('codex','opencode','pi','hermes','goose','openhands-sdk'):
        result.append({'name':'audit-'+name,'kind':'audit','prompt':f'''Read actual local source of Collie at C:/workspace/collie and the pinned {name} codebase at {str(refs/name)}. Read-only audit. Compare concrete execution-loop, tool outcomes, interruptions, session recovery, context management, queue/steering, workspace and validation behavior. We are constructing a high-concurrency Claude Opus 5 benchmark of harnesses. Produce: (1) 8-12 discriminating reproducible scenarios with initial state, exact user prompts, injection timing and external pass criteria; (2) identify model/tool/authentication differences that invalidate naive rankings; (3) at least 4 source-grounded potential Collie weaknesses with exact file/line references and an honest not-reproduced label unless code makes it certain; (4) propose a fair comparison transport for {name}, checking its source instead of inventing flags; (5) prioritize fixes by product impact. Do not call network or alter files. Do not read credential files, user sessions, benchmark graders or other runs. Ignore broad README claims when actual implementation differs. Spend effort on deep source paths rather than a superficial feature table. Report in Chinese with precise technical identifiers.'''})
    return result

if __name__=='__main__':
    plan=jobs();(ROOT/'design-plan.json').write_text(json.dumps(plan,indent=2),encoding='utf-8')
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as executor:
        all_results=list(executor.map(run,plan))
    (ROOT/'design-results.json').write_text(json.dumps(all_results,indent=2),encoding='utf-8')
