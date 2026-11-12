#!/usr/bin/env python3
"""eYeYaYe master terminal — native process lifecycle; no Docker or sudo."""
from __future__ import annotations
import getpass, hashlib, json, os, platform, select, shutil, signal, subprocess, sys, tarfile, time, ast, shlex, socket, socketserver, threading, queue
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RPC_SOCKET = ROOT / 'runtime' / 'cctv.sock'
MASTER_CONTROL_SOCKET = ROOT / 'runtime' / 'master-control.sock'
MASTER_COMMANDS = queue.Queue()
MASTER_CONTROL_SERVER = None
GUI_PROCESSES: list[subprocess.Popen] = []
engine_proc: subprocess.Popen | None = None
_SHUTDOWN_STARTED = False


class MasterControlHandler(socketserver.StreamRequestHandler):
    def handle(self):
        try:
            line = self.rfile.readline(4097)
            if not line or len(line) > 4096:
                raise ValueError('Invalid master-control request.')
            request = json.loads(line.decode('utf-8'))
            action = str(request.get('action', ''))
            if action != 'restart':
                raise ValueError('Unsupported master-control action.')
            MASTER_COMMANDS.put('restart')
            self.wfile.write(b'{"ok":true,"status":"queued"}\n')
        except Exception as exc:
            payload = json.dumps({'ok':False,'error':f'{type(exc).__name__}: {exc}'}).encode() + b'\n'
            try: self.wfile.write(payload)
            except OSError: pass


class MasterControlServer(socketserver.ThreadingUnixStreamServer):
    daemon_threads = True
    allow_reuse_address = True


def start_master_control_server():
    global MASTER_CONTROL_SERVER
    MASTER_CONTROL_SOCKET.parent.mkdir(parents=True, exist_ok=True)
    MASTER_CONTROL_SOCKET.unlink(missing_ok=True)
    server = MasterControlServer(str(MASTER_CONTROL_SOCKET), MasterControlHandler)
    os.chmod(MASTER_CONTROL_SOCKET, 0o600)
    threading.Thread(target=server.serve_forever, daemon=True, name='master-control').start()
    MASTER_CONTROL_SERVER = server
    say('Master lifecycle control socket ready.')


def stop_master_control_server():
    global MASTER_CONTROL_SERVER
    server = MASTER_CONTROL_SERVER
    MASTER_CONTROL_SERVER = None
    if server is not None:
        try: server.shutdown()
        except Exception: pass
        try: server.server_close()
        except Exception: pass
    MASTER_CONTROL_SOCKET.unlink(missing_ok=True)


def restart_everything():
    """Restart all GUI-owned players and the master-owned native media engine."""
    say('RESTART EVERYTHING: closing GUI players and stopping the engine, camera bridge, recorders and project MediaMTX.')
    for proc in list(GUI_PROCESSES):
        try:
            _terminate_process_group(proc.pid, graceful_seconds=8)
            try: proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired: pass
        except Exception as exc:
            say(f'GUI restart warning (PID {proc.pid}): {exc}')
    GUI_PROCESSES.clear()
    stop_engine()
    start_engine()
    open_gui()
    say('RESTART EVERYTHING COMPLETE.')


def stamp(): return datetime.now().astimezone().strftime('%Y%m%d_%H%M%S')
def say(text=''): print(text, flush=True)
def shlex_split(text): return shlex.split(text)


def run(cmd, check=False, capture=False, timeout=None, env=None):
    say('EXEC ' + shlex.join(str(x) for x in cmd))
    return subprocess.run([str(x) for x in cmd], cwd=ROOT, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.STDOUT if capture else None,
        check=check, timeout=timeout, env=env or os.environ.copy())


def control_values():
    try:
        tree = ast.parse((ROOT / 'USER_CONTROL.py').read_text(encoding='utf-8'))
        out = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                try: value = ast.literal_eval(node.value)
                except Exception: continue
                for target in (node.targets if isinstance(node, ast.Assign) else [node.target]):
                    if isinstance(target, ast.Name): out[target.id] = value
        return out
    except Exception:
        return {}


def backup_root():
    rel = str(control_values().get('BACKUP_DIRECTORY', 'backups'))
    path = (ROOT / rel).resolve()
    if not path.is_relative_to(ROOT):
        raise RuntimeError('BACKUP_DIRECTORY must remain inside the project folder.')
    return path


def remove_legacy_docker_files():
    """Remove only this release's obsolete Docker descriptors, never user data."""
    for name in ('Dockerfile', 'docker-compose.yml', '.dockerignore'):
        try: (ROOT / name).unlink(missing_ok=True)
        except OSError as exc: say(f'Legacy-file cleanup warning for {name}: {exc}')
    legacy_dir = ROOT / 'docker'
    try: (legacy_dir / '.gitkeep').unlink(missing_ok=True)
    except OSError: pass
    try: legacy_dir.rmdir()  # succeeds only when empty
    except OSError: pass


def ensure_layout():
    if not os.access(ROOT, os.R_OK | os.W_OK | os.X_OK):
        raise SystemExit('The project directory must be writable by the current user.')
    for name in ('config', 'control', 'data', 'logs', 'runtime', 'cache', 'backups'):
        (ROOT / name).mkdir(parents=True, exist_ok=True)
    for relative in ('config/cameras.json', 'config/bookmarks.json', 'USER_CONTROL.py',
                     'control/COMMANDS.json', 'cam3/rtmp_cam.py'):
        path = ROOT / relative
        if path.is_file():
            try: path.chmod(0o600)
            except OSError: pass
    backup_root().mkdir(parents=True, exist_ok=True)
    remove_legacy_docker_files()


def check_host():
    if platform.system() != 'Linux':
        raise SystemExit('eYeYaYe requires Linux.')
    if platform.machine().lower() not in ('x86_64', 'amd64'):
        raise SystemExit('This eYeYaYe release is built for Linux amd64/x86_64.')
    try:
        import tkinter  # noqa: F401
    except Exception as exc:
        raise SystemExit('Tkinter is required. Install it once with: sudo apt install python3-tk') from exc
    if not shutil.which('mpv'):
        raise SystemExit('MPV is required for continuous video playback. Install it once with: sudo apt install mpv')
    bundle = ROOT / 'cam3' / 'CAM3_LIVE_STANDALONE'
    required = (bundle / 'rtmp_live_bridge.py', bundle / 'rtmp_cam.py',
                bundle / 'control' / 'COMMANDS.json', bundle / 'bin' / 'mediamtx',
                bundle / 'bin' / 'ffmpeg', bundle / 'bin' / 'ffprobe')
    missing = [str(p.relative_to(ROOT)) for p in required if not p.is_file()]
    nonexec = [str(p.relative_to(ROOT)) for p in required[-3:] if p.is_file() and not os.access(p, os.X_OK)]
    if missing or nonexec:
        lines = ['The project-contained media runtime is incomplete. No host-wide MediaMTX/FFmpeg is used.']
        if missing: lines.append('Missing: ' + ', '.join(missing))
        if nonexec: lines.append('Not executable: ' + ', '.join(nonexec))
        lines += ['', 'Build it once (internet is required only for this manual build step):',
                  f'  /usr/bin/python3 {ROOT / "BUILD_RUNTIME.py"}',
                  'Then start eYeYaYe again. The launcher itself performs no downloads.']
        raise SystemExit('\n'.join(lines))


def rpc_request(action, params=None, timeout=12):
    request = json.dumps({'action': action, 'params': params or {}}, separators=(',', ':')).encode() + b'\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(RPC_SOCKET))
        sock.sendall(request)
        data = bytearray()
        while len(data) < 8_000_000:
            block = sock.recv(65536)
            if not block: break
            data.extend(block)
            if b'\n' in block: break
    if not data: raise RuntimeError('Native engine returned no response.')
    response = json.loads(bytes(data).split(b'\n', 1)[0].decode('utf-8'))
    if not response.get('ok'): raise RuntimeError(response.get('error', 'Native engine request failed.'))
    return response.get('result', {})


def wait_for_engine(timeout=35):
    deadline = time.monotonic() + timeout
    last_error = 'engine socket has not appeared yet'
    while time.monotonic() < deadline:
        if engine_proc is not None and engine_proc.poll() is not None:
            break
        try:
            rpc_request('ping', timeout=1)
            say('Native engine RPC socket is ready.')
            return
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'
            time.sleep(0.35)
    log = ROOT / 'logs' / 'engine.log'
    detail = log.read_text(errors='replace')[-6000:] if log.is_file() else ''
    raise RuntimeError('eYeYaYe engine did not become ready. ' + last_error + ('\nRecent engine log:\n' + detail if detail else ''))


def start_engine():
    global engine_proc
    if engine_proc is not None and engine_proc.poll() is None:
        say('eYeYaYe engine is already running under this master terminal.')
        return
    try:
        rpc_request('ping', timeout=1)
        raise RuntimeError('An eYeYaYe engine is already listening outside this master terminal. Refusing to start a duplicate.')
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout, TimeoutError, OSError, json.JSONDecodeError):
        pass
    ensure_layout()
    log_path = ROOT / 'logs' / 'engine.log'
    env = os.environ.copy()
    env['CCTV_PROJECT_ROOT'] = str(ROOT)
    env['PYTHONUNBUFFERED'] = '1'
    env['PATH'] = str(ROOT / 'cam3' / 'CAM3_LIVE_STANDALONE' / 'bin') + os.pathsep + env.get('PATH', '')
    with log_path.open('ab', buffering=0) as log:
        engine_proc = subprocess.Popen([sys.executable, '-u', str(ROOT / 'app' / 'engine.py')],
            cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log,
            stderr=subprocess.STDOUT, start_new_session=True)
    say(f'Engine process started (PID {engine_proc.pid}).')
    wait_for_engine()


def _terminate_process_group(pgid: int, graceful_seconds: float = 8.0) -> None:
    try: os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError: return
    deadline = time.monotonic() + graceful_seconds
    while time.monotonic() < deadline:
        try: os.killpg(pgid, 0)
        except ProcessLookupError: return
        time.sleep(0.1)
    try: os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError: pass


def stop_engine():
    global engine_proc
    proc = engine_proc
    if proc is None:
        say('Engine is not running under this master terminal.')
        return
    if proc.poll() is None:
        say('Stopping the local engine. CAM3 bridge, FFmpeg recorders and project MediaMTX will close.')
        try: proc.send_signal(signal.SIGTERM)
        except ProcessLookupError: pass
        try: proc.wait(timeout=22)
        except subprocess.TimeoutExpired:
            say('Engine did not stop cleanly; terminating its owned process group.')
            _terminate_process_group(proc.pid, graceful_seconds=2)
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired: pass
    engine_proc = None


def reap_gui_processes():
    survivors = []
    for proc in GUI_PROCESSES:
        if proc.poll() is None:
            survivors.append(proc)
        else:
            _terminate_process_group(proc.pid, graceful_seconds=1)
            try: proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired: pass
    GUI_PROCESSES[:] = survivors


def open_gui():
    gui = ROOT / 'app' / 'gui.py'
    if not gui.is_file(): raise RuntimeError('Native desktop GUI file is missing.')
    if not shutil.which('mpv'):
        say('MPV is required for continuous native live/playback video. Install it once: sudo apt install mpv')
        return
    if engine_proc is None or engine_proc.poll() is not None:
        raise RuntimeError('The local engine is stopped. Type start before opening the GUI.')
    GUI_PROCESSES[:] = [p for p in GUI_PROCESSES if p.poll() is None]
    say('Launching the native desktop GUI. Closing it does NOT stop camera streams or recording.')
    gui_log = ROOT / 'logs' / 'gui-console.log'
    gui_log.parent.mkdir(parents=True, exist_ok=True)
    with gui_log.open('ab', buffering=0) as output:
        proc = subprocess.Popen([sys.executable, str(gui)], cwd=ROOT,
            stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
            env={**os.environ, 'CCTV_PROJECT_ROOT': str(ROOT)}, start_new_session=True)
    GUI_PROCESSES.append(proc)


def editor_for(path: Path):
    editor = os.environ.get('EDITOR')
    cmd = shlex_split(editor) if editor else None
    if not cmd:
        gedit = shutil.which('gedit')
        cmd = [gedit] if gedit else [shutil.which('vi') or '/usr/bin/vi']
    say('EXEC ' + shlex.join(cmd + [str(path)]))
    subprocess.Popen(cmd + [str(path)], cwd=ROOT)


def parse_launch_mode(args):
    if not args or args in (['-G'], ['--gui']): return 'gui'
    if args in (['-T'], ['--terminal']): return 'terminal'
    if args in (['help'], ['--help'], ['-h']): return 'help'
    raise ValueError('Usage: ./launcher [-G|--gui] [-T|--terminal] | --help')


def safe_status():
    try:
        state = rpc_request('state.get', timeout=2)
        cams = state.get('cameras', [])
        online = 'RUNNING' if state.get('media_server') else 'STARTING / ERROR'
        types = ', '.join(f"{c.get('id')}:{c.get('type')}:{'enabled' if c.get('enabled') else 'off'}" for c in cams) or 'no cameras configured'
        return (f"{online} | cameras={len(cams)} [{types}] | original segments={state.get('original_segments',0)} "
                f"| archives={state.get('archive_files',0)} | free={state.get('disk_free_gb','?')} GB")
    except Exception as exc:
        return 'Native engine not ready: ' + str(exc)


def help_text():
    say('''eYeYaYe MASTER TERMINAL — NATIVE STANDALONE
------------------------------------------
help                         Show this help.
status                       Show engine, camera, recording and disk status.
gui                          Open/reopen the desktop GUI; engine keeps running.
logs                         Show local application and MediaMTX logs.
logs follow                  Follow local logs (Ctrl+C returns here).
cameras                      List configured cameras without passwords.
camera add                   Add an RTSP or CAM3 camera interactively.
config show                  Display USER_CONTROL.py.
config edit                  Open USER_CONTROL.py (gedit preferred).
commands show                Display FFmpeg/FFprobe command templates.
commands edit                Open control/COMMANDS.json.
archive build <camera> <day> Build a daily archive, e.g. archive build cam1 2026-11-01.
purge preview                Preview purge candidates; never deletes files.
purge execute                Requires enabled switches and typed confirmation.
backup                       Create a project backup without build cache.
backup with-cache            Include the local download/build cache.
backup without-cache         Exclude cache.
restore <file>               Verify and restore a .tar.gz backup with rollback.
stop                         Stop this project's local media engine.
start                        Start the local media engine.
rebuild                      Rebuild project-local MediaMTX/FFmpeg, then restart engine.
restart                      Restart the complete GUI and engine process stack.
quit                         Stop GUI players and all processes owned by this project.

MediaMTX, FFmpeg and FFprobe live under cam3/CAM3_LIVE_STANDALONE/bin/.
The launcher runs directly as the current user and never downloads binaries.
Only the manual rebuild command downloads upstream runtime archives.
''')


def list_cameras():
    path = ROOT / 'config' / 'cameras.json'
    doc = json.loads(path.read_text(encoding='utf-8'))
    say('ID       TYPE   ENABLED   NAME / ADDRESS')
    for cam in doc.get('cameras', []):
        addr = cam.get('rtsp_url') if cam.get('type') == 'rtsp' else 'Project-local CAM3 bridge'
        say(f"{cam.get('id',''):<8} {cam.get('type',''):<6} {str(cam.get('enabled',True)):<9} {cam.get('name','')} | {addr}")
    say('Passwords are intentionally not displayed.')


def add_camera():
    say('ADD CAMERA')
    cid = input('Camera ID (e.g. cam4): ').strip().lower()
    name = input('Display name: ').strip() or cid
    typ = input('Type [rtsp/cam3] (rtsp): ').strip().lower() or 'rtsp'
    if typ not in ('rtsp', 'cam3'):
        say('Invalid camera type.'); return
    url = input('RTSP URL without credentials: ').strip() if typ == 'rtsp' else ''
    user = input('Username: ').strip() if typ == 'rtsp' else ''
    password = getpass.getpass('Password (input hidden): ') if typ == 'rtsp' else ''
    web = input('Camera administration URL (optional): ').strip()
    web_user = input('Camera web-admin username (optional): ').strip()
    web_password = getpass.getpass('Camera web-admin password (input hidden, optional): ') if web_user else ''
    result = rpc_request('cameras.save', {'id':cid,'name':name,'type':typ,'rtsp_url':url,'username':user,
        'password':password,'web_admin_url':web,'web_admin_username':web_user,
        'web_admin_password':web_password,'enabled':False})
    say('Camera saved disabled. Review the settings, then enable it in the GUI or config/cameras.json.')


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''): digest.update(block)
    return digest.hexdigest()


def create_backup(include_cache: bool):
    backups = backup_root(); backups.mkdir(parents=True, exist_ok=True)
    name = f'eYeYaYe_{stamp()}_' + ('with-cache' if include_cache else 'without-cache') + '.tar.gz'
    out = backups / name; temporary = out.with_suffix('.tar.gz.tmp')
    excludes = {'backups', '.git', '.restore_tmp', '.restore_recovery', '__pycache__', '.pytest_cache', 'docker'}
    if not include_cache: excludes.add('cache')
    say('Creating project backup. Recordings, settings, CAM3 sources and bundled binaries are included.')
    with tarfile.open(temporary, 'w:gz', compresslevel=4) as archive:
        for item in sorted(ROOT.iterdir()):
            if item.name in excludes: continue
            archive.add(item, arcname=item.name, recursive=True)
    with tarfile.open(temporary, 'r:gz') as check: check.getmembers()
    os.chmod(temporary, 0o600); os.replace(temporary, out)
    digest = sha256_file(out)
    sidecar = out.with_suffix(out.suffix + '.sha256')
    sidecar.write_text(digest + '  ' + out.name + '\n', encoding='utf-8'); sidecar.chmod(0o600)
    say(f'BACKUP CREATED: {out}')
    say(f'Size: {out.stat().st_size} bytes')
    say(f'SHA256: {digest}')
    say('SECURITY: the backup contains camera credentials; keep it private.')


def safe_extract(archive: tarfile.TarFile, destination: Path):
    seen = set()
    members = archive.getmembers()
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or '..' in path.parts or not path.parts:
            raise RuntimeError('Unsafe path in backup: ' + member.name)
        if member.issym() or member.islnk() or member.isdev() or member.isfifo():
            raise RuntimeError('Links/devices are not allowed in backup: ' + member.name)
        if member.name in seen: raise RuntimeError('Duplicate path in backup: ' + member.name)
        seen.add(member.name)
    archive.extractall(destination, members=members)


def create_rollback_copy():
    path = backup_root() / f'PRE_RESTORE_{stamp()}.tar.gz'
    temporary = path.with_suffix('.tar.gz.tmp')
    with tarfile.open(temporary, 'w:gz', compresslevel=2) as archive:
        for item in ROOT.iterdir():
            if item.name in ('backups', '.restore_tmp', '.restore_recovery', '.git', 'docker'): continue
            archive.add(item, arcname=item.name, recursive=True)
    temporary.chmod(0o600); os.replace(temporary, path)
    return path


def replace_from_stage(stage: Path, clear_cache_if_missing=False):
    for item in stage.iterdir():
        if item.name in ('backups', '.restore_tmp', '.restore_recovery'): continue
        destination = ROOT / item.name
        if destination.is_dir(): shutil.rmtree(destination)
        elif destination.exists(): destination.unlink()
        if item.is_dir(): shutil.copytree(item, destination)
        else: shutil.copy2(item, destination)
    if clear_cache_if_missing and not (stage / 'cache').exists():
        shutil.rmtree(ROOT / 'cache', ignore_errors=True)


def restore_backup(file_arg: str):
    source = Path(file_arg)
    if not source.is_absolute(): source = backup_root() / source
    source = source.resolve()
    if not source.is_file() or source.suffixes[-2:] != ['.tar', '.gz']:
        raise SystemExit('Restore expects an existing .tar.gz backup file.')
    digest = sha256_file(source)
    sidecar = source.with_suffix(source.suffix + '.sha256')
    if sidecar.is_file() and sidecar.read_text(encoding='utf-8').split()[0] != digest:
        raise SystemExit('Backup SHA256 check failed. No files changed.')
    say('Backup SHA256: ' + digest)
    if input('Type RESTORE eYeYaYe to stop services and apply this backup: ').strip() != 'RESTORE eYeYaYe':
        say('Restore cancelled.'); return
    stage = ROOT / '.restore_tmp'; recovery = ROOT / '.restore_recovery'
    shutil.rmtree(stage, ignore_errors=True); stage.mkdir()
    rollback = None
    stop_engine()
    try:
        with tarfile.open(source, 'r:gz') as archive: safe_extract(archive, stage)
        required = ('VERSION', 'BUILD_RUNTIME.py', 'USER_CONTROL.py', 'config/cameras.json',
            'control/COMMANDS.json', 'master.py', 'app/engine.py', 'app/gui.py',
            'app/core.py', 'cam3/rtmp_live_bridge.py', 'cam3/rtmp_cam.py')
        missing = [name for name in required if not (stage / name).exists()]
        if missing: raise RuntimeError('Backup validation failed; missing: ' + ', '.join(missing))
        if control_values().get('RESTORE_CREATE_ROLLBACK_COPY', True):
            rollback = create_rollback_copy(); say('Rollback copy: ' + str(rollback))
        replace_from_stage(stage, clear_cache_if_missing=True)
        remove_legacy_docker_files()
        shutil.rmtree(stage, ignore_errors=True)
        ensure_layout(); check_runtime_bundle()
        start_engine()
        say('RESTORE COMPLETE. The pre-restore rollback archive remains in backups/.')
    except Exception as exc:
        say('RESTORE FAILED: ' + str(exc))
        if rollback and rollback.is_file():
            say('Attempting automatic rollback...')
            try:
                shutil.rmtree(recovery, ignore_errors=True); recovery.mkdir()
                with tarfile.open(rollback, 'r:gz') as archive: safe_extract(archive, recovery)
                replace_from_stage(recovery)
                remove_legacy_docker_files(); ensure_layout(); check_runtime_bundle(); start_engine()
                say('AUTOMATIC ROLLBACK COMPLETED.')
            except Exception as rollback_error:
                say('AUTOMATIC ROLLBACK ALSO FAILED: ' + str(rollback_error))
                say('Keep the rollback archive and project directory intact for manual recovery.')
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(recovery, ignore_errors=True)


def check_runtime_bundle():
    bundle = ROOT / 'cam3' / 'CAM3_LIVE_STANDALONE'
    required = ('rtmp_live_bridge.py', 'rtmp_cam.py', 'control/COMMANDS.json',
                'bin/mediamtx', 'bin/ffmpeg', 'bin/ffprobe')
    missing = [name for name in required if not (bundle / name).is_file()]
    if missing: raise RuntimeError('Project-contained runtime bundle is incomplete: ' + ', '.join(missing))
    for name in ('mediamtx', 'ffmpeg', 'ffprobe'):
        path = bundle / 'bin' / name
        if not os.access(path, os.X_OK): raise RuntimeError(f'Bundled executable is not executable: {path}')


def purge_execute():
    values = control_values()
    if not values.get('PURGE_ENABLED', False) or values.get('PURGE_DRY_RUN', True):
        say('STOP: set PURGE_ENABLED=True and PURGE_DRY_RUN=False in USER_CONTROL.py first.'); return
    categories = {
        'original footage': bool(values.get('PURGE_ORIGINAL_AFTER_VERIFIED_ARCHIVE', False)),
        'playback/work cache': bool(values.get('PURGE_CACHE_PLAYBACK_ENABLED', False)),
        'temporary job files': bool(values.get('PURGE_TEMP_JOB_FILES_ENABLED', False)),
        'rotated logs': bool(values.get('PURGE_ROTATED_LOGS_ENABLED', False)),
    }
    enabled = [key for key, active in categories.items() if active]
    if not enabled: say('STOP: no purge category is enabled.'); return
    phrase = values.get('PURGE_CONFIRMATION_TEXT', 'PURGE CCTV DATA')
    say('WARNING: enabled purge categories: ' + ', '.join(enabled))
    if input(f'Type exactly {phrase!r} to continue: ').strip() != phrase:
        say('Purge cancelled.'); return
    result = run([sys.executable, str(ROOT / 'app' / 'archive.py'), 'purge', '--execute', '--confirm', phrase], timeout=600)
    say('Purge exit code: ' + str(result.returncode))


def show_logs(follow=False):
    paths = [ROOT / 'logs' / name for name in ('application.log', 'engine.log', 'mediamtx.log')]
    existing = [str(path) for path in paths if path.is_file()]
    if follow:
        tail = shutil.which('tail')
        if not tail or not existing:
            say('No local log files are available to follow.'); return
        proc = subprocess.Popen([tail, '-n', '80', '-F', *existing], cwd=ROOT)
        try: proc.wait()
        except KeyboardInterrupt:
            proc.terminate()
            try: proc.wait(timeout=2)
            except subprocess.TimeoutExpired: proc.kill()
    else:
        for path in paths:
            if path.is_file():
                say('\n--- ' + str(path.relative_to(ROOT)) + ' ---')
                try: say(path.read_text(errors='replace')[-12000:])
                except OSError as exc: say(str(exc))


def build_runtime():
    was_running = engine_proc is not None and engine_proc.poll() is None
    stop_engine()
    run([sys.executable, str(ROOT / 'BUILD_RUNTIME.py')], check=True, timeout=1800)
    if was_running: start_engine()


def handle(line: str):
    args = shlex_split(line)
    if not args: return True
    cmd = args[0].lower()
    try:
        if cmd in ('help', 'h', '?'): help_text()
        elif cmd == 'status': say(safe_status())
        elif cmd == 'gui': open_gui()
        elif cmd == 'logs': show_logs(len(args) > 1 and args[1] == 'follow')
        elif cmd == 'cameras': list_cameras()
        elif cmd == 'camera' and len(args) > 1 and args[1] == 'add': add_camera()
        elif cmd == 'config':
            if len(args) > 1 and args[1] == 'edit': editor_for(ROOT / 'USER_CONTROL.py')
            else: say((ROOT / 'USER_CONTROL.py').read_text(encoding='utf-8'))
        elif cmd == 'commands':
            if len(args) > 1 and args[1] == 'edit': editor_for(ROOT / 'control' / 'COMMANDS.json')
            else: say((ROOT / 'control' / 'COMMANDS.json').read_text(encoding='utf-8'))
        elif cmd == 'archive' and len(args) > 3 and args[1] == 'build':
            result = rpc_request('archive.run', {'task':'build','camera_id':args[2],'day':args[3]})
            say(result.get('message', 'Archive queued.'))
        elif cmd == 'purge' and len(args) > 1:
            if args[1] == 'preview': say(rpc_request('purge.preview', {}).get('report', 'No report.'))
            elif args[1] == 'execute': purge_execute()
            else: say('Usage: purge preview | purge execute')
        elif cmd == 'backup':
            mode = args[1] if len(args) > 1 else 'without-cache'
            if mode not in ('with-cache', 'without-cache'): say('Usage: backup [with-cache|without-cache]')
            else: create_backup(mode == 'with-cache')
        elif cmd == 'restore':
            if len(args) < 2: say('Usage: restore <backup.tar.gz>')
            else: restore_backup(args[1])
        elif cmd == 'stop': stop_engine()
        elif cmd == 'start': start_engine()
        elif cmd in ('rebuild', 'build-runtime'): build_runtime()
        elif cmd == 'restart': restart_everything()
        elif cmd == 'quit': say('Master terminal exiting. Owned GUI and engine processes will stop.'); return False
        else: say('Unknown command. Type help.')
    except KeyboardInterrupt: say('Operation interrupted; master terminal remains active.')
    except Exception as exc: say(f'ERROR: {type(exc).__name__}: {exc}')
    return True


def shutdown_project():
    global _SHUTDOWN_STARTED
    if _SHUTDOWN_STARTED: return
    _SHUTDOWN_STARTED = True
    say('\nSHUTTING DOWN eYeYaYe — GUI players, CAM3 bridge, FFmpeg and project MediaMTX...')
    for proc in list(GUI_PROCESSES):
        try:
            _terminate_process_group(proc.pid, graceful_seconds=8)
            try: proc.wait(timeout=0.5)
            except subprocess.TimeoutExpired: pass
        except Exception as exc: say(f'GUI shutdown warning (PID {proc.pid}): {exc}')
    GUI_PROCESSES.clear()
    stop_engine()
    say('eYeYaYe stopped. All processes owned by this project are closed.')


def _exit_signal(signum, _frame):
    raise SystemExit(128 + int(signum))


def main():
    global _SHUTDOWN_STARTED
    try: launch_mode = parse_launch_mode(sys.argv[1:])
    except ValueError as exc: say(str(exc)); return 2
    if launch_mode == 'help': help_text(); say('Startup options: -G/--gui (default) or -T/--terminal.'); return 0
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, _exit_signal)
    if hasattr(signal, 'SIGHUP'): signal.signal(signal.SIGHUP, _exit_signal)
    try:
        check_host(); ensure_layout(); check_runtime_bundle()
        start_master_control_server()
        say('Project-local MediaMTX: ' + str(ROOT / 'cam3' / 'CAM3_LIVE_STANDALONE' / 'bin' / 'mediamtx'))
        say('Project-local FFmpeg: ' + str(ROOT / 'cam3' / 'CAM3_LIVE_STANDALONE' / 'bin' / 'ffmpeg'))
        say('Starting the local engine as the current user.')
        start_engine()
        say('\neYeYaYe MASTER TERMINAL ACTIVE — this terminal owns the project lifecycle.')
        say('Closing the GUI keeps streams and recording running.')
        say('Closing this terminal or typing quit stops this project and its players.')
        if launch_mode == 'gui': open_gui()
        else: say('Terminal-only mode. Open the GUI by typing: gui')
        help_text(); last = 0.0
        while True:
            reap_gui_processes()
            try:
                requested = MASTER_COMMANDS.get_nowait()
            except queue.Empty:
                requested = None
            if requested == 'restart':
                try: restart_everything()
                except Exception as exc: say(f'RESTART EVERYTHING FAILED: {type(exc).__name__}: {exc}')
            now = time.monotonic()
            refresh = max(3, int(control_values().get('STATUS_REFRESH_SECONDS', 15)))
            if now - last >= refresh:
                say('[LIVE STATUS] ' + safe_status()); last = now
            ready, _, _ = select.select([sys.stdin], [], [], 2)
            if ready:
                line = sys.stdin.readline()
                if not line:
                    say('Terminal input closed. eYeYaYe will stop its owned processes.')
                    break
                if not handle(line): break
    except SystemExit:
        raise
    except KeyboardInterrupt:
        say('Terminal interrupted. eYeYaYe will stop its owned processes.')
    finally:
        shutdown_project()
        stop_master_control_server()


if __name__ == '__main__': main()
