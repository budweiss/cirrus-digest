"""Install/inspect/revert only the three authorized media schedule adapters.

Run with the platform Python as root for --install/--rollback. Never starts a
job, changes recipients, or creates a second timer. Backups are retained.
"""
import json
import os
import plistlib
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def command(args, check=True):
    return subprocess.run(args,check=check,capture_output=True,text=True)


def mac(mode):
    app=Path('/Users/buddy/projects/cirrus-digest')
    if not (app/'media_jobs.py').exists():
        raise RuntimeError('production media adapter missing')
    for job in ('ytwatch','digest'):
        path=Path('/Library/LaunchDaemons/com.cirrus.'+job+'.plist')
        target='system/com.cirrus.'+job
        backup=path.with_suffix('.plist.pre-media-s298')
        current=plistlib.loads(path.read_bytes())
        if mode=='--check':
            print(json.dumps({'job':job,'program':current['ProgramArguments'],
                'media_enabled':current.get('EnvironmentVariables',{}).get('CUMULUS_MEDIA'),
                'schedule':current.get('StartCalendarInterval')}))
            continue
        status=command(['launchctl','print',target],False)
        if 'state = running' in status.stdout:
            raise RuntimeError(job+' currently running; retry cutover after completion')
        if mode=='--rollback':
            if not backup.exists():
                raise RuntimeError('rollback backup missing')
            data=backup.read_bytes()
        else:
            if not backup.exists():
                shutil.copy2(path,backup)
            current['RunAtLoad']=False
            current.setdefault('EnvironmentVariables',{})['CUMULUS_MEDIA']='1'
            if job=='ytwatch':
                current['ProgramArguments']=['/usr/bin/python3',str(app/'media_jobs.py'),'youtube']
            data=plistlib.dumps(current)
        command(['launchctl','bootout',target],False)
        temp=path.with_suffix('.plist.media-tmp');temp.write_bytes(data);temp.chmod(0o644);temp.replace(path)
        command(['launchctl','bootstrap','system',str(path)])
        print(job+': '+mode+' complete; schedule retained, job not started')


def linux(mode):
    if socket.gethostname()!='cumulus1':
        raise RuntimeError('CUMULUS1 only')
    unit='cirrus-pedagogy.service'
    path=Path('/etc/systemd/system/cirrus-pedagogy.service.d/95-media.conf')
    if mode=='--check':
        print(command(['systemctl','show',unit,'-p','ExecStart','-p','TimeoutStartUSec']).stdout)
        return
    status=command(['systemctl','show',unit,'-p','ActiveState','--value']).stdout.strip()
    if status in ('active','activating'):
        raise RuntimeError('Pedagogy running; retry cutover after completion')
    if mode=='--rollback':
        path.unlink(missing_ok=True)
    else:
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text('[Service]\nExecStart=\nExecStart=/home/buddy/cirrus-digest/.venv/bin/python '
            '/home/buddy/cirrus-digest/media_jobs.py pedagogy\nEnvironment=CUMULUS_MEDIA=1\nTimeoutStartSec=5400\n')
    command(['systemctl','daemon-reload'])
    print('Pedagogy: '+mode+' complete; existing timer retained, job not started')


if __name__=='__main__':
    if len(sys.argv)!=2 or sys.argv[1] not in ('--check','--install','--rollback'):
        raise SystemExit('expected --check, --install or --rollback')
    mode=sys.argv[1]
    if mode!='--check' and os.geteuid()!=0:
        raise SystemExit('installation requires root for these fixed service files')
    (mac if sys.platform=='darwin' else linux)(mode)
