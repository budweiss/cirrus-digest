"""Owned-machine containment for worktree verification, never live services.

Offline tests get private home/cache/tmp and no network sockets. Explicit daily
dry-runs retain read-only live config and outbound network, with private output.
Both modes prohibit writes outside the worktree/scratch and fail closed when
containment is unavailable. This is accident containment, not a hostile-code VM.
"""
import ctypes
import ctypes.util
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import site
import subprocess
import sys
import tempfile


def _linux_guard(root, scratch, reads, offline=True):
    libc = ctypes.CDLL(None, use_errno=True)
    abi = libc.syscall(444, 0, 0, 1)
    if abi < 3:
        raise RuntimeError('Landlock ABI 3 or newer required')
    # File rights through TRUNCATE (ABI 3), including creation/removal/rename.
    all_rights = (1 << 15) - 1
    read_rights = (1 << 0) | (1 << 2) | (1 << 3)
    class Ruleset(ctypes.Structure):
        _fields_ = [('handled_access_fs', ctypes.c_uint64)]
    class Beneath(ctypes.Structure):
        _pack_ = 1
        _fields_ = [('allowed_access', ctypes.c_uint64), ('parent_fd', ctypes.c_int32)]
    attr = Ruleset(all_rights)
    fd = libc.syscall(444, ctypes.byref(attr), ctypes.sizeof(attr), 0)
    if fd < 0:
        raise OSError(ctypes.get_errno(), 'Landlock create failed')
    try:
        for path, rights in [(p, read_rights) for p in reads] + [(root, all_rights), (scratch, all_rights), ('/dev/null', 1 << 1)]:
            p = Path(path).resolve()
            if not p.exists():
                continue
            pf = os.open(str(p), os.O_PATH | os.O_CLOEXEC)
            try:
                rule = Beneath(rights, pf)
                if libc.syscall(445, fd, 1, ctypes.byref(rule), 0) != 0:
                    raise OSError(ctypes.get_errno(), 'Landlock path rule failed')
            finally:
                os.close(pf)
        if libc.prctl(38, 1, 0, 0, 0) != 0 or libc.syscall(446, fd, 0) != 0:
            raise OSError(ctypes.get_errno(), 'Landlock enforcement failed')
    finally:
        os.close(fd)
    sec = ctypes.CDLL(ctypes.util.find_library('seccomp') or 'libseccomp.so.2')
    sec.seccomp_init.argtypes = [ctypes.c_uint32]
    sec.seccomp_init.restype = ctypes.c_void_p
    sec.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    sec.seccomp_rule_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int, ctypes.c_uint]
    sec.seccomp_load.argtypes = [ctypes.c_void_p]
    sec.seccomp_release.argtypes = [ctypes.c_void_p]
    ctx = sec.seccomp_init(0x7fff0000)
    if not ctx:
        raise RuntimeError('seccomp initialization failed')
    try:
        # socketpair remains available for local Python asyncio internals.
        calls = (b'socket', b'connect', b'bind', b'listen', b'sendto', b'sendmsg') if offline else (b'bind', b'listen')
        for name in calls:
            number = sec.seccomp_syscall_resolve_name(name)
            if number < 0 or sec.seccomp_rule_add(ctx, 0x50001, number, 0) != 0:
                raise RuntimeError('seccomp network rule failed')
        if sec.seccomp_load(ctx) != 0:
            raise RuntimeError('seccomp enforcement failed')
    finally:
        sec.seccomp_release(ctx)


def command_args(args):
    return [str(a) for a in args]


def run(args, root, timeout=120, offline=True):
    """Run verification with filesystem/network restrictions; fail closed on errors."""
    root = Path(root).resolve()
    if not root.is_dir():
        raise ValueError('test worktree missing')
    if root in {(Path.home() / 'projects/cirrus-digest').resolve(),
                (Path.home() / 'cirrus-digest').resolve()}:
        raise ValueError('refusing live operational checkout as writable test root')
    with tempfile.TemporaryDirectory(prefix='cowork-isolation-') as td:
        scratch = Path(td).resolve()
        env = {k: v for k, v in os.environ.items()
               if k in ('PATH', 'LANG', 'LC_ALL', 'LC_CTYPE', 'SYSTEMROOT')}
        for key, sub in [('HOME', 'home'), ('TMPDIR', 'tmp'), ('TMP', 'tmp'),
                         ('TEMP', 'tmp'), ('XDG_CACHE_HOME', 'cache'),
                         ('XDG_CONFIG_HOME', 'config'), ('XDG_DATA_HOME', 'data')]:
            folder = scratch / sub
            folder.mkdir(exist_ok=True)
            env[key] = str(folder)
        # Keep installed dependencies available without using the real HOME.
        packages = site.getusersitepackages()
        packages = [packages] if isinstance(packages, str) else packages
        packages = [str(Path(p).resolve()) for p in packages if Path(p).is_dir()]
        env['PYTHONPATH'] = os.pathsep.join(packages)
        if offline:
            project = scratch / 'home/projects/cirrus-digest'
            config = project / 'config'
            config.mkdir(parents=True)
            # Only source definitions, never live credentials/cookies/overlays.
            # Imports that need source configuration get isolated fixture paths.
            for name in ('sources.json', 'sources-pedagogy.json'):
                source = root / 'config' / name
                if source.is_file() and not source.is_symlink():
                    data = json.loads(source.read_text())
                    data.setdefault('digest', {}).update(
                        output_dir=str(scratch / 'digests'), log_dir=str(scratch / 'logs'))
                    if 'email' in data:
                        data['email']['accounts'] = []
                    (config / name).write_text(json.dumps(data))
            (scratch / 'home/cirrus-digest').symlink_to(project, target_is_directory=True)
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        if offline:
            env['COWORK_OFFLINE_TEST'] = '1'
        else:
            # Only the explicit --dry-run gate may use the existing read-only
            # live-config/network contract. Writes remain kernel-contained.
            if '--dry-run' not in command_args(args):
                raise ValueError('networked isolation requires --dry-run')
            env['HOME'] = str(Path.home())
            env['TARGET_ENV'] = os.environ.get('TARGET_ENV', 'dev')
            output = root / '.dryrun-output'
            output.mkdir(exist_ok=True)
            env['COWORK_DRYRUN_ROOT'] = tempfile.mkdtemp(prefix='run-', dir=str(output))
        system = platform.system()
        command = [str(a) for a in args]
        if system == 'Darwin':
            if not shutil.which('sandbox-exec'):
                raise RuntimeError('sandbox-exec unavailable; refusing uncontained test')
            # Keep system reads available for macOS dyld. Restrict user and
            # temporary data to this invocation and installed dependencies.
            reads = packages + [str(Path(sys.prefix).resolve()), str(root), str(scratch)]
            if not offline:
                reads += [str(Path.home() / 'projects/cirrus-digest'),
                          str(Path.home() / 'cirrus-digest')]
            profile = '(version 1) (allow default) (deny file-write*)\n'
            profile += '(deny network*)\n' if offline else '(deny network-bind)\n'
            for p in (Path.home(), Path(tempfile.gettempdir())):
                profile += '(deny file-read-data (subpath %s))\n' % json.dumps(str(p.resolve()))
            for p in reads:
                profile += '(allow file-read-data (subpath %s))\n' % json.dumps(str(Path(p).resolve()))
            for p in (root, scratch):
                profile += '(allow file-write* (subpath %s))\n' % json.dumps(str(p))
            profile += '(allow file-write-data (literal "/dev/null"))\n'
            command = ['sandbox-exec', '-p', profile] + command
        elif system == 'Linux':
            reads = ['/usr', '/lib', '/lib64', '/bin', '/sbin', '/etc', '/proc', '/dev', '/sys']
            reads += packages + [str(Path(sys.prefix).resolve())]
            if not offline:
                reads += [str(Path.home() / 'projects/cirrus-digest'),
                          str(Path.home() / 'cirrus-digest')]
            # Never grant a broad real HOME or /tmp read rule.
            reads = [p for p in reads if Path(p).resolve() not in (Path('/'), Path.home().resolve())]
            command = [sys.executable, str(Path(__file__).resolve()), '--child',
                       str(root), str(scratch), json.dumps(reads), str(int(offline))] + command
        else:
            raise RuntimeError('unsupported containment platform: ' + system)
        child = subprocess.Popen(command, cwd=str(root), env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 text=True, start_new_session=True)
        try:
            stdout, stderr = child.communicate(timeout=timeout)
            return subprocess.CompletedProcess(command, child.returncode, stdout, stderr)
        finally:
            # Stop the test process group, including ordinary descendants, so a
            # timeout cannot leave background writes in this worktree. This
            # does not supervise hostile descendants that deliberately setsid().
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            child.stdout.close()
            child.stderr.close()


def selftest():
    import unittest
    suite = unittest.defaultTestLoader.loadTestsFromName('test_runtime_isolation')
    return unittest.TextTestRunner(verbosity=1).run(suite).wasSuccessful()


if __name__ == '__main__':
    if sys.argv[1:] in (['selftest'], ['--selftest']):
        raise SystemExit(0 if selftest() else 1)
    if len(sys.argv) >= 5 and sys.argv[1] == '--run' and sys.argv[3] == '--':
        result = run(sys.argv[4:], sys.argv[2])
        sys.stdout.write(result.stdout)
        sys.stderr.write(result.stderr)
        raise SystemExit(result.returncode)
    if len(sys.argv) < 7 or sys.argv[1] != '--child':
        raise SystemExit('internal isolation launcher only')
    try:
        _linux_guard(sys.argv[2], sys.argv[3], json.loads(sys.argv[4]), bool(int(sys.argv[5])))
    except Exception as exc:
        print('isolation unavailable: ' + type(exc).__name__ + ': ' + str(exc), file=sys.stderr)
        raise SystemExit(125)
    os.execvpe(sys.argv[6], sys.argv[6:], os.environ)
