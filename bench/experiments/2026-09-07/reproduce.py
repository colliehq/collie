"""Replay a frozen experiment in a fresh external directory (Windows host)."""
import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
COMMIT = '8577c10a33370bf84ae9cf953db64354be92a30d'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--track', choices=('native', 'normalized', 'web'), required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--collie-repo', type=Path, required=True)
    parser.add_argument('--deadline-utc', required=True, help='Explicit future ISO timestamp, at most 3 hours away')
    parser.add_argument('--claude-cli', type=Path)
    parser.add_argument('--task-ids', help='Comma-separated exact IDs; default: four complex native tasks or all six normalized tasks')
    parser.add_argument('--coverage-diagnostic', action='store_true')
    parser.add_argument('--collie-shell-ablation', action='store_true')
    parser.add_argument('--repetitions', type=int, default=1)
    parser.add_argument('--concurrency', type=int, default=4)
    parser.add_argument('--prepare-only', action='store_true', help='Write resolved commands and scripts without launching models')
    args = parser.parse_args()
    if os.name != 'nt':
        parser.error('The frozen native/process controller requires a Windows host.')
    repo = args.collie_repo.resolve()
    output = args.output.resolve()
    if output.exists() or output == repo or output.is_relative_to(repo) or output.is_relative_to(ROOT):
        parser.error('Output must be a new directory outside both the Collie checkout and this bundle.')
    head = subprocess.check_output(['git', '-C', str(repo), 'rev-parse', 'HEAD'], text=True).strip()
    dirty = subprocess.check_output(['git', '-C', str(repo), 'status', '--porcelain'], text=True)
    if head != COMMIT or dirty.strip():
        parser.error('Use a clean detached checkout at '+COMMIT)
    deadline = dt.datetime.fromisoformat(args.deadline_utc.replace('Z', '+00:00'))
    if deadline.tzinfo is None:
        parser.error('Deadline must include a timezone.')
    remaining = (deadline-dt.datetime.now(dt.timezone.utc)).total_seconds()
    if not 0 < remaining <= 10800:
        parser.error('Deadline must be in the next three hours.')
    if args.concurrency < 1 or args.repetitions < 1:
        parser.error('Concurrency and repetitions must be positive.')
    if args.collie_shell_ablation and args.track != 'normalized':
        parser.error('Shell ablation is normalized-only.')
    if args.track == 'web' and (args.task_ids or args.coverage_diagnostic or args.repetitions != 1 or args.concurrency != 4):
        parser.error('The frozen web controller always runs two tasks x two workers x two repetitions at concurrency 4.')
    cli = args.claude_cli or Path(os.environ['APPDATA'])/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'
    if not cli.is_file():
        parser.error('Native Claude executable is missing; provide --claude-cli.')
    output.mkdir(parents=True)
    changes = []

    for source in sorted((ROOT/'snapshot').glob('*.py')):
        before = source.read_text(encoding='utf-8')
        after = before
        if source.name in ('native_batch.py', 'native_attempt.py'):
            after = after.replace("r'C:\\workspace\\collie'", repr(str(repo)))
        if source.name == 'native_jobs.py':
            after = after.replace("CLI=Path(os.environ['APPDATA'])/'npm/node_modules/@anthropic-ai/claude-code/bin/claude.exe'",
                                  'CLI=Path('+repr(str(cli.resolve()))+')')
            after = after.replace('DEADLINE=datetime.datetime(2026,9,7,20,0,tzinfo=datetime.timezone.utc).timestamp()',
                                  'DEADLINE='+repr(deadline.timestamp()))
        if source.name == 'native_attempt.py':
            after = after.replace('1788811200-15', repr(deadline.timestamp())+'-15')
        if source.name == 'native_batch.py':
            after = after.replace('2026-09-07T20:00:00Z', deadline.isoformat())
        if source.name == 'workflow_attempt.py':
            after = after.replace("'C:/workspace/collie'", repr(str(repo)))
        if source.name == 'workflow_batch.py':
            after = after.replace("Path(r'C:\\workspace\\collie-evolution-2026-09-07\\coding_benchmark.py')",
                                  "(ROOT/'workflow_attempt.py')")
        compile(after, source.name, 'exec')
        target = output/source.name
        target.write_text(after, encoding='utf-8')
        changes.append({'file':source.name,'source_sha256':hashlib.sha256(source.read_bytes()).hexdigest(),
                        'replay_sha256':hashlib.sha256(target.read_bytes()).hexdigest(),
                        'changed':before != after})

    filename = 'tasks-coverage.json' if args.coverage_diagnostic else 'tasks.json'
    tasks = json.loads((ROOT/filename).read_text(encoding='utf-8'))
    if args.task_ids:
        wanted = set(args.task_ids.split(','))
        if not wanted <= {t['task_id'] for t in tasks}:
            parser.error('Unknown task ID.')
        tasks = [t for t in tasks if t['task_id'] in wanted]
    elif args.track == 'native' and not args.coverage_diagnostic:
        tasks = [t for t in tasks if not t['task_id'].startswith('local-')]
    taskfile = output/'tasks.json'
    taskfile.write_text(json.dumps(tasks, ensure_ascii=False, indent=2), encoding='utf-8')
    if args.track == 'native':
        command = [sys.executable, str(output/'native_batch.py'), '--name', 'native-replay',
                   '--tasks', str(taskfile), '--repetitions', str(args.repetitions),
                   '--concurrency', str(args.concurrency)]
    elif args.track == 'normalized':
        shutil.copytree(ROOT/'adapter-source', output/'adapter-source')
        command = [sys.executable, str(output/'run_normalized.py'), '--root', str(output/'normalized-replay'),
                   '--tasks', str(taskfile), '--collie-repo', str(repo),
                   '--bench-repo', str(output/'adapter-source'), '--deadline-utc', deadline.isoformat(),
                   '--repetitions', str(args.repetitions), '--concurrency', str(args.concurrency)]
        if args.collie_shell_ablation:
            command.append('--collie-shell-ablation')
    else:
        command = [sys.executable, str(output/'workflow_batch.py')]
    (output/'replay-plan.json').write_text(json.dumps({
        'source_commit':head,'track':args.track,'command':command,'path_and_deadline_changes':changes,
        'methodology':'Frozen evaluator with explicit path/deadline substitution; new samples are not historical results.',
        'web_controller_fixed_design':args.track == 'web'}, indent=2), encoding='utf-8')
    print(json.dumps({'prepared':str(output),'model_launch_requested':not args.prepare_only}))
    if not args.prepare_only:
        return subprocess.call(command, cwd=output)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
