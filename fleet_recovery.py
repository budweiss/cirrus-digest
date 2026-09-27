"""Bounded cancellation of controller-owned subprocesses, never shared services.

Handles are kept by the launching controller. After controller restart, remote
or inherited processes require reconciliation; a bare persisted PID is not
sufficient authority. HTTP cancellation remains backend-specific and separate.
"""
import json
import os
import signal
import subprocess
import time


def cancel_child(process, grace=30, allow_force=False):
    if not isinstance(process,subprocess.Popen) or process.poll() is not None:
        raise ValueError('requires a live child handle owned by this controller')
    # Popen retains child identity until reaped. Only a dedicated session/group
    # may be signalled; never accept a caller-supplied PID or service name.
    if os.getpgid(process.pid)!=process.pid or os.getsid(process.pid)!=process.pid:
        raise ValueError('child must own an isolated process session')
    if not 0<grace<=30:raise ValueError('invalid grace window')
    os.killpg(process.pid,signal.SIGTERM)
    forced=False
    try:process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        if not allow_force:
            return {'stopped':False,'forced':False,'reason':'force not permitted; reservation retained'}
        os.killpg(process.pid,signal.SIGKILL);forced=True
        process.wait(timeout=5)
    # Children may outlive their parent. Verify the owned group is gone before
    # allowing the controller to release any reservation.
    try:os.killpg(process.pid,0)
    except ProcessLookupError:return {'stopped':True,'forced':forced,'returncode':process.returncode}
    return {'stopped':False,'forced':forced,'reason':'owned group still exists; reconcile'}


def checkpoint(path,project,input_hash):
    d=json.loads(path.read_text())
    if d.get('version')!=1 or d.get('project')!=project or d.get('input_hash')!=input_hash:
        raise ValueError('checkpoint identity/version mismatch')
    if not isinstance(d.get('completed_sections'),list) or any(type(i)!=int or i<0 for i in d['completed_sections']):
        raise ValueError('invalid checkpoint progress')
    return d
