"""Apply the tested native sign-in option without moving runner credentials."""
import json
import os
from pathlib import Path
import shlex
import signal
import subprocess
import sys
import time
import urllib.request

repo=Path.cwd()
state=Path(sys.argv[1])
endpoint=json.loads((repo/'panel-endpoint.json').read_text())
record=json.loads((state/'panel.json').read_text())
port,pid=record['port'],record['pid']
headers={'Authorization':'Bearer '+endpoint['access_token']}
with urllib.request.urlopen(urllib.request.Request('http://127.0.0.1:'+str(port)+'/api/state',headers=headers),timeout=20) as response:
    info=json.load(response)
command=subprocess.check_output(['ps','-p',str(pid),'-o','command='],text=True).strip()
if '-m whoisauto serve' not in command and '/tmp/whoisauto-auth-observer.py serve' not in command:
    raise SystemExit('Unexpected panel process; update stopped')
executable=shlex.split(command)[0]
if not Path(executable).is_file(): raise SystemExit('Panel interpreter not found')
subprocess.run(['git','fetch','origin','main'],check=True,capture_output=True,text=True)
targets=['whoisauto/roblox.py','whoisauto/player.py','whoisauto/web.py','whoisauto/native/open_roblox.swift',
         'whoisauto/static/app.js','whoisauto/static/index.html']
for target in targets:
    current=(repo/target).read_bytes()
    expected=[]
    for ref in ('HEAD','30ebf97','FETCH_HEAD'):
        result=subprocess.run(['git','show',ref+':'+target],capture_output=True)
        if result.returncode == 0: expected.append(result.stdout)
    if current not in expected: raise SystemExit('Runner has custom source changes; update stopped')
source=Path('/tmp/whoisauto-open-roblox-next.swift')
source.write_bytes(subprocess.check_output(['git','show','FETCH_HEAD:whoisauto/native/open_roblox.swift']))
staged=Path('/tmp/whoisauto-open-roblox-next')
compiled=subprocess.run(['xcrun','swiftc',str(source),'-O','-o',str(staged)],capture_output=True,text=True)
if compiled.returncode: raise SystemExit('Native launcher compilation failed; update stopped')
subprocess.run(['git','checkout','FETCH_HEAD','--',*targets],check=True,capture_output=True,text=True)
binary=state/'opom'/'whoisauto-open-roblox'
os.replace(staged,binary)
os.utime(binary,None)
environment={**os.environ,'PYTHONPATH':str(repo),'WHOISAUTO_ACCESS_TOKEN':endpoint['access_token'],
    'OPOM_ROOT':info['paths']['root'],'OPOM_ROBLOX_APP':info['install']['roblox']['path'],
    'OPOM_EDITOR_APP':info['install']['editor']['path'],'OPOM_WORKSPACE':info['paths']['root']+'/workspace',
    'WHOISAUTO_RUNTIME':str(state.parent),'WHOISAUTO_INSTALLERS':info['paths']['installers']}
os.kill(pid,signal.SIGTERM)
for _ in range(50):
    try: os.kill(pid,0)
    except ProcessLookupError: break
    time.sleep(0.1)
with Path('/tmp/panel.log').open('w') as log:
    child=subprocess.Popen([executable,'-u','-m','whoisauto','serve','--port',str(port),'--runtime',str(state.parent),'--no-browser'],
        cwd=repo,env=environment,stdin=subprocess.DEVNULL,stdout=log,stderr=log,start_new_session=True)
for _ in range(60):
    try:
        value=json.loads((state/'panel.json').read_text())
        if value.get('pid') == child.pid and value.get('port') == port: break
    except (OSError,ValueError): pass
    if child.poll() is not None: raise SystemExit('Updated panel failed to start')
    time.sleep(0.5)
else: raise SystemExit('Updated panel did not start in time')
print('PANEL_UPDATED_WITH_NATIVE_SIGN_IN',flush=True)
request=urllib.request.Request('http://127.0.0.1:'+str(port)+'/api/roblox/login',data=b'{}',headers=headers,method='POST')
with urllib.request.urlopen(request,timeout=90) as response:
    value=json.load(response)
    print(json.dumps({'native_login_status':response.status,'manual_sign_in':value.get('manual_sign_in'),
                      'player_pid':value.get('instance',{}).get('pid'),'execution_port':value.get('instance',{}).get('port')}),flush=True)
