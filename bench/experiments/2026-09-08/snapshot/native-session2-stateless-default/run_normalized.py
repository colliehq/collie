"""Reviewed Windows launcher around the Claude-authored evaluator snapshot."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile
from pathlib import Path
from native_jobs import ROOT, environment

SNAPSHOT=ROOT/'normalized_batch.py'
if not SNAPSHOT.exists():
    shutil.copyfile(ROOT/'design/normalized-runner/normalized_batch.py',SNAPSHOT)
spec=importlib.util.spec_from_file_location('normalized_batch',SNAPSHOT)
module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)

# Verify every recursive cleanup is inside a named experiment tree or an
# evaluator-created temporary directory. Never delete a source repository.
original_remove=shutil.rmtree
def checked_remove(path,*args,**kwargs):
    target=Path(path).resolve();base=ROOT.resolve();temp=Path(tempfile.gettempdir()).resolve()
    inside=target!=base and target.is_relative_to(base)
    owned_temp=any(p.parent==temp and p.name.startswith('normalized-batch-') for p in (target,*target.parents))
    if not (inside or owned_temp):
        raise RuntimeError('cleanup target outside benchmark directories')
    return original_remove(target,*args,**kwargs)
module.shutil.rmtree=checked_remove

# A file transport avoids Windows' command-line length limit for large graders.
original_loader=module.load_bench_task_helpers
def load_helpers(repo):
    helpers=original_loader(repo)
    original_validate=helpers._validate_task_data
    def validate_task(task):
        # New-file feature tasks are valid: validate every gold path/content with
        # the legacy checker, without materializing placeholders in the fixture.
        added=set(task['gold_files'])-set(task['fixture_files'])
        checked={**task,'fixture_files':{**task['fixture_files'],**{name:'\n' for name in added}}}
        original_validate(checked)
    helpers._validate_task_data=validate_task
    def run_grader(task,work):
        with tempfile.TemporaryDirectory(prefix='normalized-batch-selfcheck-grader-') as temp:
            path=Path(temp)/'grader.py'
            path.write_text('import sys\nsys.path.insert(0, '+repr(str(work))+')\n'+task['hidden_grader'],encoding='utf-8')
            return subprocess.run([sys.executable,'-I',str(path)],cwd=work,capture_output=True,
                                  text=True,encoding='utf-8',errors='replace',timeout=60)
    helpers._run_hidden_grader=run_grader
    return helpers
module.load_bench_task_helpers=load_helpers

original_self_check=module.task_self_check
def check_exact_grader(tasks,helpers):
    receipts=original_self_check(tasks,helpers)
    with tempfile.TemporaryDirectory(prefix='normalized-batch-selfcheck-exact-') as temp:
        for index,task in enumerate(tasks):
            for gold in (False,True):
                work=Path(temp)/str(index)/('gold' if gold else 'baseline')
                helpers.materialize_task(task,work,gold=gold)
                result=module.grade(task,work,'preflight',helpers)
                if result['resolved']!=gold:
                    raise RuntimeError('exact production grader preflight failed for '+task['task_id'])
    return receipts
module.task_self_check=check_exact_grader

# The process uses only the native subscription route with no inherited API overrides.
clean=environment();os.environ.clear();os.environ.update(clean)
if __name__=='__main__':
    argv=sys.argv[1:]
    if '--collie-shell-ablation' in argv:
        argv.remove('--collie-shell-ablation')
        module.ARMS=('collie',)
        module.COLLIE_SHELL_ABLATION=True
        module.CLAIM='exploratory_collie_shell_capability_ablation'
        module.COMPARISON_LABEL='collie_tool_availability_ablation_not_a_cross_product_ranking'
    raise SystemExit(module.main(argv))
