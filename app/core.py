#!/usr/bin/env python3
"""CCTV Portable media engine core. No HTTP listener or browser interface."""
from __future__ import annotations
import ast, json, os, re, signal, stat, subprocess, threading, time, sys
from datetime import datetime, timezone
from urllib.parse import unquote
from pathlib import Path
from runtime_paths import resolve_media_tools

ROOT = Path(os.environ.get('CCTV_PROJECT_ROOT', '/project' if Path('/project').exists() else Path(__file__).resolve().parents[1]))
APP = ROOT / 'app'
CONFIG = ROOT / 'config'
DATA = ROOT / 'data'
RUNTIME = ROOT / 'runtime'
LOGS = ROOT / 'logs'
CAM3 = ROOT / 'cam3'
CAM3_BUNDLE = CAM3 / 'CAM3_LIVE_STANDALONE'
USER_CONTROL = ROOT / 'USER_CONTROL.py'
LAST_VALID_CONTROL = CONFIG / 'user_control_last_valid.json'
CAMERAS_FILE = CONFIG / 'cameras.json'
BOOKMARKS_FILE = CONFIG / 'bookmarks.json'
APPEARANCE_FILE = CONFIG / 'appearance.json'
COMMANDS_FILE = ROOT / 'control' / 'COMMANDS.json'
MTX_CONFIG = RUNTIME / 'mediamtx.yml'
MEDIA_LOG = LOGS / 'mediamtx.log'
BIN_DIR = CAM3_BUNDLE / 'bin'
MEDIAMTX_BIN = BIN_DIR / 'mediamtx'
BUNDLED_FFMPEG = BIN_DIR / 'ffmpeg'
BUNDLED_FFPROBE = BIN_DIR / 'ffprobe'
LIVE_DIR = RUNTIME / 'live'

lock = threading.RLock()
media_proc = None
media_log_handle = None
bridge_procs: dict[str, subprocess.Popen] = {}
bridge_logs: dict[str, object] = {}
audio_mux_procs: dict[str, subprocess.Popen] = {}
audio_mux_logs: dict[str, object] = {}
recording_procs: dict[str, subprocess.Popen] = {}
recording_logs: dict[str, object] = {}
recording_retry_after: dict[str, float] = {}
recording_lock = threading.RLock()
last_error = ''
control_error = ''
control_cache = None
config_write_lock = threading.RLock()
archive_job = {'running': False, 'message': 'No archive job has been run.'}
archive_job_lock = threading.Lock()

APPEARANCE_DEFAULTS = {
    'schema_version': 1, 'preset': 'cyberpunk',
    'colors': {
        'background':'#070914','surface':'#0e1426','surface_raised':'#141d35',
        'sidebar':'#090f1e','header':'#0b1020','text':'#eaf4ff','muted':'#91a3bd',
        'border':'#273653','accent':'#00e5ff','accent_alt':'#ff35d3',
        'success':'#00efa3','warning':'#ffbd59','danger':'#ff4f86',
        'button_bg':'#111d34','button_text':'#eafaff','input_bg':'#091222',
        'recorded':'#557b9e','protected':'#00c995','event':'#ffad47',
        'video_border':'#24496b','timeline_bg':'#080f1d',
        'button_hover':'#1b2f4b','button_primary_bg':'#12364a','button_primary_text':'#efffff',
        'nav_active_bg':'#10253a','nav_active_text':'#f5fdff','focus_ring':'#00e5ff',
        'badge_bg':'#101a2b','timeline_empty':'#172235','mask_fill':'#00e5ff',
        'mask_outline':'#00ffe0','overlay_bg':'#050914',
        'camera_tile_bg':'#050a12',
        'camera_offline_bg':'#0a1422',
        'camera_offline_text':'#8798b0',
        'toast_bg':'#141d35',
        'toast_border':'#00e5ff',
        'warning_bg':'#2a2115',
        'warning_text':'#f7d3a1',
        'warning_border':'#ffbd59',
        'help_bg':'#081321',
        'snapshot_bg':'#050a12',
        'status_online':'#00efa3',
        'status_pending':'#ffbd59',
        'status_offline':'#ff4f86',
        'timeline_recorded_hover':'#e8effa',
        'nav_hover_bg':'#1b2f4b',
        'video_player_bg':'#000000',
        'admin_frame_bg':'#0e1426',
        'scrollbar_track':'#080f1d',
        'scrollbar_thumb':'#2b405f',
        'selection_bg':'#00e5ff',
        'selection_text':'#070914',
        'disabled_bg':'#131c2d',
        'disabled_text':'#697b95',
        'disabled_border':'#293a52',
        'danger_bg':'#381421'
    },
    'effects': {
        'neon_buttons':True,'button_glow':0.58,'panel_glow':0.16,
        'corner_radius':12,'font_size':14,'font_family':'system',
        'density':'comfortable','sidebar_width':190,'button_style':'neon',
        'transitions':True,'panel_borders':True,'grid_lines':True
    }
}
APPEARANCE_COLOR_KEYS = tuple(APPEARANCE_DEFAULTS['colors'])
APPEARANCE_EFFECT_KEYS = tuple(APPEARANCE_DEFAULTS['effects'])

def validate_appearance(payload: dict) -> dict:
    """Strictly validate user-editable visual tokens before saving them."""
    if not isinstance(payload, dict):
        raise ValueError('Appearance settings must be a JSON object.')
    colors = payload.get('colors', {})
    effects = payload.get('effects', {})
    if not isinstance(colors, dict) or not isinstance(effects, dict):
        raise ValueError('Appearance colors and effects must be objects.')
    clean = json.loads(json.dumps(APPEARANCE_DEFAULTS))
    preset = str(payload.get('preset', 'custom'))[:32]
    clean['preset'] = preset
    for key, value in colors.items():
        if key not in APPEARANCE_COLOR_KEYS:
            raise ValueError(f'Unknown appearance color: {key}')
        if not isinstance(value, str) or not re.fullmatch(r'#[0-9a-fA-F]{6}', value):
            raise ValueError(f'{key} must be a six-digit hexadecimal color such as #00E5FF.')
        clean['colors'][key] = value.lower()
    numeric_ranges = {'button_glow':(0.0,1.0),'panel_glow':(0.0,1.0),
                      'corner_radius':(0,24),'font_size':(11,20),'sidebar_width':(150,320)}
    for key, value in effects.items():
        if key not in APPEARANCE_EFFECT_KEYS:
            raise ValueError(f'Unknown appearance effect: {key}')
        if key in numeric_ranges:
            lo, hi = numeric_ranges[key]
            if isinstance(value, bool) or not isinstance(value, (int,float)) or not lo <= value <= hi:
                raise ValueError(f'{key} must be between {lo} and {hi}.')
            clean['effects'][key] = value
        elif key in ('neon_buttons','transitions','panel_borders','grid_lines'):
            if not isinstance(value, bool): raise ValueError(f'{key} must be true or false.')
            clean['effects'][key] = value
        elif key == 'font_family':
            if value not in ('system','humanist','mono','serif'): raise ValueError('Unsupported font family.')
            clean['effects'][key] = value
        elif key == 'density':
            if value not in ('compact','comfortable','spacious'): raise ValueError('Unsupported density.')
            clean['effects'][key] = value
        elif key == 'button_style':
            if value not in ('neon','solid','outline'): raise ValueError('Unsupported button style.')
            clean['effects'][key] = value
    return clean


def log(message: str) -> None:
    stamp = datetime.now().astimezone().isoformat(timespec='seconds')
    line = f'[{stamp}] {message}'
    print(line, flush=True)
    LOGS.mkdir(parents=True, exist_ok=True)
    with (LOGS / 'application.log').open('a', encoding='utf-8') as f:
        f.write(line + '\n')


def read_json(path: Path, default):
    try:
        with path.open(encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def atomic_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
    mode = int(load_control().get('CAMERA_CREDENTIAL_FILE_MODE', 0o600)) if USER_CONTROL.exists() else 0o600
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def configured_command(name: str, values: dict) -> list[str]:
    """Expand an editable command array; commands are never run through a shell."""
    catalog = read_json(COMMANDS_FILE, {})
    template = catalog.get(name)
    if not isinstance(template, list) or not template or not all(isinstance(x, str) for x in template):
        raise RuntimeError(f"Command template {name!r} is missing or malformed in control/COMMANDS.json.")
    try:
        return [item.format(**values) for item in template]
    except KeyError as exc:
        raise RuntimeError(f"Unknown placeholder {exc} in control/COMMANDS.json entry {name!r}.") from exc


def ensure_layout() -> None:
    for p in (CONFIG, DATA / 'recordings' / 'original', DATA / 'recordings' / 'archives',
              DATA / 'recordings' / 'timelapse', DATA / 'events', DATA / 'thumbnails',
              RUNTIME, LIVE_DIR, LOGS, ROOT / 'backups', ROOT / 'cache'):
        p.mkdir(parents=True, exist_ok=True)
    if not CAMERAS_FILE.exists():
        atomic_json(CAMERAS_FILE, {'schema_version': 1, 'cameras': []})
    migrate_camera_credentials_into_urls()
    if not BOOKMARKS_FILE.exists():
        atomic_json(BOOKMARKS_FILE, {'schema_version': 1, 'bookmarks': []})
    if not APPEARANCE_FILE.exists():
        APPEARANCE_FILE.write_text(json.dumps(APPEARANCE_DEFAULTS, indent=2) + '\n', encoding='utf-8')
        os.chmod(APPEARANCE_FILE, 0o600)
    # These files can contain camera credentials or editable command arguments.
    for private_file in (CAMERAS_FILE, BOOKMARKS_FILE, COMMANDS_FILE, USER_CONTROL, LAST_VALID_CONTROL):
        if private_file.exists():
            os.chmod(private_file, 0o600)
    # Snapshot a valid settings parse so a typo in a later manual edit does not
    # take the recorder offline; the master terminal reports the parse error.
    values = load_control()
    atomic_json(LAST_VALID_CONTROL, values)


def load_control() -> dict:
    """Read literal assignments only; fall back to last valid values on syntax errors."""
    global control_error, control_cache
    try:
        source = USER_CONTROL.read_text(encoding='utf-8')
        tree = ast.parse(source, filename=str(USER_CONTROL))
        values = {}
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                value_node = node.value
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                try:
                    value = ast.literal_eval(value_node)
                except Exception:
                    continue
                for target in targets:
                    if isinstance(target, ast.Name):
                        values[target.id] = value
        if not values:
            raise ValueError('No literal settings were found in USER_CONTROL.py.')
        values = resolve_media_tools(values)
        control_cache = values
        control_error = ''
        return values
    except Exception as exc:
        control_error = f'{type(exc).__name__}: {exc}'
        if control_cache is not None:
            control_cache = resolve_media_tools(control_cache)
            return control_cache
        if LAST_VALID_CONTROL.is_file():
            try:
                control_cache = resolve_media_tools(json.loads(LAST_VALID_CONTROL.read_text(encoding='utf-8')))
                return control_cache
            except Exception:
                pass
        raise


def cameras() -> list[dict]:
    doc = read_json(CAMERAS_FILE, {'cameras': []})
    return doc.get('cameras', [])



def migrate_camera_credentials_into_urls() -> None:
    """Upgrade legacy split RTSP credentials to the full URL format, in place."""
    from urllib.parse import urlsplit, urlunsplit, quote as urlquote
    doc = read_json(CAMERAS_FILE, {'schema_version': 1, 'cameras': []})
    changed = False
    for cam in doc.get('cameras', []):
        if str(cam.get('type', 'rtsp')).lower() != 'rtsp':
            continue
        raw = str(cam.get('rtsp_url', '')).strip()
        if not raw.lower().startswith(('rtsp://', 'rtsps://')):
            continue
        parts = urlsplit(raw)
        if parts.username or parts.password or not cam.get('username'):
            continue
        auth = urlquote(str(cam.get('username', '')), safe='') + ':' + urlquote(str(cam.get('password', '')), safe='') + '@'
        cam['rtsp_url'] = urlunsplit((parts.scheme, auth + parts.netloc, parts.path, parts.query, parts.fragment))
        changed = True
    if changed:
        atomic_json(CAMERAS_FILE, doc)


def bookmarks() -> list[dict]:
    return read_json(BOOKMARKS_FILE, {'bookmarks': []}).get('bookmarks', [])


def safe_id(value: str) -> str:
    value = re.sub(r'[^a-zA-Z0-9_-]+', '', str(value).strip()).lower()
    if not value or len(value) > 32:
        raise ValueError('Camera ID must contain 1-32 letters, digits, underscores or hyphens.')
    return value


def yaml_quote(value: str) -> str:
    return json.dumps(str(value), ensure_ascii=False)


def camera_source(camera: dict) -> str:
    from urllib.parse import urlsplit, urlunsplit, quote as urlquote
    raw = str(camera.get('rtsp_url', '')).strip()
    if not raw.lower().startswith(('rtsp://', 'rtsps://')):
        raise ValueError('RTSP URL must start with rtsp:// or rtsps://.')
    parts = urlsplit(raw)
    # Credentials may be embedded directly in the RTSP URL (the user's preferred
    # format). Only inject legacy split fields when the URL has no credentials.
    if not (parts.username or parts.password) and camera.get('username'):
        auth = urlquote(str(camera['username']), safe='') + ':' + urlquote(str(camera.get('password', '')), safe='') + '@'
        raw = urlunsplit((parts.scheme, auth + parts.netloc, parts.path, parts.query, parts.fragment))
    return raw


def camera_filename_code(camera_id: str) -> str:
    match = re.fullmatch(r"cam(\d+)", str(camera_id).lower())
    return f"C{match.group(1)}" if match else "C" + re.sub(r"[^A-Z0-9]", "", str(camera_id).upper())[:12]


def external_audio_source(camera: dict) -> str:
    """Build an optional separate RTSP audio source from URL + separate credentials."""
    raw = str(camera.get('audio_source_url', '')).strip()
    if not raw:
        return ''
    from urllib.parse import urlsplit, urlunsplit, quote as urlquote
    if not raw.lower().startswith(('rtsp://', 'rtsps://')):
        raise ValueError('External audio source URL must start with rtsp:// or rtsps://.')
    parts = urlsplit(raw)
    if parts.username or parts.password:
        raise ValueError('Keep external audio credentials in their separate fields, not inside the URL.')
    user = str(camera.get('audio_source_username', ''))
    password = str(camera.get('audio_source_password', ''))
    if user:
        raw = urlunsplit((parts.scheme, urlquote(user, safe='') + ':' + urlquote(password, safe='') + '@' + parts.netloc,
                          parts.path, parts.query, parts.fragment))
    return raw


def media_config_text() -> str:
    """Build only the RTSP routing configuration; FFmpeg owns all RAW recording.

    MediaMTX remains the existing camera router and CAM3 publisher target. Its
    built-in recorder is deliberately disabled so there is exactly one recording
    implementation: the per-camera FFmpeg tee process.
    """
    lines = [
        'logLevel: info', 'logDestinations: [stdout]',
        'rtsp: true', 'rtspAddress: :8554', 'rtspTransports: [tcp]',
        'rtmp: false', 'hls: false', 'webrtc: false', 'srt: false',
        'api: false', 'metrics: false', 'pprof: false',
        'pathDefaults:', '  sourceOnDemand: false', '  record: false',
    ]
    active = [c for c in cameras() if c.get('enabled', True)]
    if not active:
        lines.append('paths: {}')
        return '\n'.join(lines) + '\n'
    lines.append('paths:')
    for cam in active:
        cid = safe_id(cam.get('id', ''))
        audio = external_audio_source(cam)
        source_path = cid + '_source'
        if audio:
            lines.append(f'  {source_path}:')
            if cam.get('type') == 'cam3':
                lines.append('    source: publisher')
            else:
                lines.append('    source: ' + yaml_quote(camera_source(cam)))
                lines.append('    rtspTransport: ' + str(load_control().get('CAMERA_DEFAULT_RTSP_TRANSPORT', 'tcp')))
            lines += ['    sourceOnDemand: false', '    record: false']
            lines.append(f'  {cid}:')
            lines += ['    source: publisher', '    sourceOnDemand: false', '    record: false']
        else:
            lines.append(f'  {cid}:')
            if cam.get('type') == 'cam3':
                lines.append('    source: publisher')
            else:
                lines.append('    source: ' + yaml_quote(camera_source(cam)))
                lines.append('    rtspTransport: ' + str(load_control().get('CAMERA_DEFAULT_RTSP_TRANSPORT', 'tcp')))
            lines += ['    sourceOnDemand: false', '    record: false']
    return '\n'.join(lines) + '\n'


def write_media_config() -> None:
    with config_write_lock:
        _write_media_config_locked()

def _write_media_config_locked() -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    text = media_config_text()
    tmp = MTX_CONFIG.with_suffix('.yml.tmp')
    tmp.write_text(text, encoding='utf-8')
    os.chmod(tmp, 0o600)
    if MEDIAMTX_BIN.is_file():
        cmd=[str(MEDIAMTX_BIN), f'--validate-conf={tmp}']
        log('EXEC ' + redact_command(cmd))
        check=subprocess.run(cmd,capture_output=True,text=True,timeout=12)
        if check.returncode:
            tmp.unlink(missing_ok=True)
            raise RuntimeError('MediaMTX rejected the proposed configuration: '+(check.stderr or check.stdout)[-1800:])
    os.replace(tmp, MTX_CONFIG)
    try:
        values=load_control()
        snapshot=LAST_VALID_CONTROL.with_suffix('.json.tmp')
        snapshot.write_text(json.dumps(values,indent=2)+'\n',encoding='utf-8');os.chmod(snapshot,0o600);os.replace(snapshot,LAST_VALID_CONTROL)
    except Exception as exc:
        log(f'WARNING: unable to snapshot last valid user settings: {exc}')
    log(f'Wrote validated MediaMTX configuration: {MTX_CONFIG} (camera URLs are not printed).')


def redact_command(args: list[str]) -> str:
    redacted = []
    for arg in args:
        if isinstance(arg, str) and ('@' in arg and arg.startswith(('rtsp://', 'rtsps://'))):
            from urllib.parse import urlsplit, urlunsplit
            p = urlsplit(arg)
            if p.username or p.password:
                netloc = '***:***@' + (p.hostname or '') + (f':{p.port}' if p.port else '')
                arg = urlunsplit((p.scheme, netloc, p.path, p.query, p.fragment))
        redacted.append(arg)
    return ' '.join(subprocess.list2cmdline([x]) for x in redacted)


def stop_process(proc, label: str) -> None:
    if proc is None or proc.poll() is not None:
        return
    log(f'Stopping {label} PID={proc.pid}.')
    proc.terminate()
    try:
        proc.wait(timeout=8)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def start_mediamtx() -> None:
    global media_proc, media_log_handle
    if not MEDIAMTX_BIN.is_file():
        raise RuntimeError('Project-contained MediaMTX is missing: ' + str(MEDIAMTX_BIN) + '. The launcher builds this bundle before starting the engine.')
    write_media_config()
    validate_cmd=[str(MEDIAMTX_BIN), f'--validate-conf={MTX_CONFIG}']
    log('EXEC '+redact_command(validate_cmd))
    validate = subprocess.run(validate_cmd,
                              capture_output=True, text=True, timeout=10)
    if validate.returncode:
        raise RuntimeError('MediaMTX configuration validation failed: ' + (validate.stderr or validate.stdout)[-1200:])
    log('MediaMTX configuration validated.')
    if media_proc is not None and media_proc.poll() is None:
        return
    media_log_handle = MEDIA_LOG.open('a', encoding='utf-8', buffering=1)
    cmd = [str(MEDIAMTX_BIN), str(MTX_CONFIG)]
    log('EXEC ' + redact_command(cmd))
    media_proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=media_log_handle, stderr=subprocess.STDOUT, start_new_session=True)


def start_cam3_bridges() -> None:
    """Start the preserved CAM3 bridge from the complete project-contained bundle.

    The engine starts the single shared MediaMTX from this same bundle. The
    bridge publishes to 127.0.0.1:8554 and never starts a second server.
    """
    global bridge_procs, bridge_logs
    bundle = CAM3_BUNDLE
    bridge = bundle / 'rtmp_live_bridge.py'
    client = bundle / 'rtmp_cam.py'
    ffmpeg = bundle / 'bin' / 'ffmpeg'
    for cam in cameras():
        if not cam.get('enabled', True) or cam.get('type') != 'cam3':
            continue
        cid = safe_id(cam['id'])
        if cid in bridge_procs and bridge_procs[cid].poll() is None:
            continue
        if not (bridge.is_file() and client.is_file() and ffmpeg.is_file()):
            log('CAM3 is enabled but its project-contained bridge/client/FFmpeg bundle is incomplete: ' + str(bundle) + '. Run python3 BUILD_RUNTIME.py from the project root.')
            continue
        log_path = LOGS / f'{cid}-bridge.log'
        fh = log_path.open('a', encoding='utf-8', buffering=1)
        env = os.environ.copy()
        env.update({'CAM3_CLIENT_PATH': str(client), 'CAM3_LOG_DIR': str(bundle / 'logs'),
                    'CAM3_COMMANDS_PATH': str(ROOT / 'control' / 'COMMANDS.json'),
                    'CAM3_HOST': str(cam.get('rtmp_host', '192.168.10.104')),
                    'CAM3_PORT': str(cam.get('rtmp_port', 80)),
                    'CAM3_USERNAME': str(cam.get('rtmp_username', '')),
                    'CAM3_PASSWORD': str(cam.get('rtmp_password', '')),
                    'CAM3_STREAM_NAME': str(cam.get('rtmp_stream_name', '720p.264')),
                    'CAM3_RTSP_HOST': '127.0.0.1', 'CAM3_RTSP_PORT': '8554',
                    'CAM3_RTSP_PATH': (cid + '_source') if external_audio_source(cam) else cid, 'CAM3_FFMPEG_PATH': str(ffmpeg),
                    'PATH': str(ffmpeg.parent) + os.pathsep + os.environ.get('PATH', ''),
                    'LD_LIBRARY_PATH': str(bundle / 'lib') + os.pathsep + os.environ.get('LD_LIBRARY_PATH', ''),
                    'PYTHONUNBUFFERED': '1'})
        cmd = [sys.executable, '-u', str(bridge)]
        log('EXEC ' + redact_command(cmd) + ' [CAM3 standalone bridge; credentials redacted]')
        bridge_procs[cid] = subprocess.Popen(cmd, cwd=str(bundle), env=env,
                                             stdout=fh, stderr=subprocess.STDOUT)
        bridge_logs[cid] = fh


def start_audio_muxers() -> None:
    """Mux an optional external RTSP audio-only source onto a camera video path.

    Video is stream-copied. Audio is encoded to AAC for predictable RTSP compatibility.
    The final camera path is the only path recorded by MediaMTX.
    """
    global audio_mux_procs, audio_mux_logs
    ctl = load_control()
    ffmpeg = str(ctl.get('FFMPEG_BINARY', str(BUNDLED_FFMPEG)))
    for cam in cameras():
        if not cam.get('enabled', True):
            continue
        cid = safe_id(cam.get('id', ''))
        try:
            audio_url = external_audio_source(cam)
        except ValueError as exc:
            log(f'{cid} external audio configuration error: {exc}')
            continue
        if not audio_url:
            continue
        current = audio_mux_procs.get(cid)
        if current is not None and current.poll() is None:
            continue
        video_url = f'rtsp://127.0.0.1:8554/{cid}_source'
        output_url = f'rtsp://127.0.0.1:8554/{cid}'
        command = configured_command('audio_mux_publish', {
            'ffmpeg': ffmpeg, 'loglevel': ctl.get('FFMPEG_LOGLEVEL', 'warning'),
            'video_url': video_url, 'audio_url': audio_url, 'output_url': output_url,
            'audio_codec': ctl.get('EXTERNAL_AUDIO_CODEC', 'aac'),
            'audio_bitrate': ctl.get('EXTERNAL_AUDIO_BITRATE', '96k')})
        log_path = LOGS / f'{cid}-audio-mux.log'
        fh = log_path.open('a', encoding='utf-8', buffering=1)
        log('EXEC ' + redact_command(command) + f' [external audio mux: {cid}]')
        audio_mux_procs[cid] = subprocess.Popen(command, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=fh, cwd=str(ROOT), start_new_session=True)
        audio_mux_logs[cid] = fh


def stop_audio_muxers() -> None:
    for cid, proc in list(audio_mux_procs.items()):
        stop_process(proc, f'{cid} external audio mux')
        audio_mux_procs.pop(cid, None)
        fh = audio_mux_logs.pop(cid, None)
        if fh:
            try: fh.close()
            except Exception: pass


def live_pipe_path(camera_id: str) -> Path:
    return LIVE_DIR / (safe_id(camera_id) + '.mkvpipe')


def ensure_live_pipe(camera_id: str) -> Path:
    LIVE_DIR.mkdir(parents=True, exist_ok=True)
    path = live_pipe_path(camera_id)
    if path.exists():
        if not stat.S_ISFIFO(path.stat().st_mode):
            raise RuntimeError(f'Refusing to replace a non-FIFO live pipe: {path}')
    else:
        os.mkfifo(path, 0o660)
    os.chmod(path, 0o660)
    return path


def _tee_escape(value: str) -> str:
    """Escape path characters interpreted by FFmpeg's tee slave parser."""
    text = str(value)
    for char in ('\\', ':', "'", '[', ']', '|'):
        text = text.replace(char, '\\' + char)
    return text


def build_recording_command(camera_id: str, input_url: str | None = None,
                            output_dir: Path | None = None, pipe_path: Path | None = None,
                            control: dict | None = None, realtime: bool = False,
                            stream_loop: bool = False) -> list[str]:
    """Build the user's FFmpeg segment-recorder plus one-way Matroska viewer branch.

    Both outputs receive the same already-encoded packets. The recording branch
    writes MKV segments; the viewer branch is isolated behind the FIFO muxer so
    a stalled/absent MPV reader drops only preview packets, never recording data.
    """
    cid = safe_id(camera_id)
    ctl = dict(control or load_control())
    segment = int(ctl.get('RECORDING_SEGMENT_SECONDS', 600))
    if not 60 <= segment <= 3600:
        raise ValueError('RECORDING_SEGMENT_SECONDS must be between 60 and 3600.')
    recording_format = str(ctl.get('RECORDING_FORMAT', 'mkv')).lower().lstrip('.')
    if recording_format != 'mkv':
        raise ValueError('V8 original recordings use MKV only; RECORDING_FORMAT must be mkv.')
    source = str(input_url or f'rtsp://127.0.0.1:8554/{cid}')
    outdir = Path(output_dir or (DATA / 'recordings' / 'original' / cid))
    pipe = Path(pipe_path or ensure_live_pipe(cid))
    outdir.mkdir(parents=True, exist_ok=True)
    code = camera_filename_code(cid)
    pattern = outdir / f'%H%M%S_%d%m%Y_RAW_{code}.mkv'
    queue_size = max(16, min(8192, int(ctl.get('LIVE_PIPE_QUEUE_PACKETS', 512))))
    segment_slave = (
        '[f=segment:segment_time=' + str(segment) +
        ':segment_format=matroska:strftime=1:reset_timestamps=1:segment_atclocktime=1]' +
        _tee_escape(str(pattern))
    )
    fifo_slave = (
        '[f=fifo:fifo_format=matroska:queue_size=' + str(queue_size) +
        ':drop_pkts_on_overflow=1:attempt_recovery=1:recovery_wait_time=1' +
        ':max_recovery_attempts=0:restart_with_keyframe=1:onfail=ignore]' +
        _tee_escape(str(pipe))
    )
    tee_output = segment_slave + '|' + fifo_slave
    ffmpeg = str(ctl.get('FFMPEG_BINARY', str(BUNDLED_FFMPEG)))
    command = [ffmpeg, '-nostdin', '-hide_banner', '-y', '-loglevel', str(ctl.get('FFMPEG_LOGLEVEL', 'warning'))]
    if realtime:
        command.append('-re')
    if stream_loop:
        command += ['-stream_loop', '-1']
    if source.lower().startswith(('rtsp://', 'rtsps://')):
        command += ['-rtsp_transport', str(ctl.get('CAMERA_DEFAULT_RTSP_TRANSPORT', 'tcp')),
                    '-timeout', str(int(ctl.get('RECORDING_INPUT_TIMEOUT_US', 60000000)))]
    command += [
        '-i', source,
        '-map', '0:v:0', '-map', '0:a?',
        '-fps_mode:v', 'passthrough',
        '-c:v', 'copy', '-c:a', 'copy', '-copyinkf',
        '-f', 'tee', tee_output,
    ]
    return command


def _close_recording_log(camera_id: str) -> None:
    handle = recording_logs.pop(camera_id, None)
    if handle:
        try:
            handle.close()
        except Exception:
            pass


def stop_recorder(camera_id: str, graceful: bool = True) -> None:
    """Stop one recorder; drain its FIFO only if FFmpeg cannot exit by itself."""
    cid = safe_id(camera_id)
    proc = recording_procs.pop(cid, None)
    recording_retry_after.pop(cid, None)
    if proc is None:
        _close_recording_log(cid)
        return
    if proc.poll() is None:
        log(f'Stopping {cid} FFmpeg recorder PID={proc.pid}.')
        if graceful:
            try:
                proc.send_signal(signal.SIGINT)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                # A named FIFO writer waits for an MPV reader. On shutdown the
                # GUI has already stopped; attach a temporary drain reader so
                # FFmpeg can flush/finalize its current MKV segment cleanly.
                fifo = live_pipe_path(cid)
                fd = None
                try:
                    if fifo.exists() and stat.S_ISFIFO(fifo.stat().st_mode):
                        fd = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
                    deadline = time.monotonic() + 5.0
                    while proc.poll() is None and time.monotonic() < deadline:
                        if fd is not None:
                            try:
                                os.read(fd, 65536)
                            except BlockingIOError:
                                pass
                            except OSError:
                                break
                        time.sleep(0.02)
                finally:
                    if fd is not None:
                        os.close(fd)
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2)
        else:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
    _close_recording_log(cid)


def stop_recorders(graceful: bool = True) -> None:
    for cid in list(recording_procs):
        stop_recorder(cid, graceful=graceful)


def start_recorders() -> None:
    """Maintain one FFmpeg tee recorder per enabled camera."""
    ctl = load_control()
    active_ids = set()
    for cam in cameras():
        cid = safe_id(cam.get('id', ''))
        if not cam.get('enabled', True):
            continue
        active_ids.add(cid)
    for cid in list(recording_procs):
        proc = recording_procs[cid]
        if cid not in active_ids or not ctl.get('ENABLE_ORIGINAL_RECORDING', True):
            stop_recorder(cid, graceful=True)
            continue
        if proc.poll() is not None:
            code = proc.returncode
            log(f'{cid} FFmpeg recorder exited (rc={code}); retry scheduled.')
            recording_procs.pop(cid, None)
            _close_recording_log(cid)
            recording_retry_after[cid] = time.monotonic() + max(2, int(ctl.get('RECORDING_RESTART_DELAY_SECONDS', 5)))
    if not ctl.get('ENABLE_ORIGINAL_RECORDING', True):
        return
    for cam in cameras():
        if not cam.get('enabled', True):
            continue
        cid = safe_id(cam.get('id', ''))
        current = recording_procs.get(cid)
        if current is not None and current.poll() is None:
            continue
        if time.monotonic() < recording_retry_after.get(cid, 0.0):
            continue
        try:
            fifo = ensure_live_pipe(cid)
            command = build_recording_command(cid, pipe_path=fifo, control=ctl)
            log('EXEC ' + redact_command(command) + f' [RAW recorder + one-way Matroska live pipe: {cid}]')
            log_path = LOGS / f'{cid}-recorder.log'
            handle = log_path.open('a', encoding='utf-8', buffering=1)
            proc = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=handle, cwd=str(ROOT), start_new_session=True)
            recording_procs[cid] = proc
            recording_logs[cid] = handle
            log(f'{cid} FFmpeg recorder started PID={proc.pid}; source is MediaMTX localhost path; video/audio are stream-copied.')
            recording_retry_after.pop(cid, None)
        except Exception as exc:
            recording_retry_after[cid] = time.monotonic() + max(5, int(ctl.get('RECORDING_RESTART_DELAY_SECONDS', 5)))
            log(f'{cid} FFmpeg recorder start failed: {type(exc).__name__}: {exc}')


def recording_state(camera_id: str) -> dict:
    cid = safe_id(camera_id)
    proc = recording_procs.get(cid)
    if proc is not None and proc.poll() is None:
        return {'state': 'RECORDING', 'pid': proc.pid, 'live_pipe': str(live_pipe_path(cid).relative_to(ROOT))}
    retry = recording_retry_after.get(cid, 0.0)
    return {'state': 'RETRYING' if retry > time.monotonic() else 'WAITING',
            'pid': None, 'live_pipe': str(live_pipe_path(cid).relative_to(ROOT))}


def restart_media_stack() -> None:
    """Apply camera/path changes by restarting MediaMTX and bundled CAM3 publisher."""
    global media_proc
    with lock:
        stop_recorders(graceful=True)
        stop_audio_muxers()
        for cid, proc in list(bridge_procs.items()):
            stop_process(proc, f'{cid} bridge')
            fh = bridge_logs.pop(cid, None)
            if fh:
                try: fh.close()
                except Exception: pass
            bridge_procs.pop(cid, None)
        stop_process(media_proc, 'MediaMTX configuration reload')
        media_proc = None
        start_mediamtx()
        time.sleep(1.5)
        start_cam3_bridges()
        time.sleep(1.0)
        start_audio_muxers()


def supervisor() -> None:
    """Keep project media services alive, but back off when validation fails.

    A rejected MediaMTX configuration must not become a ten-second log storm.
    A changed configuration is retried promptly; an unchanged failure backs
    off to one minute while remaining visible through state.get and the logs.
    """
    global last_error
    retry_delay = 5
    last_failure = None
    next_attempt = 0.0
    while True:
        try:
            if media_proc is None or media_proc.poll() is not None:
                if time.monotonic() < next_attempt:
                    time.sleep(min(1.0, next_attempt - time.monotonic()))
                    continue
                log('MediaMTX is not running; attempting validated restart.')
                start_mediamtx()
                time.sleep(1.5)
                if media_proc is None or media_proc.poll() is not None:
                    raise RuntimeError('MediaMTX exited immediately after startup; inspect logs/mediamtx.log.')
            start_cam3_bridges()
            start_audio_muxers()
            start_recorders()
            last_error = ''
            last_failure = None
            retry_delay = 5
            next_attempt = time.monotonic() + 10
            time.sleep(10)
        except Exception as exc:
            last_error = f'{type(exc).__name__}: {exc}'
            signature = last_error
            if signature != last_failure:
                log('SUPERVISOR ERROR: ' + last_error)
                last_failure = signature
            next_attempt = time.monotonic() + retry_delay
            retry_delay = min(60, retry_delay * 2)
            time.sleep(1)


def archive_scheduler() -> None:
    """Build closed daily archive days after their configured original-retention delay.

    This task never purges source recordings. Purging is a separate, opt-in,
    manually confirmed operation with independent safety checks.
    """
    last_scan = 0.0
    while True:
        ctl = load_control()
        interval = max(60, int(ctl.get('ARCHIVE_AUTO_SCAN_SECONDS', 1800)))
        if not ctl.get('ARCHIVE_AUTO_BUILD', True) or not ctl.get('ENABLE_DAILY_ARCHIVE', True):
            time.sleep(min(interval, 300))
            continue
        if time.monotonic() - last_scan < interval:
            time.sleep(30)
            continue
        last_scan = time.monotonic()
        try:
            import sys as _sys
            if str(APP) not in _sys.path:
                _sys.path.insert(0, str(APP))
            from archive import archive_window, media_start, zone_for
            tz = zone_for(ctl)
            now = datetime.now(tz)
            delay = max(0, int(ctl.get('ARCHIVE_AUTO_BUILD_DELAY_DAYS', 2)))
            latest_eligible_end = now - __import__('datetime').timedelta(days=delay)
            hh, mm = map(int, str(ctl.get('ARCHIVE_DAY_START', '05:00')).split(':'))
            boundary_seconds = hh * 3600 + mm * 60
            job_done = False
            for cam in cameras():
                cid = safe_id(cam.get('id', ''))
                source_dir = DATA / 'recordings' / 'original' / cid
                if not source_dir.is_dir():
                    continue
                dates = []
                for media in source_dir.rglob('*'):
                    if not media.is_file() or media.suffix.lower() not in ('.ts', '.mp4', '.mkv'):
                        continue
                    try:
                        ts = media_start(media, tz)
                        day = ts.date()
                        if ts.hour * 3600 + ts.minute * 60 + ts.second < boundary_seconds:
                            day = day - __import__('datetime').timedelta(days=1)
                        dates.append(day)
                    except Exception:
                        continue
                if not dates:
                    continue
                first_day = min(dates)
                cursor = first_day
                while cursor <= now.date():
                    day_text = cursor.isoformat()
                    day_start, day_end = archive_window(day_text, str(ctl.get('ARCHIVE_DAY_START', '05:00')), tz)
                    manifest = DATA / 'recordings' / 'archives' / cid / day_text / f'{cid}_{day_text}.manifest.json'
                    if day_end <= latest_eligible_end and not manifest.is_file():
                        with archive_job_lock:
                            if archive_job.get('running'):
                                break
                            archive_job.update({'running': True, 'message': f'Automatic archive {cid} / {day_text}.'})
                        try:
                            cmd = ['/usr/bin/python3', str(APP / 'archive.py'), 'build', '--camera', cid, '--day', day_text]
                            log('EXEC ' + redact_command(cmd) + ' [automatic daily archive]')
                            result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True, text=True, timeout=24 * 3600)
                            combined = (result.stdout + '\n' + result.stderr)[-8000:]
                            for line in combined.splitlines():
                                log('AUTO-ARCHIVE ' + line[:2000])
                            archive_job.update({'running': False, 'message': combined, 'returncode': result.returncode})
                        except Exception as exc:
                            archive_job.update({'running': False, 'message': str(exc)})
                        job_done = True
                        break  # One camera/day job per scheduler pass.

                    # Build each enabled daily time-lapse only after the same
                    # archive day has been closed and its MER manifest exists.
                    if (day_end <= latest_eligible_end and manifest.is_file()
                            and ctl.get('TIMELAPSE_AUTO_BUILD', False)):
                        modes = []
                        if ctl.get('ENABLE_REGULAR_TIMELAPSE', False):
                            modes.append('regular')
                        if ctl.get('ENABLE_INTELLIGENT_TIMELAPSE', False):
                            modes.append('intelligent')
                        tl_dir = DATA / 'recordings' / 'timelapse' / cid
                        for mode in modes:
                            kind = 'TLR' if mode == 'regular' else 'TLI'
                            already_built = False
                            if tl_dir.is_dir():
                                for output in tl_dir.glob('*.mp4'):
                                    try:
                                        parsed = media_start(output, tz)
                                        stem = output.stem
                                        if (start <= parsed < day_end
                                                and f'_{kind}_' in stem):
                                            already_built = True
                                            break
                                    except Exception:
                                        continue
                            if already_built:
                                continue
                            with archive_job_lock:
                                if archive_job.get('running'):
                                    break
                                archive_job.update({'running': True, 'message': f'Automatic {kind} time-lapse {cid} / {day_text}.'})
                            try:
                                cmd = ['/usr/bin/python3', str(APP / 'archive.py'), 'timelapse',
                                       '--camera', cid, '--day', day_text, '--mode', mode]
                                log('EXEC ' + redact_command(cmd) + f' [automatic daily {kind}]')
                                result = subprocess.run(cmd, cwd=str(ROOT), capture_output=True,
                                                        text=True, timeout=24 * 3600)
                                combined = (result.stdout + '\n' + result.stderr)[-8000:]
                                for line in combined.splitlines():
                                    log('AUTO-TIMELAPSE ' + line[:2000])
                                archive_job.update({'running': False, 'message': combined,
                                                    'returncode': result.returncode})
                            except Exception as exc:
                                archive_job.update({'running': False, 'message': str(exc)})
                            job_done = True
                            break  # One camera/day/mode job per scheduler pass.
                        if job_done:
                            break
                    cursor += __import__('datetime').timedelta(days=1)
                if job_done:
                    break
            if job_done:
                # Continue queued camera/day/archive/time-lapse jobs promptly;
                # the regular scan interval applies only while the queue is idle.
                last_scan = time.monotonic() - interval
        except Exception as exc:
            log(f'AUTOMATIC ARCHIVE SCHEDULER ERROR: {type(exc).__name__}: {exc}')


def control_file_watcher() -> None:
    """Apply direct USER_CONTROL.py edits without restarting recordings."""
    last = USER_CONTROL.stat().st_mtime_ns if USER_CONTROL.exists() else 0
    while True:
        time.sleep(5)
        try:
            current = USER_CONTROL.stat().st_mtime_ns
            if current == last:
                continue
            # Only mark the new version as seen after it parses and validates.
            values = load_control()
            if control_error:
                log('USER_CONTROL.py changed but is invalid; retaining last valid runtime settings: '+control_error)
                last = current
                continue
            write_media_config()
            last = current
            log('USER_CONTROL.py change applied; MediaMTX will hot-reload supported parameters.')
        except Exception as exc:
            log(f'USER_CONTROL watcher error: {type(exc).__name__}: {exc}')


def safe_file(relative: str) -> Path:
    candidate = (ROOT / unquote(relative)).resolve()
    allowed_roots = [(DATA / 'recordings').resolve(), (ROOT / 'cache' / 'playback').resolve()]
    if not any(candidate.is_relative_to(base) for base in allowed_roots) or not candidate.is_file():
        raise FileNotFoundError('Recording path not found.')
    return candidate


def public_camera(cam: dict) -> dict:
    out = dict(cam)
    if not load_control().get('ENABLE_CAMERA_WEB_ADMIN_LINKS', True): out['web_admin_url'] = ''
    out.pop('password', None)
    out.pop('web_admin_password', None)
    out['has_web_admin_password'] = bool(cam.get('web_admin_password'))
    out['has_password'] = bool(cam.get('password'))
    threshold = int(load_control().get('CAMERA_CLOCK_WARNING_SECONDS', 5))
    out['clock_warning'] = abs(int(cam.get('clock_offset_seconds', 0))) >= threshold
    out['live_url'] = f"rtsp://127.0.0.1:8554/{cam.get('id','')}"
    return out


