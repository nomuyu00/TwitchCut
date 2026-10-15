"""Run one authorized local stage; retain compact state and full logs locally.

This is not a scheduler or uploader. A zero exit code is process success, not QA.
"""
from __future__ import annotations

import argparse
from contextlib import ExitStack, contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time
import uuid


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8-sig'))


def write(path, value):
    path = Path(path)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def stamp():
    return datetime.now(timezone.utc).isoformat()


def paths(job_path):
    job = read(job_path)
    root, work = Path(job['root']).resolve(), Path(job['work']).resolve()
    if job.get('schema') != 1 or not work.is_relative_to(root):
        raise ValueError('Invalid job paths/schema')
    return root, work


def within(root, path):
    path = Path(path).resolve()
    if not path.is_relative_to(root):
        raise ValueError(f'Path escaped job: {path}')
    return path


@contextmanager
def lease(path, stage):
    """Fail on existing leases; never remove another or a stale process's lease."""
    path.parent.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump({'pid': os.getpid(), 'token': token, 'stage': stage, 'created_at': stamp()}, stream)
        yield
    finally:
        try:
            if read(path).get('token') == token:
                path.unlink()
        except (OSError, ValueError):
            pass


def emit(value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


class OwnedWindowsJob:
    """A private nested Job Object, using only the handle of our own Popen child."""
    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self.ctypes = ctypes
        self.attached = False
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)

        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_longlong), ('job_time', ctypes.c_longlong),
                        ('flags', wintypes.DWORD), ('min_ws', ctypes.c_size_t), ('max_ws', ctypes.c_size_t),
                        ('active_limit', wintypes.DWORD), ('affinity', ctypes.c_size_t),
                        ('priority', wintypes.DWORD), ('scheduling', wintypes.DWORD)]

        class Io(ctypes.Structure):
            _fields_ = [(name, ctypes.c_ulonglong) for name in
                        ('read_ops', 'write_ops', 'other_ops', 'read_bytes', 'write_bytes', 'other_bytes')]

        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', Io), ('process_memory', ctypes.c_size_t),
                        ('job_memory', ctypes.c_size_t), ('peak_process', ctypes.c_size_t),
                        ('peak_job', ctypes.c_size_t)]

        self.api.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
        self.api.CreateJobObjectW.restype = wintypes.HANDLE
        self.api.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
        self.api.SetInformationJobObject.restype = wintypes.BOOL
        self.api.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
        self.api.AssignProcessToJobObject.restype = wintypes.BOOL
        self.api.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
        self.api.TerminateJobObject.restype = wintypes.BOOL
        self.api.CloseHandle.argtypes = (wintypes.HANDLE,)
        self.api.CloseHandle.restype = wintypes.BOOL
        self.handle = self.api.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        info = Extended()
        info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self.api.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
            code = ctypes.get_last_error()
            self.close()
            raise ctypes.WinError(code)

    def attach(self, child):
        if not self.api.AssignProcessToJobObject(self.handle, int(child._handle)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        self.attached = True

    def terminate(self):
        if not self.api.TerminateJobObject(self.handle, 130):
            raise self.ctypes.WinError(self.ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.api.CloseHandle(self.handle)
            self.handle = None


def stop_owned_child(child, windows_job=None):
    if child.poll() is not None:
        return
    if windows_job is not None and windows_job.attached:
        windows_job.terminate()
    else:
        child.terminate()
    try:
        child.wait(timeout=10)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait(timeout=10)


def run(args):
    root, work = paths(args.job)
    spec_path = within(work, args.spec)
    spec = read(spec_path)
    stage = spec.get('id', '')
    command = spec.get('command')
    if spec.get('schema') != 1 or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', stage):
        raise ValueError('Stage requires schema 1 and a stable simple id')
    if not isinstance(command, list) or not command or not all(isinstance(v, str) and '\x00' not in v for v in command):
        raise ValueError('command must be an argument array; shell text is unsupported')
    if not Path(command[0]).is_absolute() or not Path(command[0]).is_file():
        raise ValueError('Executable must be an existing absolute path')
    cwd = within(root, spec.get('cwd', work))
    if not cwd.is_dir():
        raise ValueError('cwd must be an existing workspace directory')
    resources = spec.get('resources', [])
    if not isinstance(resources, list) or len(resources) != len(set(resources)) or not all(
            isinstance(v, str) and re.fullmatch(r'[a-z][a-z0-9-]{0,40}', v) for v in resources):
        raise ValueError('Invalid resource lease names')
    seconds = spec.get('expected_seconds')
    if seconds is not None and (isinstance(seconds, bool) or not isinstance(seconds, (float, int))
                                or not math.isfinite(seconds) or seconds <= 0):
        raise ValueError('expected_seconds must be a positive finite media duration')
    log = work / 'logs' / (stage + '.log')
    log.parent.mkdir(parents=True, exist_ok=True)
    state_path = work / 'stage-state.json'
    state = {'schema': 1, 'stage': stage, 'status': 'starting', 'worker_pid': os.getpid(),
             'started_at': stamp(), 'spec': str(spec_path),
             'spec_sha256': hashlib.sha256(spec_path.read_bytes()).hexdigest(),
             'log': str(log), 'resources': resources, 'return_code': None}
    child = None
    windows_job = None
    reader = None
    progress = {}
    progress_lock = threading.Lock()
    reader_errors = []
    start = time.monotonic()

    def capture(stream, out):
        try:
            for line in iter(stream.readline, b''):
                out.write(line)
                out.flush()
                if len(line) <= 4096:
                    key, sep, value = line.decode('utf-8', errors='replace').strip().partition('=')
                    if sep and key in ('frame', 'out_time_us', 'out_time', 'speed', 'progress'):
                        with progress_lock:
                            progress[key] = value
        except Exception as error:
            reader_errors.append(str(error))
        finally:
            stream.close()

    with ExitStack() as stack:
        stack.enter_context(lease(work / 'stage-run.lock', stage))
        for resource in sorted(resources):
            stack.enter_context(lease(root / 'editing/publishing/resource-leases' / (resource + '.lock'), stage))
        # Reuse requires explicit new revision; don't overwrite prior execution evidence.
        out = stack.enter_context(log.open('xb'))
        write(state_path, state)
        try:
            if os.name == 'nt':
                windows_job = OwnedWindowsJob()
            flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == 'nt' else 0
            child = subprocess.Popen(command, cwd=cwd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     shell=False, creationflags=flags)
            if windows_job is not None:
                windows_job.attach(child)
            state.update(status='running', child_pid=child.pid)
            write(state_path, state)
            emit({'stage': stage, 'status': 'running', 'child_pid': child.pid, 'state': str(state_path)})
            reader = threading.Thread(target=capture, args=(child.stdout, out), daemon=True)
            reader.start()
            previous_bucket = 0
            while child.poll() is None:
                try:
                    child.wait(timeout=args.interval)
                except subprocess.TimeoutExpired:
                    pass
                with progress_lock:
                    state['progress'] = dict(progress)
                state.update(updated_at=stamp(), elapsed_seconds=round(time.monotonic() - start, 2))
                if seconds and 'out_time_us' in state['progress']:
                    try:
                        pct = min(100.0, max(0.0, float(state['progress']['out_time_us']) / (seconds * 10000)))
                        state['percent'] = round(pct, 1)
                        bucket = int(pct // 10)
                        if bucket > previous_bucket:
                            emit({'stage': stage, 'status': 'running', 'percent': state['percent']})
                            previous_bucket = bucket
                    except ValueError:
                        pass
                write(state_path, state)
            reader.join(timeout=10)
            if reader.is_alive() or reader_errors:
                raise RuntimeError('Log capture incomplete: ' + '; '.join(reader_errors))
            state.update(status='succeeded' if child.returncode == 0 else 'failed', return_code=child.returncode)
        except KeyboardInterrupt:
            if child is not None:
                stop_owned_child(child, windows_job)
            state.update(status='interrupted', return_code=child.returncode if child else None)
        except Exception as error:
            if child is not None:
                stop_owned_child(child, windows_job)
            state.update(status='failed', return_code=child.returncode if child else None,
                         error=str(error))
        finally:
            if windows_job is not None:
                windows_job.close()
            if reader is not None:
                reader.join(timeout=10)
            with progress_lock:
                state['progress'] = dict(progress)
            state.update(updated_at=stamp(), elapsed_seconds=round(time.monotonic() - start, 2))
            write(state_path, state)
            emit({k: state[k] for k in ('stage', 'status', 'return_code', 'elapsed_seconds')})
    return 0 if state['status'] == 'succeeded' else (130 if state['status'] == 'interrupted' else 1)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    execute = commands.add_parser('run')
    execute.add_argument('--job', required=True)
    execute.add_argument('--spec', required=True)
    execute.add_argument('--interval', type=float, default=10)
    status = commands.add_parser('status')
    status.add_argument('--job', required=True)
    args = parser.parse_args()
    try:
        if args.action == 'status':
            _, work = paths(args.job)
            state_path = work / 'stage-state.json'
            value = read(state_path) if state_path.is_file() else {'status': 'not_started'}
            # Last-written state is not a fresh process-liveness or QA assertion.
            emit({'last_written_state': value, 'process_liveness_checked': False, 'qa_inferred': False})
            return 0
        if not math.isfinite(args.interval) or not 1 <= args.interval <= 60:
            raise ValueError('Local observation interval must be 1–60 seconds')
        return run(args)
    except (OSError, ValueError) as error:
        emit({'error': str(error)})
        return 2


if __name__ == '__main__':
    sys.exit(main())
