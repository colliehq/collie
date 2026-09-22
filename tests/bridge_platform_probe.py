import os,sys,subprocess,time,tempfile,urllib.request,signal
from pathlib import Path
with tempfile.TemporaryDirectory(prefix='collie-bridge-diag-') as state:
    env=dict(os.environ,COLLIE_STATE_DIR=state,COLLIE_BROWSER_BRIDGE_NOSPAWN='1')
    boot="import faulthandler,signal,runpy,sys; faulthandler.register(signal.SIGUSR1); sys.argv=['collie','browser-bridge','--port','8689']; runpy.run_module('harness.cli',run_name='__main__')"
    p=subprocess.Popen([sys.executable,'-c',boot],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        deadline=time.monotonic()+20
        while time.monotonic()<deadline:
            if p.poll() is not None:
                print('EARLY EXIT',p.returncode);break
            try:
                with urllib.request.urlopen('http://127.0.0.1:8689/health',timeout=1) as response:
                    print('HEALTH',response.status,response.read());break
            except Exception as exc: print(type(exc).__name__,str(exc),flush=True)
            time.sleep(.5)
    finally:
        if p.poll() is None:
            p.send_signal(signal.SIGUSR1)
            time.sleep(.3)
        print('LISTENER',subprocess.run(['lsof','-nP','-iTCP:8689'],capture_output=True,text=True).stdout)
        p.kill()
        out,err=p.communicate(timeout=10)
        print('STDOUT',out.decode('utf8',errors='backslashreplace'))
        print('STDERR',err.decode('utf8',errors='backslashreplace'))
