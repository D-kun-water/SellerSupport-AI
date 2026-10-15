"""Windows application launcher, using the standard library only."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
from uuid import uuid4
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.model_config import local_values, bootstrap_tokens, credential_file
startup_settings = local_values()
LOG = ROOT / 'storage/launcher'
SERVICES = [('workbench', 8012, startup_settings.get('SELLERSUPPORT_WORKBENCH_DB', 'storage/workbench.sqlite3'), 'empty'),
            ('validation', 8019, startup_settings.get('SELLERSUPPORT_VALIDATION_DB', 'storage/validation.sqlite3'), 'empty')]
kernel = ctypes.WinDLL('kernel32', use_last_error=True)
kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel.OpenProcess.restype = wintypes.HANDLE
kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
kernel.CloseHandle.argtypes = [wintypes.HANDLE]

def process_time(pid):
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return None
    try:
        exit_code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code)) or exit_code.value != 259:
            return None
        stamps = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(s) for s in stamps)):
            return None
        return stamps[0].dwHighDateTime << 32 | stamps[0].dwLowDateTime
    finally:
        kernel.CloseHandle(handle)

def owned(name):
    path = LOG / f'{name}.json'
    if not path.exists():
        return None
    record = json.loads(path.read_text(encoding='utf-8-sig'))
    if record.get('creation_time') and process_time(record['pid']) == record['creation_time']:
        return record['pid']
    return None

def listening(port):
    with socket.socket() as probe:
        probe.settimeout(1)
        return probe.connect_ex(('127.0.0.1', port)) == 0

def stop_owned(name):
    pid = owned(name)
    if not pid:
        return
    handle = kernel.OpenProcess(0x1001, False, pid)
    if not handle:
        raise RuntimeError(f'Cannot stop {name}.')
    try:
        if process_time(pid) != json.loads((LOG / f'{name}.json').read_text())['creation_time']:
            raise RuntimeError('Process identity changed; retry.')
        if not kernel.TerminateProcess(handle, 0):
            raise RuntimeError(f'Cannot stop {name}.')
    finally:
        kernel.CloseHandle(handle)
    deadline = time.monotonic() + 5
    while owned(name):
        if time.monotonic() > deadline:
            raise RuntimeError(f'{name} did not stop.')
        time.sleep(.1)

def ready(port):
    try:
        with urlopen(f'http://127.0.0.1:{port}/api/health', timeout=2) as response:
            data = json.load(response)['data']
        with urlopen(f'http://127.0.0.1:{port}/', timeout=2) as response:
            return (data['status'] == 'ok'
                    and data.get('translation_api_version') == 1
                    and data.get('workflow_api_version') == 3 and data.get('knowledge_catalog_api_version') == 2 and response.status == 200)
    except Exception:
        return False


def frontend_needs_build():
    entry = ROOT / 'frontend/dist/index.html'
    if not entry.is_file():
        return True
    inputs = list((ROOT/'frontend/src').rglob('*'))
    inputs += [ROOT/'frontend'/name for name in ('index.html','package.json','package-lock.json','tsconfig.json','vite.config.ts')]
    built_at = entry.stat().st_mtime_ns
    return any(path.is_file() and path.stat().st_mtime_ns > built_at for path in inputs)

def initialize_database(database, mode):
    """Create only missing local databases; existing business records stay intact."""
    path = ROOT / database
    if path.exists():
        return
    from app.store import Store
    from app.model_config import local_values
    from app import merchant
    staged = path.with_name(path.name + '.initializing-' + uuid4().hex)
    try:
        store = Store(staged)
        if mode == 'test':
            store.seed()
            from app.reships import seed_policy
            seed_policy(store)
        settings = local_values()
        tokens = bootstrap_tokens(path, settings)
        identities = [(tokens[role],
                       dict(id='local-'+role,role=role,orders=['TEST-O1','TEST-O2','TEST-O3','TEST-O4']))
                      for role in ('agent','supervisor')]
        if len({token for token,_ in identities}) != len(identities):
            raise RuntimeError('Local identity tokens must be distinct')
        merchant.seed_members(store, identities, empty=mode=='empty')
        staged.rename(path)
    finally:
        if staged.exists():
            staged.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--stop', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    LOG.mkdir(parents=True, exist_ok=True)
    if args.stop:
        for name, *_ in SERVICES:
            pid = owned(name)
            if pid:
                stop_owned(name)
                print(f'Stopped {name}.')
        return
    if frontend_needs_build():
        dependency_lock=ROOT/'frontend/node_modules/.package-lock.json'
        package_lock=ROOT/'frontend/package-lock.json'
        if not dependency_lock.is_file() or package_lock.stat().st_mtime_ns > dependency_lock.stat().st_mtime_ns:
            subprocess.run(['npm.cmd', 'ci'], cwd=ROOT/'frontend', check=True)
        subprocess.run(['npm.cmd', 'run', 'build'], cwd=ROOT/'frontend', check=True)
    import fastapi, uvicorn
    for name, port, database, _ in SERVICES:
        if listening(port) and not owned(name):
            raise RuntimeError(f'Port {port} is used by another service. Close it and retry.')
    for _, _, database, mode in SERVICES:
        initialize_database(database, mode)
        credentials = credential_file(ROOT/database)
        if credentials.exists():
            print('Local initial credentials: '+str(credentials), flush=True)
    for name, port, database, mode in SERVICES:
        if owned(name) and not ready(port):
            # Only replace a process whose PID and creation time belong to this launcher.
            # An old backend can otherwise serve new JS while lacking its translation API.
            stop_owned(name)
        if not owned(name):
            env = dict(os.environ)
            env.update(PYTHONIOENCODING='utf-8', PYTHONDONTWRITEBYTECODE='1',
                       SELLERSUPPORT_DB=database, SELLERSUPPORT_SEED_MODE=mode,
                       SELLERSUPPORT_AI_ENABLED=startup_settings.get('SELLERSUPPORT_AI_ENABLED','false'))
            with (LOG / f'{name}.out.log').open('w') as out, (LOG / f'{name}.err.log').open('w') as err:
                process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'app.main:app',
                    '--host', '127.0.0.1', '--port', str(port)], cwd=ROOT, env=env,
                    stdin=subprocess.DEVNULL, stdout=out, stderr=err,
                    creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP)
            (LOG / f'{name}.json').write_text(json.dumps({'pid': process.pid,
                'creation_time': process_time(process.pid)}), encoding='utf-8')
        deadline = time.monotonic() + 40
        while not ready(port):
            if not owned(name) or time.monotonic() > deadline:
                raise RuntimeError(f'{name} failed. See storage/launcher/{name}.err.log')
            time.sleep(.5)
        print(f'Ready: http://127.0.0.1:{port}/', flush=True)
    if not args.no_browser:
        os.startfile('http://127.0.0.1:8012/')
    print('Project ready. To stop services: python scripts/run.py --stop')

if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print(f'Application launch failed: {exc}', file=sys.stderr)
        sys.exit(1)
