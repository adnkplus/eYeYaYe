#!/usr/bin/env python3
"""Native Tk desktop interface for eYeYaYe. No browser UI or HTTP API."""
from __future__ import annotations
import json
import os
import socket
import shutil
import signal
import itertools
import re
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from datetime import datetime, timezone, timedelta, date
from pathlib import Path
import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
from workspace import load_workspace, save_workspace
from timeline import archive_day_for, day_window, record_source_intervals, choose_record, output_position_for_wall_time

ROOT = Path(__file__).resolve().parents[1]
SOCKET = ROOT / 'runtime' / 'cctv.sock'
LOG = ROOT / 'logs' / 'gui.log'

PALETTES = {
    'cyberpunk': {'bg':'#070914','panel':'#0e1426','raised':'#141d35','text':'#eaf4ff','muted':'#91a3bd','accent':'#00e5ff','accent2':'#ff35d3','ok':'#00efa3','warn':'#ffbd59','bad':'#ff4f86','button':'#12364a'},
    'midnight': {'bg':'#09111f','panel':'#101d31','raised':'#192b45','text':'#edf5ff','muted':'#9eafc6','accent':'#67a9ff','accent2':'#9a82ff','ok':'#59d6a3','warn':'#f3c66b','bad':'#ee718d','button':'#183653'},
    'graphite': {'bg':'#15191e','panel':'#20262d','raised':'#2b333c','text':'#f0f2f4','muted':'#a8b0b8','accent':'#69c7d3','accent2':'#c58bd8','ok':'#72c99a','warn':'#e5b86e','bad':'#df7882','button':'#29424a'},
    'high-contrast': {'bg':'#000000','panel':'#101010','raised':'#202020','text':'#ffffff','muted':'#dddddd','accent':'#00ffff','accent2':'#ff00ff','ok':'#00ff00','warn':'#ffff00','bad':'#ff4040','button':'#003f4a'},
}


def rpc(action, params=None, timeout=10):
    payload = json.dumps({'action': action, 'params': params or {}}, separators=(',', ':')).encode() + b'\n'
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(str(SOCKET))
        sock.sendall(payload)
        data = bytearray()
        while len(data) < 8_000_000:
            part = sock.recv(65536)
            if not part:
                break
            data.extend(part)
            if b'\n' in part:
                break
    if not data:
        raise RuntimeError('The recording engine returned no response.')
    result = json.loads(bytes(data).split(b'\n', 1)[0].decode('utf-8'))
    if not result.get('ok'):
        raise RuntimeError(result.get('error', 'Engine request failed.'))
    return result.get('result', {})


def log_error(exc):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open('a', encoding='utf-8') as handle:
        handle.write(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {type(exc).__name__}: {exc}\n')
        handle.write(traceback.format_exc() + '\n')


class MpvPlayer:
    """One real continuous mpv player embedded into a Tk X11 child window."""
    _ids = itertools.count(1)

    def __init__(self, video_widget, player_id, log_path, input_conf, mpv_binary, control=None):
        self.video_widget = video_widget
        self.control = dict(control or {})
        self.player_id = player_id
        self.socket_path = ROOT / 'runtime' / f'mpv_{player_id}.sock'
        self.log_path = Path(log_path)
        self.input_conf = Path(input_conf)
        self.mpv_binary = mpv_binary
        self.process = None
        self.log_handle = None
        self.source = ''
        self.is_live = False
        self.volume = 80.0
        self.speed = 1.0
        self.fps = 'Auto'
        self.paused = False
        self._request_ids = itertools.count(1)
        self._pipe_thread = None
        self._pipe_stop = None
        self._pipe_path = None

    def start(self, source='', is_live=False, volume=80, speed=1.0, fps='Auto', position=0.0):
        self.stop()
        self.video_widget.update_idletasks()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open('wb', buffering=0)
        self.source = str(source or '')
        self.is_live = bool(is_live)
        self.volume = float(volume)
        self.speed = float(speed)
        self.fps = str(fps)
        self.paused = False
        args = [
            self.mpv_binary, '--no-config', '--terminal=no', '--force-window=yes',
            '--keep-open=yes', '--idle=yes', '--no-osc', '--osd-level=1',
            '--msg-level=all=info', '--vo=gpu,x11', '--input-vo-keyboard=no',
            '--demuxer=lavf',
            '--audio=yes', '--aid=auto', '--audio-device=auto', '--mute=no', '--audio-file-auto=no', f'--wid={int(self.video_widget.winfo_id())}',
            f'--input-ipc-server={self.socket_path}', f'--input-conf={self.input_conf}',
            f"--cache-secs={float(self.control.get('GUI_MPV_CACHE_SECONDS',2.5)):g}", '--cache=yes', '--demuxer-readahead-secs=2.5',
            f"--video-sync={self.control.get('GUI_MPV_VIDEO_SYNC','display-resample')}", f"--hwdec={self.control.get('GUI_MPV_HWDEC','auto-safe')}",
            f'--volume={self.volume:g}', f'--speed={self.speed:g}',
        ]
        if self.fps != 'Auto':
            args.append(f'--vf=@eyeyaye-fps:lavfi=[fps={int(self.fps)}]')
        # Start MPV idle first. Source loading is sent over IPC only after the
        # socket is live; passing source at process creation caused fast exits
        # and dropped playback commands on slow desktops.
        self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=self.log_handle, cwd=str(ROOT))
        return self.process

    def start_live_pipe(self, pipe_path, volume=80, speed=1.0, fps='Auto'):
        # MPV consumes a one-way Matroska byte stream from stdin. The GUI never
        # connects to the camera; FFmpeg owns the sole MediaMTX input session.
        self.stop()
        self.video_widget.update_idletasks()
        self.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self.socket_path.unlink(missing_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_handle = self.log_path.open('wb', buffering=0)
        self.source = str(pipe_path)
        self.is_live = True
        self.volume = float(volume)
        self.speed = float(speed)
        self.fps = str(fps)
        self.paused = False
        args = [
            self.mpv_binary, '--no-config', '--terminal=no', '--force-window=yes',
            '--keep-open=no', '--no-osc', '--osd-level=1', '--msg-level=all=info',
            '--vo=gpu,x11', '--input-vo-keyboard=no',
            '--demuxer=lavf', '--demuxer-lavf-format=matroska',
            '--audio=yes', '--aid=auto', '--audio-device=auto', '--mute=no',
            f'--wid={int(self.video_widget.winfo_id())}',
            f'--input-ipc-server={self.socket_path}', f'--input-conf={self.input_conf}',
            f'--volume={self.volume:g}', f'--speed={self.speed:g}', '-'
        ]
        if self.fps != 'Auto':
            args.insert(-1, f'--vf=@eyeyaye-fps:lavfi=[fps={int(self.fps)}]')
        self.process = subprocess.Popen(args, stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL, stderr=self.log_handle, cwd=str(ROOT), bufsize=0)
        self._pipe_path = Path(pipe_path)
        self._pipe_stop = threading.Event()
        self._pipe_thread = threading.Thread(target=self._forward_live_pipe,
            args=(self._pipe_path, self.process, self._pipe_stop),
            name=f'mpv-pipe-{self.player_id}', daemon=True)
        self._pipe_thread.start()
        return self.process

    @staticmethod
    def _forward_live_pipe(path, process, stop_event):
        # A nonblocking FIFO reader can stay open between FFmpeg FIFO-writer
        # reconnects. EOF is not fatal; a dead MPV or broken stdin is.
        import select
        fd = None
        saw_stream_data = False
        try:
            while not stop_event.is_set() and process.poll() is None:
                if fd is None:
                    try:
                        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
                    except OSError:
                        stop_event.wait(0.1)
                        continue
                try:
                    readable, _, _ = select.select([fd], [], [], 0.2)
                except (OSError, ValueError):
                    break
                if not readable:
                    continue
                try:
                    block = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                except OSError:
                    break
                if not block:
                    if saw_stream_data:
                        # FFmpeg's FIFO output has closed this Matroska stream.
                        # End MPV's stdin so the tile supervisor can relaunch it
                        # against the next Matroska header after FIFO recovery.
                        break
                    stop_event.wait(0.05)
                    continue
                saw_stream_data = True
                try:
                    view = memoryview(block)
                    while view and not stop_event.is_set() and process.poll() is None:
                        written = os.write(process.stdin.fileno(), view)
                        view = view[written:]
                except (BrokenPipeError, OSError, AttributeError):
                    break
        finally:
            if saw_stream_data and process.poll() is None and process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass

    def connected(self):
        return self.process is not None and self.process.poll() is None and self.socket_path.exists()

    def command(self, command, timeout=0.20):
        if not self.connected():
            return None
        request_id = next(self._request_ids)
        payload = json.dumps({'command': command, 'request_id': request_id}, separators=(',', ':')).encode() + b'\n'
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self.socket_path))
                client.sendall(payload)
                data = bytearray()
                while len(data) < 65536:
                    block = client.recv(65536)
                    if not block:
                        break
                    data.extend(block)
                    if b'\n' in block:
                        break
            if not data:
                return None
            reply = json.loads(bytes(data).split(b'\n', 1)[0].decode('utf-8'))
            if reply.get('error') not in (None, 'success'):
                return None
            return reply.get('data')
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def get(self, property_name, default=None):
        value = self.command(['get_property', property_name])
        return default if value is None else value

    def get_many(self, property_names, timeout=0.22):
        """Read multiple MPV properties over one IPC connection and one deadline."""
        names = list(dict.fromkeys(str(x) for x in property_names))
        result = {name: None for name in names}
        if not names or not self.connected():
            return result
        ids = {next(self._request_ids): name for name in names}
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.settimeout(timeout)
                client.connect(str(self.socket_path))
                payload = b''.join(
                    json.dumps({'command': ['get_property', name], 'request_id': rid},
                               separators=(',', ':')).encode() + b'\n'
                    for rid, name in ids.items()
                )
                client.sendall(payload)
                buffer = bytearray()
                received = set()
                deadline = time.monotonic() + timeout
                while len(received) < len(ids):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    client.settimeout(remaining)
                    block = client.recv(65536)
                    if not block:
                        break
                    buffer.extend(block)
                    while b'\n' in buffer:
                        line, _, rest = buffer.partition(b'\n')
                        buffer = bytearray(rest)
                        try:
                            reply = json.loads(line.decode('utf-8'))
                        except (UnicodeDecodeError, json.JSONDecodeError):
                            continue
                        rid = reply.get('request_id')
                        if rid in ids:
                            name = ids[rid]
                            if reply.get('error') in (None, 'success'):
                                result[name] = reply.get('data')
                            received.add(rid)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        return result

    def set(self, property_name, value):
        self.command(['set_property', property_name, value])

    def set_fps_cap(self, fps):
        value = str(fps)
        if value != 'Auto' and (not value.isdigit() or not 1 <= int(value) <= 120):
            return False
        # Remove and replace just this named filter. The MPV process and RTSP
        # session stay alive; no engine or MediaMTX restart is involved.
        self.command(['vf', 'remove', '@eyeyaye-fps'])
        if value != 'Auto':
            self.command(['vf', 'add', f'@eyeyaye-fps:lavfi=[fps={int(value)}]'])
        self.fps = value
        return True

    def load(self, source, is_live=False, position=0.0):
        self.source = str(source)
        self.is_live = bool(is_live)
        self.paused = False
        self.command(['loadfile', self.source, 'replace'])
        if position > 0 and not is_live:
            self.command(['seek', float(position), 'absolute+exact'])

    def seek(self, position):
        if not self.is_live:
            self.command(['seek', max(0.0, float(position)), 'absolute+exact'])

    def stop(self):
        stop_event = self._pipe_stop
        if stop_event is not None:
            stop_event.set()
        proc = self.process
        if proc is not None and proc.poll() is None:
            try:
                self.command(['quit'], timeout=0.35)
            except Exception:
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try:
                    proc.wait(timeout=1.5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=1.5)
        if proc is not None and proc.stdin is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        thread = self._pipe_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        self._pipe_thread = None
        self._pipe_stop = None
        self._pipe_path = None
        self.process = None
        self.socket_path.unlink(missing_ok=True)
        if self.log_handle:
            try:
                self.log_handle.close()
            except Exception:
                pass
            self.log_handle = None



class VideoTile:
    """Camera tile with its own video process, audio, speed, seek and FPS controls."""
    def __init__(self, owner, parent, camera, kind, index, recordings=None):
        self.owner = owner
        self.camera = camera or {'id': f'play{index+1}', 'name': f'PLAYER {index+1}'}
        self.camera_id = self.camera.get('id', '')
        self.kind = kind
        self.index = index
        self.recordings = list(recordings or [])
        self.current_record = None
        self.current_source = ''
        self.initial_position = 0.0
        self.volume = float(owner.control.get('GUI_DEFAULT_TILE_VOLUME', 80))
        self.speed = float(owner.control.get('PLAYBACK_DEFAULT_SPEED', 1.0))
        self.fps = str(owner.control.get('GUI_DEFAULT_FPS' if kind == 'live' else 'GUI_PLAYBACK_FPS_DEFAULT', 'Auto'))
        self.muted = False
        self.stats_visible = False
        self._stats_after = None
        self._updating_seek = False
        self._seek_dragging = False
        self.play_state = 'STOPPED'
        self._live_started_at = 0.0
        self._last_video_at = None
        self._last_live_restart_at = 0.0
        self._live_restart_count = 0
        self._last_audio_signature = None
        self._last_live_time_pos = None
        self._last_progress_at = None
        self._player_started_at = 0.0
        self._source_load_started_at = 0.0
        self._pending_load_after = None
        bg = owner.palette.get('camera_tile_bg', '#050a12')
        self.frame = tk.Frame(parent, bg=bg, highlightthickness=1, highlightbackground=owner.palette['accent'])
        self.header = tk.Frame(self.frame, bg=bg); self.header.pack(fill='x', padx=7, pady=(5,2))
        self.title = tk.Label(self.header, text=f"{self.camera.get('name',self.camera_id)} · {self.camera_id}", bg=bg, fg=owner.palette['accent'], font=('DejaVu Sans',9,'bold'))
        self.title.pack(side='left')
        self.status = tk.Label(self.header, text='STARTING', bg=bg, fg=owner.palette['warn'], font=('DejaVu Sans Mono',8))
        self.status.pack(side='right')
        self.cue_button=tk.Button(self.header,text='CUES ▾',command=self._show_cue_menu,relief='flat',bg=owner.palette['raised'],fg=owner.palette['accent'],activebackground=owner.palette['button'],font=('DejaVu Sans',8,'bold'))
        self.cue_button.pack(side='right',padx=5)
        self.picker_var = tk.StringVar(value='Select video…')
        self.picker = None
        if kind == 'playback':
            self.picker = ttk.Combobox(self.header, textvariable=self.picker_var, state='readonly', width=26)
            self.picker.pack(side='right', padx=(4,8))
            self.picker.bind('<<ComboboxSelected>>', self._pick_recording)
        self.video = tk.Frame(self.frame, bg='#000000', highlightthickness=0)
        self.video.pack(fill='both', expand=True, padx=5, pady=3)
        self.placeholder = tk.Label(self.video, text='WAITING FOR VIDEO', bg='#000000', fg=owner.palette['muted'], font=('DejaVu Sans Mono',10))
        self.placeholder.place(relx=.5, rely=.5, anchor='center')
        self.controls = tk.Frame(self.frame, bg=bg); self.controls.pack(fill='x', padx=6, pady=(1,2))
        self._build_controls()
        self.footer = tk.Label(self.frame, text='', bg=bg, fg=owner.palette['muted'], font=('DejaVu Sans Mono',7), anchor='w')
        self.footer.pack(fill='x', padx=7, pady=(0,4))
        self.player = None
        self.link_var = tk.BooleanVar(value=True)
        self.link_button = tk.Checkbutton(self.header,text='LINK',variable=self.link_var,relief='flat',bd=0,bg=bg,fg=owner.palette['muted'],activebackground=bg,activeforeground=owner.palette['accent'],selectcolor=owner.palette['raised'],font=('DejaVu Sans Mono',7))
        self.link_button.pack(side='right',padx=3)
        self.cue_in_ts = None
        self.cue_out_ts = None
        self.set_recordings(self.recordings)
        self.video.configure(takefocus=1)
        self.video.bind('<Button-1>', lambda _e: owner.set_focused_tile(self), add='+')
        self.video.bind('<Double-Button-1>', lambda _e: owner.toggle_tile_fullscreen(self))
        self.video.bind('<KeyPress>', owner._on_video_key, add='+')
        self.frame.bind('<Button-1>', lambda _e: owner.set_focused_tile(self), add='+')
        self.video.bind('<Button-4>', lambda _e: self._video_wheel(1))
        self.video.bind('<Button-5>', lambda _e: self._video_wheel(-1))
        self.video.bind('<MouseWheel>', self._video_wheel_event)
        self._start_after = owner.root.after(350, self.start_player)
        owner.root.after(700, self._poll_status)

    def _button(self, parent, text, command, width=7):
        return tk.Button(parent, text=text, command=command, relief='flat',
            bg=self.owner.palette['raised'], fg=self.owner.palette['accent'],
            activebackground=self.owner.palette['button'], activeforeground=self.owner.palette['text'],
            width=width, padx=3, pady=3, font=('DejaVu Sans', 8, 'bold'))

    def _build_controls(self):
        row = tk.Frame(self.controls, bg=self.controls['bg']); row.pack(fill='x')
        if self.kind == 'playback':
            self.play_button = self._button(row, 'PLAY', self.play, 6); self.play_button.pack(side='left', padx=1)
            self.pause_button = self._button(row, 'PAUSE', self.pause, 6); self.pause_button.pack(side='left', padx=1)
            self.stop_button = self._button(row, 'STOP', self.stop_playback, 6); self.stop_button.pack(side='left', padx=1)
            self.audio_button = self._button(row, 'AUDIO', self.next_audio_track, 6); self.audio_button.pack(side='left', padx=1)
            self._button(row, 'STATS', self.toggle_stats, 6).pack(side='left', padx=1)
            self._button(row, 'FULL', lambda: self.owner.toggle_tile_fullscreen(self), 6).pack(side='left', padx=1)
            tk.Label(row, text='Speed', bg=row['bg'], fg=self.owner.palette['muted']).pack(side='left', padx=(6,2))
            self.speed_var = tk.StringVar(value=f'{self.speed:g}x')
            self.speed_box = ttk.Combobox(row, textvariable=self.speed_var,
                values=[f'{x:g}x' for x in self.owner.control.get('GUI_SPEED_OPTIONS',[0.25,0.5,0.75,1,1.25,1.5,2,4,8,16,30,60])],
                width=5, state='readonly')
            self.speed_box.pack(side='left'); self.speed_box.bind('<<ComboboxSelected>>', self._speed_selected)
            self.speed_box.bind('<MouseWheel>', self._speed_wheel); self.speed_box.bind('<Button-4>', lambda e:self._speed_wheel_step(1)); self.speed_box.bind('<Button-5>', lambda e:self._speed_wheel_step(-1))
            self.speed_box.bind('<Enter>', lambda _e:self.speed_box.focus_set())
            self._speed_wheel_dir = 0
        else:
            self._button(row, 'MUTE', self.toggle_mute, 6).pack(side='left', padx=1)
            self._button(row, 'STATS', self.toggle_stats, 6).pack(side='left', padx=1)
            self._button(row, 'FULL', lambda: self.owner.toggle_tile_fullscreen(self), 6).pack(side='left', padx=1)
        tk.Label(row, text='FPS cap', bg=row['bg'], fg=self.owner.palette['muted']).pack(side='left', padx=(6,2))
        self.fps_var = tk.StringVar(value=self.fps)
        self.fps_box = ttk.Combobox(row, textvariable=self.fps_var,
            values=self.owner.control.get('GUI_FPS_OPTIONS',['Auto','15','25','30','50','60']),
            width=5, state='readonly')
        self.fps_box.pack(side='left'); self.fps_box.bind('<<ComboboxSelected>>', self._fps_selected)
        if self.kind == 'playback':
            seekrow = tk.Frame(self.controls, bg=row['bg']); seekrow.pack(fill='x', pady=(2,0))
            self.seek_var = tk.DoubleVar(value=0)
            self.seek_scale = ttk.Scale(seekrow, from_=0, to=100, variable=self.seek_var, command=self._seek_changed)
            self.seek_scale.pack(side='left', fill='x', expand=True)
            self.seek_scale.bind('<ButtonPress-1>', lambda _e:setattr(self,'_seek_dragging',True))
            self.seek_scale.bind('<ButtonRelease-1>', self._seek_release)
            self.time_label = tk.Label(seekrow, text='00:00 / --:--', bg=row['bg'], fg=self.owner.palette['muted'], font=('DejaVu Sans Mono',7))
            self.time_label.pack(side='right', padx=4)
        volume_row = tk.Frame(self.controls, bg=self.controls['bg']); volume_row.pack(fill='x', pady=(2,0))
        self.mute_button = self._button(volume_row, 'UNMUTE' if self.muted else 'MUTE', self.toggle_mute, 7)
        self.mute_button.pack(side='left', padx=1)
        self.volume_var = tk.DoubleVar(value=self.volume)
        self.volume_scale = ttk.Scale(volume_row, from_=0, to=100, variable=self.volume_var, command=self._volume_changed)
        self.volume_scale.pack(side='left', fill='x', expand=True, padx=4)
        self.volume_label = tk.Label(volume_row, text=f'{int(self.volume)}%', bg=volume_row['bg'], fg=self.owner.palette['muted'], font=('DejaVu Sans Mono',7), width=4)
        self.volume_label.pack(side='right')
        for widget in (self.volume_scale, self.volume_label, self.mute_button):
            widget.bind('<MouseWheel>', self._volume_wheel)
            widget.bind('<Button-4>', lambda e:self._volume_step(1))
            widget.bind('<Button-5>', lambda e:self._volume_step(-1))

    def toggle_stats(self):
        if not self.player or not self.player.connected():
            self.status.configure(text='PLAYER NOT READY', fg=self.owner.palette['warn'])
            return
        self.stats_visible = not self.stats_visible
        if self.stats_visible:
            self._refresh_stats_overlay()
        else:
            if self._stats_after is not None:
                try: self.owner.root.after_cancel(self._stats_after)
                except tk.TclError: pass
                self._stats_after = None
            self.player.command(['show-text', '', 1, 1])

    def _refresh_stats_overlay(self):
        try:frame_exists=self.frame.winfo_exists()
        except tk.TclError:frame_exists=False
        if not self.stats_visible or not frame_exists or not self.player or not self.player.connected():
            self.stats_visible = False
            self._stats_after = None
            return
        text = (
            'eYeYaYe — STREAM / FILE STATISTICS\n'
            'VIDEO  ${video-codec-name}  |  ${width}x${height}  |  ${container-fps} fps  |  ${estimated-vf-fps} output fps  |  ${video-bitrate}\n'
            'AUDIO  ${audio-codec-name}  |  ${audio-params}  |  ${audio-bitrate}  |  device ${audio-device}\n'
            'CONTAINER  ${file-format}  |  DEMUXER ${current-demuxer}  |  VIDEO FORMAT ${video-format}\n'
            'TIME ${time-pos} / ${duration}  |  CACHE ${demuxer-cache-duration}s  |  DROPPED ${decoder-frame-drop-count}  |  VOL ${volume}%'
        )
        self.player.command(['show-text', text, 1350, 1])
        self._stats_after = self.owner.root.after(1000, self._refresh_stats_overlay)

    def set_recordings(self, recordings):
        self.recordings=list(recordings or [])
        if self.kind!='playback' or self.picker is None:return
        def label(rec):
            stamp=datetime.fromtimestamp(float(rec.get('start_ts',0))).astimezone().strftime('%d/%m/%y %H:%M:%S')
            return f"{rec.get('camera_id','')} · {rec.get('type_code',rec.get('group','')).upper()} · {stamp} · {rec.get('name','')}"
        self.record_labels=[label(x) for x in self.recordings]
        self.picker.configure(values=self.record_labels)
        if self.current_record:
            for i,item in enumerate(self.recordings):
                if item.get('path')==self.current_record.get('path'):
                    self.picker.current(i);return
        preferred=[(i,x) for i,x in enumerate(self.recordings) if x.get('camera_id')==self.camera_id]
        if self.owner.timeline_sync_active:
            return
        if preferred:
            i,rec=max(preferred,key=lambda ix:ix[1].get('start_ts',0));self.picker.current(i);self.load_recording(rec)
        elif self.recordings and not self.current_record:
            self.picker.current(0)

    def _pick_recording(self,_event=None):
        i=self.picker.current()
        if 0<=i<len(self.recordings):
            self.owner.timeline_sync_active=False
            self.load_recording(self.recordings[i])

    def load_recording(self, record, position=0.0, autoplay=True):
        path=(ROOT/str(record.get('path',''))).resolve()
        if not path.is_file():
            self.status.configure(text='MISSING FILE',fg=self.owner.palette['bad']);return
        self.current_record=record
        self.current_source=str(path)
        self.initial_position=max(0.0,float(position))
        self.camera_id=record.get('camera_id',self.camera_id)
        self.play_state='PLAYING' if autoplay else 'PAUSED'
        self.title.configure(text=f"{record.get('camera_id','')} · {record.get('type_code',record.get('group','')).upper()}")
        self.footer.configure(text=record.get('name',''))
        self.placeholder.configure(text='OPENING RECORDING')
        self.placeholder.place(relx=.5,rely=.5,anchor='center')
        if self.picker is not None:
            for i,item in enumerate(self.recordings):
                if item.get('path') == record.get('path'):
                    self.picker.current(i);break
        self._load_source(self.current_source, False, self.initial_position)
        if not autoplay and self.player and self.player.connected():
            self.player.set('pause', True)

    def mark_no_footage(self):
        self.current_record=None
        self.current_source=''
        self.initial_position=0.0
        self.play_state='STOPPED'
        if self.player and self.player.connected():
            self.player.command(['stop'])
        self.status.configure(text='NO FOOTAGE',fg=self.owner.palette['muted'])
        self.footer.configure(text='No recording covers the selected wall-clock time.')
        self.placeholder.configure(text='NO FOOTAGE AT THIS TIME')
        self.placeholder.place(relx=.5,rely=.5,anchor='center')
        if self.picker is not None:
            self.picker_var.set('No footage at selected time')

    def _player_log_excerpt(self):
        try:
            lines = (ROOT/'logs'/f'mpv-{self.kind}-{self.index}.log').read_text(
                encoding='utf-8', errors='replace').splitlines()
            useful = [line.strip() for line in lines if line.strip()]
            return ' | '.join(useful[-4:])[-420:]
        except OSError:
            return 'No MPV log was created.'

    def _schedule_source_load(self, source, is_live, position=0.0):
        if self._pending_load_after is not None:
            try: self.owner.root.after_cancel(self._pending_load_after)
            except tk.TclError: pass
        self._pending_load_after = self.owner.root.after(
            100, lambda: self._wait_for_player_then_load(str(source), bool(is_live), float(position), 0))

    def start_player(self):
        self._start_after = None
        if not self.frame.winfo_exists():
            return
        if self.kind == 'live' and not self.owner.live_camera_is_enabled(self.camera_id):
            self.status.configure(text='LIVE OFF', fg=self.owner.palette['muted'])
            self.placeholder.configure(text='LIVE STREAM DISABLED')
            self.placeholder.place(relx=.5, rely=.5, anchor='center')
            return
        if self.kind == 'live' and hasattr(self.owner, 'show_live_var') and not self.owner.show_live_var.get():
            self.status.configure(text='LIVE DOCK HIDDEN', fg=self.owner.palette['muted'])
            return
        if self.kind == 'playback' and hasattr(self.owner, 'show_playback_var') and not self.owner.show_playback_var.get():
            self.status.configure(text='PLAYBACK HIDDEN', fg=self.owner.palette['muted'])
            return
        # A live MPV process can take a short time to create its IPC socket.
        # Never terminate/relaunch it just because that socket is not ready yet.
        if self.player and self.player.process and self.player.process.poll() is None:
            if not self.player.connected():
                self.status.configure(text='PLAYER STARTING', fg=self.owner.palette['warn'])
            return
        mpv = self.owner.mpv_binary
        if not mpv:
            self.placeholder.configure(text='mpv not installed — sudo apt install mpv')
            self.status.configure(text='PLAYER MISSING', fg=self.owner.palette['bad'])
            return
        self.video.update_idletasks()
        if not self.video.winfo_ismapped():
            self._start_after = self.owner.root.after(150, self.start_player)
            return
        if self.player is None:
            self.player = MpvPlayer(self.video, f'{self.kind}_{self.index}_{self.camera_id}',
                ROOT/'logs'/f'mpv-{self.kind}-{self.index}.log',
                ROOT/'runtime'/'mpv-playback-input.conf' if self.kind=='playback' else ROOT/'runtime'/'mpv-live-input.conf',
                mpv, self.owner.control)
        try:
            if self.kind == 'live':
                pipe_path = ROOT / 'runtime' / 'live' / f'{self.camera_id}.mkvpipe'
                if not pipe_path.exists():
                    self.status.configure(text='RECORDER PIPE MISSING', fg=self.owner.palette['bad'])
                    self.footer.configure(text=f'Waiting for recorder FIFO: {pipe_path}')
                    return
                self.player.start_live_pipe(pipe_path, self.volume, self.speed, self.fps)
            else:
                source = self.current_source
                self.player.start('', False, self.volume, self.speed, self.fps, 0.0)
                if source:
                    self._schedule_source_load(source, False, self.initial_position)
            now = time.monotonic()
            self._player_started_at = now
            if self.kind == 'live':
                self._live_started_at = now
                self._last_video_at = None
                self._last_live_time_pos = None
                self._last_progress_at = None
                self._last_live_restart_at = now
                self.placeholder.configure(text='WAITING FOR LIVE VIDEO')
                self.placeholder.place(relx=.5, rely=.5, anchor='center')
                self.status.configure(text='LIVE FIFO', fg=self.owner.palette['warn'])
            elif source:
                self.play_state = 'PLAYING'
                self.placeholder.configure(text='OPENING RECORDING')
                self.placeholder.place(relx=.5, rely=.5, anchor='center')
                self.status.configure(text='LOADING FILE', fg=self.owner.palette['warn'])
            else:
                self.play_state = 'STOPPED'
                self.status.configure(text='SELECT A VIDEO', fg=self.owner.palette['muted'])
        except Exception as exc:
            self.placeholder.configure(text='PLAYER START FAILED')
            self.status.configure(text='PLAYER ERROR', fg=self.owner.palette['bad'])
            detail = self._player_log_excerpt()
            self.footer.configure(text=(str(exc) + ' | ' + detail)[:420])
            log_error(exc)

    def stop_player(self):
        if self._pending_load_after is not None:
            try: self.owner.root.after_cancel(self._pending_load_after)
            except tk.TclError: pass
            self._pending_load_after = None
        if self._start_after is not None:
            try: self.owner.root.after_cancel(self._start_after)
            except tk.TclError: pass
            self._start_after = None
        if self.player:
            self.player.stop(); self.player = None
        if self.kind == 'live':
            if self.owner.live_camera_is_enabled(self.camera_id):
                self.status.configure(text='LIVE DOCK HIDDEN', fg=self.owner.palette['muted'])
            else:
                self.status.configure(text='LIVE OFF', fg=self.owner.palette['muted'])
                self.placeholder.configure(text='LIVE STREAM DISABLED')
                self.placeholder.place(relx=.5, rely=.5, anchor='center')


    def _load_source(self, source, is_live, position=0.0):
        self.current_source = str(source)
        self._ensure_player()
        if not self.player:
            return
        if self.player.connected():
            self.player.load(source, is_live, position)
        elif self.player.process is not None and self.player.process.poll() is None:
            self._schedule_source_load(source, is_live, position)
        elif self._start_after is None:
            self._start_after = self.owner.root.after(100, self.start_player)

    def _wait_for_player_then_load(self, source, is_live, position, attempt):
        self._pending_load_after = None
        if not self.frame.winfo_exists() or not self.player:
            return
        if self.player.process is None or self.player.process.poll() is not None:
            self.status.configure(text='PLAYER EXITED', fg=self.owner.palette['bad'])
            self.footer.configure(text=self._player_log_excerpt() or 'MPV exited before its IPC socket became ready.')
            return
        if self.player.connected():
            self.player.load(source, is_live, position)
            self._source_load_started_at = time.monotonic()
            if self.kind == 'playback':
                self.play_state = 'PLAYING'
                self._set_play_state('PLAYING')
            return
        if attempt >= 50:
            self.status.configure(text='PLAYER IPC TIMEOUT', fg=self.owner.palette['bad'])
            self.footer.configure(text=(self._player_log_excerpt() or 'MPV IPC socket did not appear.')[:420])
            return
        self._pending_load_after = self.owner.root.after(
            100, lambda: self._wait_for_player_then_load(source, is_live, position, attempt + 1))

    def _ensure_player(self):
        if self.player is None or self.player.process is None or self.player.process.poll() is not None:
            self.start_player()

    def _restart_player(self, position=0.0):
        if self.player is None:
            self.start_player()
            return
        try:
            if self.kind == 'live':
                pipe_path = ROOT / 'runtime' / 'live' / f'{self.camera_id}.mkvpipe'
                self.player.start_live_pipe(pipe_path, self.volume, self.speed, self.fps)
            else:
                source = self.current_source
                self.player.start('', False, self.volume, self.speed, self.fps, 0.0)
                self._schedule_source_load(source, False, position)
            now = time.monotonic()
            self._player_started_at = now
            if self.kind == 'live':
                self._live_started_at = now
                self._last_video_at = None
                self._last_live_time_pos = None
                self._last_progress_at = None
                self._last_live_restart_at = now
                self.placeholder.configure(text='RECONNECTING LIVE FIFO')
                self.placeholder.place(relx=.5, rely=.5, anchor='center')
                self.status.configure(text='RECONNECTING FIFO', fg=self.owner.palette['warn'])
        except Exception as exc:
            self.status.configure(text='RESTART FAILED', fg=self.owner.palette['bad'])
            self.footer.configure(text=str(exc)[:250])
            log_error(exc)

    def _poll_status(self):
        try:
            if not self.frame.winfo_exists():
                return
        except tk.TclError:
            return
        now = time.monotonic()
        try:
            if self.kind == 'live' and not self.owner.live_camera_is_enabled(self.camera_id):
                if self.player is not None:
                    self.stop_player()
                self.status.configure(text='LIVE OFF', fg=self.owner.palette['muted'])
                self.placeholder.configure(text='LIVE STREAM DISABLED')
                self.placeholder.place(relx=.5, rely=.5, anchor='center')
            elif self.kind == 'live' and hasattr(self.owner, 'show_live_var') and not self.owner.show_live_var.get():
                self.status.configure(text='LIVE DOCK HIDDEN', fg=self.owner.palette['muted'])
            elif self.player is None:
                self.status.configure(text='PLAYER STOPPED', fg=self.owner.palette['muted'])
                if self.kind == 'live' and self.owner.show_live_var.get() and self.owner.live_camera_is_enabled(self.camera_id) and self._start_after is None:
                    self._start_after = self.owner.root.after(200, self.start_player)
            elif self.player.process is not None and self.player.process.poll() is not None:
                if self.kind == 'live' and self.owner.show_live_var.get():
                    self.status.configure(text='PLAYER EXITED — RECONNECTING', fg=self.owner.palette['bad'])
                    retry_delay = min(30.0, 2.0 * (2 ** min(self._live_restart_count, 4)))
                    if now - self._last_live_restart_at >= retry_delay:
                        self._live_restart_count += 1
                        self._restart_player()
                else:
                    self.status.configure(text='PLAYER EXITED', fg=self.owner.palette['bad'])
                    self.footer.configure(text=self._player_log_excerpt()[:420])
            elif not self.player.connected():
                age = now - self._player_started_at
                self.status.configure(text='PLAYER STARTING' if age < 8 else 'PLAYER IPC UNAVAILABLE',
                                      fg=self.owner.palette['warn'] if age < 8 else self.owner.palette['bad'])
                if age >= 8:
                    self.footer.configure(text=(self._player_log_excerpt() or 'MPV is alive but its IPC socket is unavailable.')[:420])
            else:
                props = self.player.get_many([
                    'core-idle', 'video-params', 'playback-time', 'time-pos', 'duration', 'pause',
                    'eof-reached', 'track-list', 'audio-codec-name', 'volume',
                    'video-codec-name', 'container-fps', 'estimated-vf-fps',
                    'video-bitrate', 'audio-bitrate', 'file-format', 'current-demuxer',
                    'demuxer-cache-duration', 'decoder-frame-drop-count',
                ], timeout=0.22)
                idle = bool(props.get('core-idle'))
                video_params = props.get('video-params')
                try:
                    video_width = int(video_params.get('w', video_params.get('width', 0)) or 0) if isinstance(video_params, dict) else 0
                    video_height = int(video_params.get('h', video_params.get('height', 0)) or 0) if isinstance(video_params, dict) else 0
                except (TypeError, ValueError):
                    video_width = video_height = 0
                has_video = video_width > 0 and video_height > 0
                if (self.kind == 'playback' and self.current_source and self.play_state == 'PLAYING'
                        and idle and self._source_load_started_at
                        and now - self._source_load_started_at > 4.0
                        and not props.get('eof-reached')):
                    self.status.configure(text='FILE OPEN FAILED', fg=self.owner.palette['bad'])
                    self.placeholder.configure(text='CANNOT DECODE VIDEO')
                    self.placeholder.place(relx=.5, rely=.5, anchor='center')
                    self.footer.configure(text=self._player_log_excerpt()[:420])
                time_pos = props.get('playback-time')
                if time_pos is None:
                    time_pos = props.get('time-pos')
                if self.kind == 'live' and has_video and time_pos is not None:
                    try:
                        time_pos = float(time_pos)
                        if self._last_live_time_pos is None or abs(time_pos - self._last_live_time_pos) > 0.015:
                            self._last_live_time_pos = time_pos
                            self._last_progress_at = now
                            self._last_video_at = now
                    except (TypeError, ValueError):
                        time_pos = None
                elif has_video and self.kind == 'live' and self._last_video_at is None:
                    self._last_video_at = now
                    self._last_progress_at = now
                if has_video and self.placeholder.winfo_ismapped():
                    self.placeholder.place_forget()
                elif not has_video and not self.placeholder.winfo_ismapped():
                    self.placeholder.configure(text='WAITING FOR VIDEO' if self.kind == 'live' else 'OPENING RECORDING')
                    self.placeholder.place(relx=.5, rely=.5, anchor='center')

                tracks = props.get('track-list') or []
                audio_tracks = [item for item in tracks if isinstance(item, dict) and item.get('type') == 'audio'] if isinstance(tracks, list) else []
                selected_audio = next((item for item in audio_tracks if item.get('selected')), audio_tracks[0] if audio_tracks else None)
                if selected_audio:
                    audio_codec = props.get('audio-codec-name') or selected_audio.get('codec', 'audio track')
                    audio_text = f'AUDIO {audio_codec} ({len(audio_tracks)} track)'
                elif self.kind == 'live' and self.camera.get('type') == 'cam3':
                    audio_text = 'AUDIO none (CAM3 has no microphone)'
                elif has_video or self.kind == 'playback':
                    audio_text = 'NO AUDIO TRACK'
                else:
                    audio_text = 'AUDIO waiting'

                if self.kind == 'live':
                    progress_reference = self._last_progress_at or self._last_video_at or self._live_started_at
                    age = max(0.0, now - progress_reference)
                    stalled = bool(has_video and self._last_progress_at and age >= 12.0)
                    if has_video and not stalled:
                        self.status.configure(text='LIVE', fg=self.owner.palette['ok'])
                        self._live_restart_count = 0
                        self.footer.configure(text=f'VIDEO {video_width}x{video_height}  |  {audio_text}  |  LIVE')
                    else:
                        self.status.configure(text='STREAM STALLED' if stalled else ('CONNECTING' if now-self._live_started_at < 8 else 'WAITING FOR VIDEO'),
                                              fg=self.owner.palette['bad'] if stalled else self.owner.palette['warn'])
                        self.placeholder.configure(text='STREAM STALLED — RECONNECTING' if stalled else 'WAITING FOR LIVE VIDEO')
                        self.placeholder.place(relx=.5, rely=.5, anchor='center')
                        grace = 12.0 if stalled else (45.0 if self.camera.get('type') == 'cam3' else 20.0)
                        retry_delay = min(45.0, grace * (1.0 if self._live_restart_count == 0 else min(3.0, 1.4 ** self._live_restart_count)))
                        if age >= grace and now - self._last_live_restart_at >= retry_delay:
                            self._live_restart_count += 1
                            self.footer.configure(text=f'No advancing video for {int(age)}s — reconnect {self._live_restart_count}; {audio_text}')
                            self._restart_player()
                else:
                    eof = bool(props.get('eof-reached'))
                    paused = bool(props.get('pause'))
                    if eof:
                        if self.kind == 'playback' and self.owner._timeline_continue_after_eof(self):
                            self._set_play_state('PLAYING')
                        else:
                            self._set_play_state('STOPPED')
                    elif self.play_state != 'STOPPED' and has_video:
                        self._set_play_state('PAUSED' if paused else 'PLAYING')
                    elif self.current_source and not has_video and not idle and self.play_state != 'STOPPED':
                        self.status.configure(text='DECODING VIDEO', fg=self.owner.palette['warn'])
                    elif not self.current_source:
                        self.status.configure(text='SELECT A VIDEO', fg=self.owner.palette['muted'])
                    pos = props.get('time-pos'); duration = props.get('duration')
                    if pos is not None and duration and float(duration) > 0 and not self._seek_dragging:
                        self._updating_seek = True
                        self.seek_var.set(100 * max(0, min(1, float(pos) / float(duration))))
                        self.time_label.configure(text=f'{self._fmt_time(pos)} / {self._fmt_time(duration)}')
                        self._updating_seek = False
                    if has_video:
                        self.footer.configure(text=f'VIDEO {video_width}x{video_height}  |  {audio_text}')
                    elif self.current_source and self.play_state != 'STOPPED':
                        self.footer.configure(text='Waiting for video/audio tracks…')
        except (tk.TclError, OSError, ValueError, TypeError) as exc:
            try: self.footer.configure(text=f'STATUS CHECK: {str(exc)[:180]}')
            except tk.TclError: pass
            log_error(exc)
        try:
            self.owner.root.after(700, self._poll_status)
        except tk.TclError:
            pass

    @staticmethod
    def _fmt_time(value):
        n=max(0,int(float(value)));return f'{n//3600:02d}:{(n%3600)//60:02d}:{n%60:02d}' if n>=3600 else f'{n//60:02d}:{n%60:02d}'

    def _set_play_state(self, state):
        self.play_state = state
        if self.kind == 'playback':
            self.status.configure(text=state, fg=self.owner.palette['ok'] if state == 'PLAYING' else self.owner.palette['warn'] if state == 'PAUSED' else self.owner.palette['muted'])
            self.play_button.configure(relief='sunken' if state == 'PLAYING' else 'flat')
            self.pause_button.configure(relief='sunken' if state == 'PAUSED' else 'flat')
            self.stop_button.configure(relief='sunken' if state == 'STOPPED' else 'flat')

    def play(self):
        if not self.current_source:
            self.status.configure(text='SELECT A VIDEO', fg=self.owner.palette['muted'])
            return
        if not self.player or not self.player.connected():
            self._load_source(self.current_source, False, 0)
        else:
            if self.play_state == 'STOPPED':
                self._load_source(self.current_source, False, 0)
            else:
                self.player.set('pause', False)
        if self.player:
            self.player.paused = False
        self._set_play_state('PLAYING')

    def toggle_play_pause(self):
        if self.play_state == 'PAUSED': self.play()
        else: self.pause()

    def pause(self):
        if not self.current_source:
            self.status.configure(text='SELECT A VIDEO', fg=self.owner.palette['muted'])
            return
        if self.player:
            self.player.set('pause', True); self.player.paused = True
        self._set_play_state('PAUSED')

    def stop_playback(self):
        if self.player: self.player.command(['stop'])
        self._set_play_state('STOPPED')
        if hasattr(self, 'time_label'): self.time_label.configure(text='00:00 / --:--')
        if hasattr(self, 'seek_var'): self.seek_var.set(0)

    def toggle_mute(self):
        self.muted = not self.muted
        if self.player: self.player.set('mute', self.muted)
        label = 'UNMUTE' if self.muted else 'MUTE'
        self.mute_button.configure(text=label)
    def next_audio_track(self):
        if self.player:self.player.command(['cycle','aid'])

    def _volume_changed(self,value):
        self.volume=max(0,min(100,float(value)))
        if hasattr(self,'volume_label'):self.volume_label.configure(text=f'{int(self.volume)}%')
        if self.player:self.player.set('volume',self.volume)
        self.owner.tile_volume_changed(self,self.volume)
    def _volume_step(self,direction):
        step=int(self.owner.control.get('GUI_WHEEL_VOLUME_STEP',5));new=max(0,min(100,self.volume+direction*step));self.volume_var.set(new);self._volume_changed(new)
    def _volume_wheel(self,event):
        self._volume_step(1 if getattr(event,'delta',0)>0 else -1);return 'break'
    def _speed_selected(self,_event=None):
        try:self.speed=float(self.speed_var.get().rstrip('x'))
        except Exception:return
        if self.player:self.player.set('speed',self.speed)
        self.owner.tile_speed_changed(self,self.speed)
    def _speed_wheel_step(self,direction):
        vals=[float(x) for x in self.owner.control.get('GUI_SPEED_OPTIONS',[.25,.5,.75,1,1.25,1.5,2,4,8,16,30,60])]
        current=min(range(len(vals)),key=lambda i:abs(vals[i]-self.speed))
        nxt=max(0,min(len(vals)-1,current+(1 if direction>0 else -1)))
        self.speed_var.set(f'{vals[nxt]:g}x');self._speed_selected()
    def _speed_wheel(self,event):
        d=1 if getattr(event,'delta',0)>0 else -1;self._speed_wheel_step(d);return 'break'
    def _fps_selected(self,_event=None):
        self.fps=self.fps_var.get()
        if self.player:self.player.set_fps_cap(self.fps)
    def _seek_changed(self,_value=None):
        if self._updating_seek:return
    def _seek_release(self,_event=None):
        self._seek_dragging=False
        if self.player:
            dur=self.player.get('duration',None)
            if dur:self.player.seek(float(dur)*float(self.seek_var.get())/100.0)
    def _video_wheel(self,direction):
        if self.kind=='playback' and self.player:
            step=float(self.owner.control.get('GUI_WHEEL_SEEK_SECONDS',5));pos=float(self.player.get('time-pos',0) or 0)
            self.player.seek(max(0,pos+direction*step))
    def _video_wheel_event(self,event):
        self._video_wheel(1 if getattr(event,'delta',0)>0 else -1);return 'break'
    def _playback_wall_timestamp(self):
        if self.kind == 'live':
            return datetime.now().astimezone().timestamp()
        if not self.current_record:
            return None
        pos=float(self.player.get('time-pos',0) or 0) if self.player else 0.0
        time_map=self.current_record.get('time_map') or []
        if time_map:
            for item in time_map:
                a=float(item.get('output_start_seconds',0));b=float(item.get('output_end_seconds',0))
                if a <= pos <= b:
                    source_start=datetime.fromisoformat(item['source_start']).timestamp()
                    return source_start + (pos-a)*float(item.get('speed',1.0))
        return float(self.current_record.get('start_ts',0))+pos

    def _cue_mark(self,which):
        value=self._playback_wall_timestamp()
        if value is None:
            messagebox.showinfo('Cue', 'Select and play a recording before marking a cue.', parent=self.owner.root)
            return
        if which=='in':self.cue_in_ts=value
        else:self.cue_out_ts=value
        a=datetime.fromtimestamp(self.cue_in_ts).astimezone().strftime('%Y-%m-%d %H:%M:%S') if self.cue_in_ts else 'not set'
        b=datetime.fromtimestamp(self.cue_out_ts).astimezone().strftime('%Y-%m-%d %H:%M:%S') if self.cue_out_ts else 'not set'
        self.footer.configure(text=f'CUE IN {a}  |  OUT {b}')

    def _save_marked_cue(self):
        if self.cue_in_ts is None or self.cue_out_ts is None or self.cue_out_ts <= self.cue_in_ts:
            messagebox.showwarning('Cue range','Mark an IN point and an OUT point, with OUT after IN.',parent=self.owner.root)
            return
        label=simpledialog.askstring('Save protected cue','Cue name:',initialvalue='Protected event',parent=self.owner.root)
        if label is None:return
        comment=simpledialog.askstring('Cue comment','Visible comment (optional):',initialvalue='',parent=self.owner.root)
        if comment is None:comment=''
        camera_id=self.current_record.get('camera_id',self.camera_id) if self.current_record else self.camera_id
        start=datetime.fromtimestamp(self.cue_in_ts).astimezone().isoformat(timespec='seconds')
        end=datetime.fromtimestamp(self.cue_out_ts).astimezone().isoformat(timespec='seconds')
        try:
            rpc('bookmarks.save',{'camera_id':camera_id,'camera_ids':[camera_id],'start':start,'end':end,'label':label,'notes':comment,'protected':True})
            self.cue_in_ts=None;self.cue_out_ts=None
            self.owner._refresh_bookmarks_cache()
            self.footer.configure(text='Cue saved — footage protected from purge.')
        except Exception as exc:messagebox.showerror('Save cue',str(exc),parent=self.owner.root)

    def _show_cue_menu(self):
        menu=tk.Menu(self.frame,tearoff=0,bg=self.owner.palette['panel'],fg=self.owner.palette['text'],activebackground=self.owner.palette['button'],activeforeground=self.owner.palette['accent'])
        menu.add_command(label='Mark cue IN at current video time',command=lambda:self._cue_mark('in'))
        menu.add_command(label='Mark cue OUT at current video time',command=lambda:self._cue_mark('out'))
        menu.add_command(label='Save marked range as protected cue…',command=self._save_marked_cue)
        menu.add_separator()
        menu.add_command(label='Open Cue Library',command=lambda:self.owner.show_page('Cue Library'))
        cues=self.owner._refresh_bookmarks_cache()
        if cues:
            menu.add_separator()
            for cue in cues[-30:]:
                title=f"{cue.get('camera_id','')} · {cue.get('label','Cue')}"
                if cue.get('notes'):title += ' — '+cue['notes'][:45]
                menu.add_command(label=title,command=lambda item=cue:self.owner._open_cue_on_tile(self,item))
        menu.tk_popup(self.cue_button.winfo_rootx(),self.cue_button.winfo_rooty()+self.cue_button.winfo_height())

    def destroy(self):
        if self._stats_after is not None:
            try:self.owner.root.after_cancel(self._stats_after)
            except tk.TclError:pass
            self._stats_after=None
        self.stats_visible=False
        self.stop_player()
        try:
            if self.frame.winfo_exists():self.frame.destroy()
        except tk.TclError:
            pass


class CCTVDesktop:
    def __init__(self, root):
        self.root = root
        self.control = self._read_literal_control()
        self.workspace_file = ROOT / 'config' / 'workspace.json'
        self.workspace_state = load_workspace(self.workspace_file)
        saved_live = self.workspace_state.get('live_camera_enabled', {})
        self.live_camera_enabled = {str(k): bool(v) for k, v in saved_live.items()} if isinstance(saved_live, dict) else {}
        self._workspace_save_after = None
        self._tk_callback_registry = {}
        self._tk_original_register = None
        self._tk_original_widget_init = None
        self._inspector_bindtag = f'eYeYaYeInspector_{id(self):x}'
        self._install_inspector_callback_tracking()
        self.inspector_mode = False
        self.inspector_window = None
        self.inspector_text = None
        self.inspector_preview = None
        self.inspector_button = None
        self.root.title('eYeYaYe — Native Desktop')
        self.root.geometry(self.workspace_state['window_geometry'])
        self.root.minsize(1050, 680)
        self.appearance = self._safe_rpc('appearance.get', {}, fallback={'preset':'cyberpunk','colors':{},'effects':{}})
        self.palette = self._palette_for(self.appearance)
        self.cameras = []
        self.recordings = []
        self.bookmarks = []
        self.timeline_sync_active = False
        self.timeline_cursor_ts = 0.0
        self._timeline_after = None
        self._timeline_transitioning = False
        self.video_tiles = []
        self.fullscreen_session = None
        self.mpv_binary = shutil.which(str(self.control.get('GUI_MPV_BINARY','/usr/bin/mpv'))) or shutil.which('mpv')
        self.page = '__initializing__'
        self.playing = False
        self.play_ts = 0.0
        self.play_start_ts = 0.0
        self.play_end_ts = 0.0
        self.play_speed = 1.0
        self.play_last_tick = time.monotonic()
        self.play_token = 0
        self.refresh_busy = False
        # Fetch the engine camera list before constructing the live/playback grids.
        # Building the page first would create empty grids on every first launch.
        self.page = '__initializing__'
        self._style()
        self._init_timeline_day()
        self._build_shell()
        self.refresh_all()
        restored_page = self.workspace_state.get('page', 'Live & Playback')
        self.show_page(restored_page)
        self.root.after(1800, self._auto_refresh)
        self.root.protocol('WM_DELETE_WINDOW', self.close)
        self.root.bind('<Configure>', self._root_configure, add='+')

    def _read_literal_control(self):
        import ast
        out = {}
        try:
            tree = ast.parse((ROOT / 'USER_CONTROL.py').read_text(encoding='utf-8'))
            for node in tree.body:
                if isinstance(node, (ast.Assign, ast.AnnAssign)):
                    try: val = ast.literal_eval(node.value)
                    except Exception: continue
                    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                    for target in targets:
                        if isinstance(target, ast.Name): out[target.id] = val
        except Exception:
            pass
        return out

    def _safe_rpc(self, action, params=None, fallback=None):
        try: return rpc(action, params)
        except Exception as exc:
            log_error(exc)
            return fallback if fallback is not None else {'error': str(exc)}

    def _install_inspector_callback_tracking(self):
        import tkinter as _tk
        original_register = _tk.Misc._register
        original_widget_init = _tk.BaseWidget.__init__
        self._tk_original_register = original_register
        self._tk_original_widget_init = original_widget_init
        owner = self

        def tracked_register(widget, func, subst=None, needcleanup=1):
            name = original_register(widget, func, subst, needcleanup)
            owner._tk_callback_registry[name] = (func, widget)
            return name

        def tracked_widget_init(widget, master, widgetName, cnf={}, kw={}, extra=()):
            original_widget_init(widget, master, widgetName, cnf, kw, extra)
            if owner.inspector_mode:
                owner._inspector_tag_widget(widget)

        _tk.Misc._register = tracked_register
        _tk.BaseWidget.__init__ = tracked_widget_init

    def _restore_inspector_callback_tracking(self):
        import tkinter as _tk
        original_register = getattr(self, '_tk_original_register', None)
        original_widget_init = getattr(self, '_tk_original_widget_init', None)
        if original_register is not None:
            _tk.Misc._register = original_register
            self._tk_original_register = None
        if original_widget_init is not None:
            _tk.BaseWidget.__init__ = original_widget_init
            self._tk_original_widget_init = None

    def _inspector_tag_widget(self, widget):
        try:
            tags = tuple(widget.bindtags())
            if self._inspector_bindtag not in tags:
                widget.bindtags((self._inspector_bindtag,) + tags)
        except tk.TclError:
            pass

    def _inspector_tag_tree(self, widget, enabled=True):
        try:
            tags = tuple(widget.bindtags())
            if enabled and self._inspector_bindtag not in tags:
                widget.bindtags((self._inspector_bindtag,) + tags)
            elif not enabled and self._inspector_bindtag in tags:
                widget.bindtags(tuple(tag for tag in tags if tag != self._inspector_bindtag))
            children = widget.winfo_children()
        except tk.TclError:
            return
        for child in children:
            self._inspector_tag_tree(child, enabled)

    def _inspector_release_event(self, event):
        return None if self._inspector_is_internal(event.widget) else 'break'

    def toggle_inspector(self):
        self.inspector_mode = not self.inspector_mode
        if self.inspector_mode:
            self.inspector_button.configure(text='INSPECTOR ON', bg=self.palette['button'], fg=self.palette['accent'])
            self._inspector_preview('INSPECTION ACTIVE\nClick any control or area to inspect its owning command and source.')
            self.root.bind_class(self._inspector_bindtag, '<ButtonPress-1>', self._inspector_pick_event)
            self.root.bind_class(self._inspector_bindtag, '<ButtonRelease-1>', self._inspector_release_event)
            self._inspector_tag_tree(self.root, True)
        else:
            self.inspector_button.configure(text='INSPECTOR OFF', bg=self.palette['raised'], fg=self.palette['accent'])
            self._inspector_preview('Inspector off. Activate it to inspect application controls and code.')
            try:
                self.root.unbind_class(self._inspector_bindtag, '<ButtonPress-1>')
                self.root.unbind_class(self._inspector_bindtag, '<ButtonRelease-1>')
            except tk.TclError:
                pass
            self._inspector_tag_tree(self.root, False)

    def _inspector_preview(self, text, expanded=None):
        widget = self.inspector_preview
        if widget is None: return
        if expanded is None:
            expanded = str(text).startswith(('INSPECTED:', 'WIDGET:', 'DRY RUN'))
        try:
            widget.configure(height=14 if expanded else 7, state='normal')
            widget.delete('1.0','end'); widget.insert('1.0',text); widget.configure(state='disabled')
        except tk.TclError: pass

    def _inspector_is_internal(self, widget):
        current = widget
        while current is not None:
            if current in (self.inspector_button, self.inspector_preview): return True
            if self.inspector_window is not None:
                try:
                    if current == self.inspector_window or str(current).startswith(str(self.inspector_window) + '.'):
                        return True
                except Exception: pass
            try:
                parent = current.winfo_parent()
                if not parent: return False
                current = current.nametowidget(parent)
            except Exception:
                return False
        return False

    def _inspector_pick_event(self, event):
        if not self.inspector_mode or self._inspector_is_internal(event.widget):
            return None
        self._open_inspector_for_widget(event.widget)
        return 'break'

    def _inspector_widget_candidates(self, widget):
        from inspector import target_for
        found = []
        seen = set()
        current = widget
        depth = 0
        while current is not None and depth < 12:
            for _name, (callback, registered_widget) in list(self._tk_callback_registry.items()):
                if registered_widget is current:
                    target = target_for(callback, ROOT)
                    if target:
                        key=(str(target['path']),target['name'],target['start_line'])
                        if key not in seen: seen.add(key); found.append(target)
            try:
                parent = current.winfo_parent()
                if not parent: break
                current = current.nametowidget(parent)
            except Exception: break
            depth += 1
        if not found:
            page_methods = {
                'Live & Playback':'page_live','Cameras':'page_cameras','Cue Library':'page_bookmarks',
                'Bookmarks':'page_bookmarks','Export Studio':'page_export_studio','Archives':'page_archives',
                'Detection Zones':'page_zones','Appearance':'page_appearance','Commands':'page_commands',
                'Settings':'page_settings','Logs':'page_logs','Maintenance':'page_maintenance'}
            method_name = page_methods.get(self.page)
            if method_name:
                target = target_for(getattr(self, method_name), ROOT)
                if target: found.append(target)
        return found

    def _open_inspector_for_widget(self, widget):
        from inspector import dry_run, commit_target
        candidates = self._inspector_widget_candidates(widget)
        widget_class = widget.winfo_class()
        try: widget_path = str(widget)
        except Exception: widget_path = repr(widget)
        if not candidates:
            self._inspector_preview(f'WIDGET: {widget_class}\nPATH: {widget_path}\nPAGE: {self.page}\nNo project-owned Python callback could be resolved.', expanded=True)
            messagebox.showinfo('Inspector', 'This element has no directly editable project callback. Its widget identity and page are shown in the sidebar.', parent=self.root)
            self._inspector_preview('INSPECTOR ON\nClick another application element to inspect its real command and source.', expanded=False)
            return

        win = tk.Toplevel(self.root)
        self.inspector_window = win
        win.title('eYeYaYe — Authoritative Source Inspector')
        win.geometry('1080x760')
        win.minsize(820,560)
        win.configure(bg=self.palette['bg'])
        win.transient(self.root)
        try: win.attributes('-topmost', True)
        except tk.TclError: pass
        win.grab_set()

        header = ttk.Frame(win, padding=(14,10)); header.pack(fill='x')
        ttk.Label(header,text='AUTHORITATIVE SOURCE INSPECTOR',style='Title.TLabel').pack(anchor='w')
        meta = ttk.Label(header,text=f'WIDGET: {widget_class}   |   PATH: {widget_path}   |   PAGE: {self.page}',style='Muted.TLabel')
        meta.pack(anchor='w',pady=(5,0))
        selector_row = ttk.Frame(win,padding=(14,0,14,8)); selector_row.pack(fill='x')
        labels=[f"{item['name']}  —  {item['path'].relative_to(ROOT)}:{item['start_line']}" for item in candidates]
        selected_label=tk.StringVar(value=labels[0])
        if len(candidates)>1:
            ttk.Label(selector_row,text='Command / code section:').pack(side='left')
            combo=ttk.Combobox(selector_row,textvariable=selected_label,values=labels,state='readonly',width=110)
            combo.pack(side='left',fill='x',expand=True,padx=8)
        else:
            ttk.Label(selector_row,text=labels[0],style='Muted.TLabel').pack(side='left')

        info = ttk.Label(win,text='Edit the real source function below. OK commits a syntax-checked change with a backup. Restart Everything commits valid pending edits and restarts the engine and GUI. Dry Run tests a temporary project copy without saving.',style='Muted.TLabel',wraplength=1020)
        info.pack(fill='x',padx=14,pady=(0,8))
        editor_frame=ttk.Frame(win,padding=(14,0,14,8)); editor_frame.pack(fill='both',expand=True)
        code_editor=tk.Text(editor_frame,wrap='none',undo=True,font=('DejaVu Sans Mono',10),
            bg=self.palette['panel'],fg=self.palette['text'],insertbackground=self.palette['accent'],
            selectbackground=self.palette['button'],relief='flat',padx=10,pady=9)
        yscroll=ttk.Scrollbar(editor_frame,orient='vertical',command=code_editor.yview)
        xscroll=ttk.Scrollbar(editor_frame,orient='horizontal',command=code_editor.xview)
        code_editor.configure(yscrollcommand=yscroll.set,xscrollcommand=xscroll.set)
        code_editor.grid(row=0,column=0,sticky='nsew');yscroll.grid(row=0,column=1,sticky='ns');xscroll.grid(row=1,column=0,sticky='ew')
        editor_frame.rowconfigure(0,weight=1);editor_frame.columnconfigure(0,weight=1)
        state={'index':0,'target':candidates[0],'edited':False,'busy':False}

        def load_candidate(index):
            state['index']=index;state['target']=candidates[index];state['edited']=False
            target=candidates[index]
            code_editor.configure(state='normal');code_editor.delete('1.0','end');code_editor.insert('1.0',target['original'])
            code_editor.edit_reset();code_editor.edit_modified(False)
            footer_status.configure(text=f"SOURCE: {target['path'].relative_to(ROOT)}  |  LINES {target['start_line']}–{target['end_line']}  |  LIVE FILE IS UNCHANGED UNTIL OK / RESTART")
        def candidate_changed(_event=None):
            if code_editor.edit_modified():
                if not messagebox.askyesno('Discard staged edit?', 'Changing the inspected function will discard the current unsaved editor text.', parent=win):
                    selected_label.set(labels[state['index']]);return
            load_candidate(labels.index(selected_label.get()))
        if len(candidates)>1: combo.bind('<<ComboboxSelected>>',candidate_changed)

        footer=ttk.Frame(win,padding=(14,8));footer.pack(fill='x')
        footer_status=ttk.Label(footer,text='',style='Muted.TLabel');footer_status.pack(fill='x',pady=(0,8))
        buttons=ttk.Frame(footer);buttons.pack(fill='x')

        def get_edit(): return code_editor.get('1.0','end-1c')
        def close_window(preview_message=None):
            try: win.grab_release()
            except tk.TclError: pass
            try: win.destroy()
            except tk.TclError: pass
            self.inspector_window=None
            self._inspector_preview(preview_message or 'INSPECTOR ON\nClick another application element to inspect its real command and source.', expanded=False)
        def run_dry():
            if state['busy']: return
            state['busy']=True
            footer_status.configure(text='DRY RUN: copying staged project, compiling all Python files, and running the test suite…')
            for child in buttons.winfo_children(): child.configure(state='disabled')
            edited=get_edit(); target=state['target']
            def worker():
                try: result=dry_run(ROOT,target,edited)
                except Exception as exc: result={'ok':False,'phase':'dry-run','message':f'{type(exc).__name__}: {exc}','python_files':0,'tests':'NOT RUN'}
                def done():
                    state['busy']=False
                    for child in buttons.winfo_children(): child.configure(state='normal')
                    message=f"DRY RUN {'PASSED' if result.get('ok') else 'FAILED'}\nPhase: {result.get('phase')}\nPython files checked: {result.get('python_files')}\nTests: {result.get('tests')}\n\n{result.get('message','')}"
                    footer_status.configure(text=f"DRY RUN {'PASSED' if result.get('ok') else 'FAILED'} — live project files were not changed.")
                    self._inspector_preview(message[:1600])
                    report=tk.Toplevel(win);report.title('Inspector Dry Run Result');report.geometry('900x600');report.transient(win)
                    report.grab_set()
                    text_box=tk.Text(report,wrap='word',font=('DejaVu Sans Mono',9),bg=self.palette['panel'],fg=self.palette['text'])
                    text_box.pack(fill='both',expand=True,padx=10,pady=10);text_box.insert('1.0',message);text_box.configure(state='disabled')
                    ttk.Button(report,text='OK',command=report.destroy).pack(anchor='e',padx=12,pady=(0,10))
                try: self.root.after(0,done)
                except tk.TclError: pass
            threading.Thread(target=worker,daemon=True,name='inspector-dry-run').start()

        def commit(close_after=True):
            if not code_editor.edit_modified():
                if close_after:
                    close_window('NO CHANGES\nThe inspected source was not modified.')
                return True
            try:
                backup=commit_target(ROOT,state['target'],get_edit())
                saved_message = (f"SAVED: {state['target']['path'].relative_to(ROOT)}\nBACKUP: {backup.relative_to(ROOT)}\nRestart Everything to load this source change into the running program.")
                footer_status.configure(text=f"COMMITTED. Backup: {backup.relative_to(ROOT)}")
                if close_after: close_window(saved_message)
                else: self._inspector_preview(saved_message, expanded=False)
                return True
            except Exception as exc:
                messagebox.showerror('Inspector — commit rejected',f'{type(exc).__name__}: {exc}',parent=win)
                return False

        def restart_everything():
            if state['busy']: return
            if code_editor.edit_modified() and not messagebox.askyesno('Restart Everything', 'Save this inspected source change, then stop and restart the complete eYeYaYe engine and GUI?', parent=win):
                return
            if code_editor.edit_modified():
                if not commit(close_after=False): return
            elif not messagebox.askyesno('Restart Everything', 'Stop and restart the complete eYeYaYe engine, camera bridge, recorders, MediaMTX and GUI?', parent=win):
                return
            close_window('RESTART QUEUED\nThe master terminal is restarting the complete eYeYaYe stack.')
            self.request_master_restart()

        ttk.Button(buttons,text='OK',style='Accent.TButton',command=commit).pack(side='left',padx=(0,7))
        ttk.Button(buttons,text='Cancel',command=close_window).pack(side='left',padx=7)
        ttk.Button(buttons,text='Restart Everything',style='Danger.TButton',command=restart_everything).pack(side='left',padx=7)
        ttk.Button(buttons,text='Dry Run',command=run_dry).pack(side='right',padx=(7,0))
        load_candidate(0)
        selected_source = candidates[0]['original']
        if len(selected_source) > 2200: selected_source = selected_source[:2200] + '\n… [preview truncated; full source is in the editor]'
        self._inspector_preview(f'INSPECTED: {widget_class}\nPAGE: {self.page}\nSOURCE: {labels[0]}\n\n{selected_source}', expanded=True)
        win.protocol('WM_DELETE_WINDOW',close_window)

    def request_master_restart(self):
        # Ask the master-terminal owner to restart engine, streams and GUI together.
        import socket as _socket
        control_socket = ROOT / 'runtime' / 'master-control.sock'
        try:
            with _socket.socket(_socket.AF_UNIX,_socket.SOCK_STREAM) as client:
                client.settimeout(4)
                client.connect(str(control_socket))
                client.sendall(b'{"action":"restart"}\n')
                response=client.recv(4096).decode('utf-8','replace')
            if 'queued' not in response.lower(): raise RuntimeError(response or 'Master did not acknowledge the restart request.')
            self.close()
        except Exception as exc:
            messagebox.showerror('Restart Everything',f'Could not reach the master lifecycle owner: {exc}\n\nUse the master terminal command: restart',parent=self.root)

    @staticmethod
    def _palette_for(appearance):
        preset = appearance.get('preset', 'cyberpunk') if isinstance(appearance, dict) else 'cyberpunk'
        palette = dict(PALETTES.get(preset, PALETTES['cyberpunk']))
        colors = appearance.get('colors', {}) if isinstance(appearance, dict) else {}
        mapping = {
            'bg': 'background', 'panel': 'surface', 'raised': 'surface_raised',
            'text': 'text', 'muted': 'muted', 'accent': 'accent',
            'accent2': 'accent_alt', 'ok': 'success', 'warn': 'warning',
            'bad': 'danger', 'button': 'button_primary_bg',
        }
        for palette_key, config_key in mapping.items():
            value = colors.get(config_key)
            if isinstance(value, str) and len(value) == 7 and value.startswith('#'):
                palette[palette_key] = value
        return palette

    def _style(self):
        p = self.palette
        self.root.configure(bg=p['bg'])
        style = ttk.Style(self.root)
        try: style.theme_use('clam')
        except tk.TclError: pass
        style.configure('.', background=p['bg'], foreground=p['text'], fieldbackground=p['panel'], bordercolor=p['raised'], font=('DejaVu Sans', 10))
        style.configure('TFrame', background=p['bg'])
        style.configure('Panel.TFrame', background=p['panel'])
        style.configure('TLabel', background=p['bg'], foreground=p['text'])
        style.configure('Muted.TLabel', background=p['bg'], foreground=p['muted'])
        style.configure('Title.TLabel', background=p['bg'], foreground=p['accent'], font=('DejaVu Sans', 18, 'bold'))
        style.configure('PanelTitle.TLabel', background=p['panel'], foreground=p['accent'], font=('DejaVu Sans', 11, 'bold'))
        style.configure('TButton', background=p['raised'], foreground=p['text'], borderwidth=1, focusthickness=2, focuscolor=p['accent'], padding=(10, 7))
        style.map('TButton', background=[('active', p['button']), ('pressed', p['accent2'])], foreground=[('disabled', p['muted'])])
        style.configure('Accent.TButton', background=p['button'], foreground=p['accent'], font=('DejaVu Sans', 10, 'bold'))
        style.configure('Danger.TButton', background=p['bad'], foreground='#ffffff')
        style.configure('TEntry', fieldbackground=p['panel'], foreground=p['text'], insertcolor=p['accent'])
        style.configure('TCombobox', fieldbackground=p['panel'], background=p['raised'], foreground=p['text'], arrowcolor=p['accent'])
        style.map('TCombobox', fieldbackground=[('readonly',p['panel'])], foreground=[('readonly',p['text'])])
        style.configure('Treeview', background=p['panel'], fieldbackground=p['panel'], foreground=p['text'], rowheight=28, borderwidth=0)
        style.configure('Treeview.Heading', background=p['raised'], foreground=p['accent'], font=('DejaVu Sans', 9, 'bold'))
        style.map('Treeview', background=[('selected',p['button'])], foreground=[('selected',p['text'])])
        style.configure('TNotebook', background=p['bg'], borderwidth=0)
        style.configure('TNotebook.Tab', background=p['raised'], foreground=p['text'], padding=(12,7))
        style.map('TNotebook.Tab', background=[('selected',p['button'])], foreground=[('selected',p['accent'])])

    def _timeline_timezone(self):
        from zoneinfo import ZoneInfo
        configured = str(self.control.get('ARCHIVE_TIMEZONE', 'local'))
        if configured != 'local':
            try: return ZoneInfo(configured)
            except Exception: pass
        env_name = os.environ.get('TZ')
        if env_name:
            try: return ZoneInfo(env_name)
            except Exception: pass
        # V4's Compose default is Europe/Paris; use the same zone in the host GUI.
        try: return ZoneInfo('Europe/Paris')
        except Exception: return datetime.now().astimezone().tzinfo

    def _init_timeline_day(self):
        day_start = str(self.control.get('ARCHIVE_DAY_START', '05:00'))
        now = datetime.now(self._timeline_timezone())
        self.timeline_day = archive_day_for(now, day_start)
        start, _end = day_window(self.timeline_day, day_start, self._timeline_timezone())
        self.timeline_cursor_ts = start.timestamp()

    def _timeline_window(self):
        day_start = str(self.control.get('ARCHIVE_DAY_START', '05:00'))
        start, end = day_window(self.timeline_day, day_start, self._timeline_timezone())
        return start.timestamp(), end.timestamp()

    def _timeline_shift_day(self, amount):
        self.timeline_day = self.timeline_day + timedelta(days=int(amount))
        if hasattr(self, 'timeline_day_var'):
            self.timeline_day_var.set(self.timeline_day.isoformat())
        start, _end = self._timeline_window()
        self.timeline_cursor_ts = start
        self._timeline_redraw()

    def _timeline_load_day(self):
        try:
            selected = date.fromisoformat(self.timeline_day_var.get().strip())
            self.timeline_day = selected
        except (ValueError, AttributeError):
            messagebox.showerror('Playback timeline', 'Enter the archive day as YYYY-MM-DD.', parent=self.root)
            return
        start, _end = self._timeline_window()
        self.timeline_cursor_ts = start
        self._timeline_redraw()

    def _timeline_redraw(self):
        canvas = getattr(self, 'timeline_canvas', None)
        if canvas is None:
            return
        try:
            if not canvas.winfo_exists():
                return
            canvas.delete('all')
            width = max(260, canvas.winfo_width())
            start, end = self._timeline_window()
            if end <= start:
                return
            cams = [c for c in self.cameras if c.get('enabled', True)]
            height = max(74, 30 + len(cams) * 18)
            canvas.configure(height=height)
            left, right = 76, width - 12
            chart_w = max(1, right - left)
            canvas.create_rectangle(left, 4, right, height - 5,
                fill=self.palette['bg'], outline=self.palette['raised'])
            # Local archive-day ticks; labels show the actual local clock.
            window_start = datetime.fromtimestamp(start, self._timeline_timezone())
            window_end = datetime.fromtimestamp(end, self._timeline_timezone())
            if hasattr(self, 'timeline_range_label') and self.timeline_range_label.winfo_exists():
                self.timeline_range_label.configure(
                    text=f"{window_start.strftime('%d/%m %H:%M')} → {window_end.strftime('%d/%m %H:%M')}")
            for hour in range(0, 25, 3):
                tick_ts = start + (end - start) * (hour / 24.0)
                x = left + chart_w * hour / 24.0
                canvas.create_line(x, 16, x, height - 6, fill=self.palette['raised'])
                tick = datetime.fromtimestamp(tick_ts, self._timeline_timezone()).strftime('%H:%M')
                canvas.create_text(x, 2, text=tick, anchor='n', fill=self.palette['muted'],
                                   font=('DejaVu Sans Mono', 7))
            colors = {'RAW': self.palette['accent'], 'MER': self.palette['ok'],
                      'TLR': self.palette['warn'], 'TLI': self.palette['accent2']}
            for row, cam in enumerate(cams):
                y = 24 + row * 18
                cid = str(cam.get('id', ''))
                code = self._camera_code_for_timeline(cid)
                label = code
                if cam.get('clock_warning'):
                    offset = int(cam.get('clock_offset_seconds', 0) or 0)
                    label += f' ! {offset:+d}s'
                canvas.create_text(4, y + 5, text=label, anchor='w',
                    fill=self.palette['warn'] if cam.get('clock_warning') else self.palette['text'],
                    font=('DejaVu Sans Mono', 8, 'bold'))
                canvas.create_line(left, y + 12, right, y + 12, fill=self.palette['raised'])
                for record in self.recordings:
                    if str(record.get('camera_id')) != cid:
                        continue
                    kind = str(record.get('type_code', '')).upper()
                    for a, b in record_source_intervals(record):
                        a2, b2 = max(a, start), min(b, end)
                        if b2 <= a2:
                            continue
                        x1 = left + chart_w * (a2 - start) / (end - start)
                        x2 = left + chart_w * (b2 - start) / (end - start)
                        if x2 - x1 < 2: x2 = x1 + 2
                        canvas.create_rectangle(x1, y + 2, min(right, x2), y + 11,
                            fill=colors.get(kind, self.palette['muted']), outline='')
            cursor = max(start, min(end, self.timeline_cursor_ts))
            cursor_x = left + chart_w * (cursor - start) / (end - start)
            canvas.create_line(cursor_x, 13, cursor_x, height - 5,
                fill='#ffffff', width=2, tags=('cursor',))
            canvas.create_text(min(right - 2, cursor_x + 4), height - 6,
                text=datetime.fromtimestamp(cursor, self._timeline_timezone()).strftime('%H:%M:%S'),
                anchor='se', fill='#ffffff', font=('DejaVu Sans Mono', 7))
        except (tk.TclError, ValueError, TypeError, OverflowError) as exc:
            log_error(exc)

    @staticmethod
    def _camera_code_for_timeline(camera_id):
        match = re.fullmatch(r'cam(\d+)', str(camera_id).lower())
        return f'C{match.group(1)}' if match else str(camera_id).upper()

    def _timeline_click(self, event):
        canvas = getattr(self, 'timeline_canvas', None)
        if canvas is None:
            return
        start, end = self._timeline_window()
        left, right = 76, max(77, canvas.winfo_width() - 12)
        fraction = max(0.0, min(1.0, (event.x - left) / max(1, right - left)))
        self._timeline_seek_all(start + fraction * (end - start), autoplay=True)

    def _timeline_seek_all(self, wall_time, autoplay=True):
        requested = float(wall_time)
        self._timeline_update_date_for_timestamp(requested)
        start, end = self._timeline_window()
        target = max(start, min(end - 0.001, requested))
        self.timeline_sync_active = bool(autoplay)
        self.timeline_cursor_ts = target
        self._timeline_transitioning = False
        for tile in self.play_tiles.values():
            choice = choose_record(self.recordings, tile.camera_id, target)
            if choice is None:
                tile.mark_no_footage()
                continue
            record, position = choice
            tile.load_recording(record, position=position, autoplay=autoplay)
        self._timeline_update_date_for_timestamp(target)
        self._timeline_redraw()
        if autoplay:
            self._timeline_schedule_poll()

    def _timeline_update_date_for_timestamp(self, wall_time):
        start, end = self._timeline_window()
        if wall_time < start or wall_time >= end:
            dt = datetime.fromtimestamp(wall_time, self._timeline_timezone())
            self.timeline_day = archive_day_for(dt, str(self.control.get('ARCHIVE_DAY_START', '05:00')))
            if hasattr(self, 'timeline_day_var'):
                self.timeline_day_var.set(self.timeline_day.isoformat())

    def _timeline_play_all(self):
        if not self.play_tiles:
            return
        if not any(tile.current_source for tile in self.play_tiles.values()):
            self._timeline_seek_all(self.timeline_cursor_ts or self._timeline_window()[0], autoplay=True)
            return
        self.timeline_sync_active = True
        for tile in self.play_tiles.values():
            if tile.current_source:
                tile.play()
        self._timeline_schedule_poll()

    def _timeline_pause_all(self):
        self.timeline_sync_active = False
        for tile in self.play_tiles.values():
            if tile.current_source:
                tile.pause()

    def _timeline_stop_all(self):
        self.timeline_sync_active = False
        for tile in self.play_tiles.values():
            tile.stop_playback()

    def _timeline_schedule_poll(self):
        if self._timeline_after is None:
            try: self._timeline_after = self.root.after(400, self._timeline_poll)
            except tk.TclError: self._timeline_after = None

    def _timeline_poll(self):
        self._timeline_after = None
        if not self.timeline_sync_active:
            return
        active = next((tile for tile in self.play_tiles.values()
                       if tile.current_source and tile.play_state == 'PLAYING' and tile.player and tile.player.connected()), None)
        if active:
            timestamp = active._playback_wall_timestamp()
            if timestamp is not None:
                self.timeline_cursor_ts = float(timestamp)
                self._timeline_update_date_for_timestamp(self.timeline_cursor_ts)
                if hasattr(self, 'timeline_day_var'):
                    self.timeline_day_var.set(self.timeline_day.isoformat())
                self._timeline_redraw()
        if self.timeline_sync_active:
            self._timeline_schedule_poll()

    def _timeline_continue_after_eof(self, tile):
        if not self.timeline_sync_active or self._timeline_transitioning:
            return False
        if not tile.current_record:
            return False
        intervals = record_source_intervals(tile.current_record)
        if not intervals:
            return False
        next_wall = max(b for _a, b in intervals)
        # Find the next available wall-clock point across all cameras, including
        # the next archive-day window. This keeps the 24-hour timeline moving.
        upcoming = []
        for record in self.recordings:
            for a, b in record_source_intervals(record):
                if b > next_wall + 0.01:
                    upcoming.append(max(a, next_wall))
        if not upcoming:
            return False
        target = min(upcoming)
        self._timeline_transitioning = True
        self.root.after(50, lambda: self._timeline_seek_all(target, autoplay=True))
        return True

    def _build_shell(self):
        shell = ttk.Frame(self.root); shell.pack(fill='both', expand=True)
        self.sidebar = tk.Frame(shell, bg=self.palette['panel'], width=205); self.sidebar.pack(side='left', fill='y'); self.sidebar.pack_propagate(False)
        tk.Label(self.sidebar, text='eYeYaYe', bg=self.palette['panel'], fg=self.palette['accent'], font=('DejaVu Sans', 18, 'bold'), justify='left').pack(anchor='w', padx=18, pady=(20,6))
        tk.Label(self.sidebar, text='NATIVE DESKTOP', bg=self.palette['panel'], fg=self.palette['muted'], font=('DejaVu Sans Mono', 8)).pack(anchor='w', padx=19, pady=(0,18))
        self.nav_buttons = {}
        for name in ('Live & Playback','Cameras','Cue Library','Export Studio','Archives','Detection Zones','Appearance','Commands','Settings','Logs','Maintenance'):
            b=tk.Button(self.sidebar,text=name,anchor='w',relief='flat',bd=0,bg=self.palette['panel'],fg=self.palette['text'],activebackground=self.palette['raised'],activeforeground=self.palette['accent'],font=('DejaVu Sans',10),padx=18,pady=10,command=lambda n=name:self.show_page(n))
            b.pack(fill='x',padx=7,pady=1); self.nav_buttons[name]=b
        self.inspector_preview = tk.Text(self.sidebar, height=7, width=24, wrap='word',
            bg=self.palette['bg'], fg=self.palette['muted'], insertbackground=self.palette['accent'],
            relief='flat', bd=0, padx=8, pady=7, font=('DejaVu Sans Mono',7), state='disabled')
        self.inspector_preview.pack(side='bottom', fill='x', padx=8, pady=(0,8))
        self.inspector_button = tk.Button(self.sidebar, text='INSPECTOR OFF', anchor='center',
            relief='flat', bd=0, bg=self.palette['raised'], fg=self.palette['accent'],
            activebackground=self.palette['button'], activeforeground=self.palette['text'],
            font=('DejaVu Sans',9,'bold'), padx=8, pady=8, command=self.toggle_inspector)
        self.inspector_button.pack(side='bottom', fill='x', padx=8, pady=(8,3))
        self.workspace = ttk.Frame(shell); self.workspace.pack(side='left',fill='both',expand=True)
        header=ttk.Frame(self.workspace); header.pack(fill='x',padx=20,pady=(16,8)); self.header=header
        self.page_title=ttk.Label(header,text='Live & Playback',style='Title.TLabel'); self.page_title.pack(side='left')
        self.status_label=ttk.Label(header,text='Connecting to engine…',style='Muted.TLabel'); self.status_label.pack(side='right')
        self.content_host = ttk.Frame(self.workspace)
        self.content_host.pack(fill='both', expand=True, padx=16, pady=(0,16))
        self.body = ttk.Frame(self.content_host)
        self.media_page = ttk.Frame(self.content_host)
        self._media_page_built = False
        self._fullscreen_session = None
        self.focused_tile = None
        self.root.bind_all('<KeyPress>', self._on_video_key, add='+')

    def _root_configure(self, event=None):
        if event is not None and event.widget is not self.root:
            return
        self._schedule_workspace_save()

    def _schedule_workspace_save(self):
        if not hasattr(self, 'root') or not self.root.winfo_exists():
            return
        if self._workspace_save_after is not None:
            try: self.root.after_cancel(self._workspace_save_after)
            except tk.TclError: pass
        self._workspace_save_after = self.root.after(650, self.persist_workspace)

    def persist_workspace(self):
        self._workspace_save_after = None
        try:
            geometry = self.root.winfo_geometry()
            if geometry and geometry.split('x', 1)[0].isdigit():
                self.workspace_state['window_geometry'] = geometry
            self.workspace_state['page'] = self.page if self.page != '__initializing__' else 'Live & Playback'
            if hasattr(self, 'show_live_var'):
                self.workspace_state['show_live'] = bool(self.show_live_var.get())
            if hasattr(self, 'show_playback_var'):
                self.workspace_state['show_playback'] = bool(self.show_playback_var.get())
            self.workspace_state['live_camera_enabled'] = dict(self.live_camera_enabled)
            if hasattr(self, 'split_orientation_var'):
                self.workspace_state['split_orientation'] = self.split_orientation_var.get()
            if hasattr(self, 'workspace_split'):
                try:
                    if len(self.workspace_split.panes()) > 1:
                        self.workspace_state['sash_position'] = int(self.workspace_split.sashpos(0))
                except (tk.TclError, ValueError):
                    pass
            for kind in ('live', 'playback'):
                prefix = 'play' if kind == 'playback' else 'live'
                col_var = getattr(self, f'{prefix}_cols_var', None)
                row_var = getattr(self, f'{prefix}_rows_var', None)
                if col_var is not None and row_var is not None:
                    try:
                        self.workspace_state[f'{kind}_grid'] = {'columns': int(col_var.get()), 'rows': int(row_var.get())}
                    except (ValueError, tk.TclError):
                        pass
            self.workspace_state = save_workspace(self.workspace_file, self.workspace_state)
        except (OSError, tk.TclError, ValueError) as exc:
            log_error(exc)

    def _apply_split_orientation(self, _event=None):
        orientation = self.split_orientation_var.get()
        if orientation not in ('side-by-side', 'stacked'):
            return
        try:
            self.workspace_split.configure(orient='vertical' if orientation == 'stacked' else 'horizontal')
            self.workspace_state['split_orientation'] = orientation
            self.root.after(250, self._restore_workspace_sash)
            self._schedule_workspace_save()
        except tk.TclError as exc:
            log_error(exc)

    def _restore_workspace_sash(self):
        try:
            if len(self.workspace_split.panes()) > 1:
                position = int(self.workspace_state.get('sash_position', 0))
                if position <= 0:
                    dimension = self.workspace_split.winfo_width() if self.split_orientation_var.get() == 'side-by-side' else self.workspace_split.winfo_height()
                    position = int(dimension * 0.64)
                self.workspace_split.sashpos(0, position)
        except (tk.TclError, ValueError):
            pass

    def _grid_variables(self, kind):
        prefix = 'play' if kind == 'playback' else 'live'
        return getattr(self, f'{prefix}_cols_var'), getattr(self, f'{prefix}_rows_var')

    def _apply_grid_layout(self, kind):
        try:
            col_var, row_var = self._grid_variables(kind)
            current = self.workspace_state.get(f'{kind}_grid', {'columns': 3, 'rows': 1})
            col_text, row_text = col_var.get().strip(), row_var.get().strip()
            cols = int(col_text, 10) if col_text else int(current.get('columns', 3))
            rows = int(row_text, 10) if row_text else int(current.get('rows', 1))
        except (ValueError, AttributeError, tk.TclError):
            messagebox.showwarning('Grid layout', 'Enter whole numbers for columns and rows.', parent=self.root)
            return
        if not (1 <= cols <= 32 and 1 <= rows <= 32) or cols * rows > 64:
            messagebox.showwarning('Grid layout', 'Use 1–32 columns and rows, with no more than 64 total tiles.', parent=self.root)
            return
        self.workspace_state[f'{kind}_grid'] = {'columns': cols, 'rows': rows}
        self.persist_workspace()
        self._rebuild_tile_group(kind)

    def clear_body(self):
        for child in self.body.winfo_children(): child.destroy()

    def show_page(self, name):
        if getattr(self, 'page', None) not in (None, '__initializing__'):
            self.persist_workspace()
        if self._fullscreen_session:
            self._exit_tile_fullscreen()
        self.page = name; self.workspace_state['page'] = name; self.page_title.configure(text=name)
        self._schedule_workspace_save()
        for n,b in self.nav_buttons.items():
            b.configure(bg=self.palette['button'] if n == name else self.palette['panel'], fg=self.palette['accent'] if n == name else self.palette['text'])
        if name in ('Live & Playback',):
            self.body.pack_forget()
            if not self._media_page_built:
                self.page_live()
                self._media_page_built = True
            self.media_page.pack(fill='both', expand=True)
            self._sync_tile_groups_if_needed()
            self._set_panels()
            return
        self.media_page.pack_forget()
        self.body.pack(fill='both', expand=True)
        self.clear_body()
        builders = {
            'Cameras':self.page_cameras, 'Cue Library':self.page_bookmarks, 'Bookmarks':self.page_bookmarks,
            'Export Studio':self.page_export_studio, 'Archives':self.page_archives, 'Detection Zones':self.page_zones,
            'Appearance':self.page_appearance, 'Commands':self.page_commands, 'Settings':self.page_settings,
            'Logs':self.page_logs, 'Maintenance':self.page_maintenance}
        builder = builders.get(name)
        if builder: builder()
        else: self.page_title.configure(text='Live & Playback'); self.show_page('Live & Playback')

    def refresh_all(self):
        state=self._safe_rpc('state.get',{},fallback={})
        if state.get('error'):
            self.status_label.configure(text='ENGINE OFFLINE — '+state['error'][:70]); return
        previous=[(c.get('id'),bool(c.get('enabled'))) for c in self.cameras]
        self.cameras=state.get('cameras',[])
        current=[(c.get('id'),bool(c.get('enabled'))) for c in self.cameras]
        self.status_label.configure(text=f"ENGINE {'ONLINE' if state.get('media_server') else 'STARTING'}  |  {state.get('disk_free_gb','?')} GB FREE  |  {state.get('original_segments',0)} ORIGINAL SEGMENTS")
        self.root.title(f"eYeYaYe — {'ONLINE' if state.get('media_server') else 'ENGINE STARTING'}")
        if previous != current and self._media_page_built:
            self._refresh_live_camera_switches()
            self._rebuild_tile_group('playback'); self._rebuild_tile_group('live')
        if self._media_page_built: self._refresh_camera_tile_status()
        elif self.page=='Cameras': self._refresh_camera_table()
        elif self.page in ('Cue Library','Bookmarks'): self._refresh_bookmarks()
        elif self.page=='Export Studio': self._refresh_export_cues()
        elif self.page=='Archives': self._refresh_recordings_table()

    def _auto_refresh(self):
        try:
            if not self.refresh_busy:
                self.refresh_busy=True
                threading.Thread(target=self._background_refresh,daemon=True).start()
        finally:self.root.after(max(3000,int(self.control.get('STATUS_REFRESH_SECONDS',5))*1000),self._auto_refresh)

    def _background_refresh(self):
        try: state=rpc('state.get',{})
        except Exception as exc: state={'error':str(exc)}
        def apply():
            self.refresh_busy=False
            if not self.root.winfo_exists(): return
            if state.get('error'):
                self.status_label.configure(text='ENGINE OFFLINE — '+state['error'][:70]); return
            previous=[(c.get('id'),bool(c.get('enabled'))) for c in self.cameras]
            self.cameras=state.get('cameras',[])
            current=[(c.get('id'),bool(c.get('enabled'))) for c in self.cameras]
            engine_state='ONLINE' if state.get('media_server') else 'MEDIA SERVER STARTING / ERROR'
            error=str(state.get('last_error','') or '')
            suffix=('  |  '+error[:110]) if error and not state.get('media_server') else ''
            self.status_label.configure(text=f"ENGINE {engine_state}  |  {state.get('disk_free_gb','?')} GB FREE  |  {state.get('original_segments',0)} ORIGINAL SEGMENTS{suffix}")
            if previous != current and self._media_page_built:
                self._refresh_live_camera_switches()
                self._rebuild_tile_group('playback'); self._rebuild_tile_group('live')
            if self._media_page_built: self._refresh_camera_tile_status()
        self.root.after(0,apply)

    def panel(self, parent, title=None):
        f=ttk.Frame(parent,style='Panel.TFrame',padding=10)
        if title: ttk.Label(f,text=title,style='PanelTitle.TLabel').pack(anchor='w',pady=(0,8))
        return f

    def page_live(self):
        if not self.mpv_binary:
            ttk.Label(self.media_page,text='Native video playback requires mpv. Install it once with: sudo apt install mpv',style='Muted.TLabel').pack(anchor='w',pady=12)
        top=ttk.Frame(self.media_page);top.pack(fill='x',pady=(0,8)); self.media_top=top
        ttk.Label(top,text='LIVE AND PLAYBACK — players stay connected while you browse sections',style='Muted.TLabel').pack(side='left')
        self.show_playback_var=tk.BooleanVar(value=bool(self.workspace_state.get('show_playback', True)))
        self.show_live_var=tk.BooleanVar(value=bool(self.workspace_state.get('show_live', True)))
        ttk.Label(top,text='VIDEO KEYS: Space play/pause · ←/→ ±5s · ↑/↓ ±60s · M mute · F fullscreen · I stats',style='Muted.TLabel').pack(side='left',padx=12)
        ttk.Checkbutton(top,text='Show Playback',variable=self.show_playback_var,command=self._set_panels).pack(side='right',padx=4)
        ttk.Checkbutton(top,text='Show Live Dock',variable=self.show_live_var,command=self._set_panels).pack(side='right',padx=4)
        ttk.Label(top,text='Panel arrangement').pack(side='right',padx=(8,3))
        self.split_orientation_var=tk.StringVar(value=self.workspace_state.get('split_orientation','side-by-side'))
        self.split_orientation_box=ttk.Combobox(top,textvariable=self.split_orientation_var,values=['side-by-side','stacked'],width=13,state='readonly')
        self.split_orientation_box.pack(side='right',padx=3)
        self.split_orientation_box.bind('<<ComboboxSelected>>',self._apply_split_orientation)
        self.live_switch_row = ttk.Frame(self.media_page)
        self.live_switch_row.pack(fill='x', pady=(0,7))
        self._refresh_live_camera_switches()
        master=ttk.Frame(self.media_page);master.pack(fill='x',pady=(0,8)); self.media_master=master
        self.link_controls_var=tk.BooleanVar(value=False)
        ttk.Checkbutton(master,text='Link tile controls',variable=self.link_controls_var).pack(side='left')
        ttk.Label(master,text='(per-tile LINK can opt out)',style='Muted.TLabel').pack(side='left',padx=(3,6))
        ttk.Label(master,text='Master volume').pack(side='left',padx=(12,3))
        self.master_volume_var=tk.DoubleVar(value=float(self.control.get('GUI_DEFAULT_TILE_VOLUME',80)))
        ttk.Scale(master,from_=0,to=100,variable=self.master_volume_var,command=self.apply_master_volume,length=130).pack(side='left')
        ttk.Button(master,text='Apply volume to all',command=lambda:self.apply_master_volume(self.master_volume_var.get())).pack(side='left',padx=4)
        ttk.Label(master,text='Master speed').pack(side='left',padx=(10,3))
        self.master_speed_var=tk.StringVar(value='1x')
        self.master_speed_box=ttk.Combobox(master,textvariable=self.master_speed_var,values=[f'{x:g}x' for x in self.control.get('GUI_SPEED_OPTIONS',[.25,.5,.75,1,1.25,1.5,2,4,8,16,30,60])],width=6,state='readonly')
        self.master_speed_box.pack(side='left')
        ttk.Button(master,text='Apply speed to playback',command=self.apply_master_speed).pack(side='left',padx=4)
        ttk.Label(master,text='Master FPS').pack(side='left',padx=(10,3))
        self.master_fps_var=tk.StringVar(value=str(self.control.get('GUI_DEFAULT_FPS','Auto')))
        self.master_fps_box=ttk.Combobox(master,textvariable=self.master_fps_var,values=self.control.get('GUI_FPS_OPTIONS',['Auto','15','25','30','50','60']),width=5,state='readonly')
        self.master_fps_box.pack(side='left')
        ttk.Button(master,text='Apply FPS to all',command=self.apply_master_fps).pack(side='left',padx=4)
        split_position=self.workspace_state.get('split_orientation','side-by-side')
        self.workspace_split=ttk.Panedwindow(self.media_page,orient='vertical' if split_position=='stacked' else 'horizontal')
        self.workspace_split.pack(fill='both',expand=True)
        self.workspace_split.bind('<ButtonRelease-1>',lambda _e:self._schedule_workspace_save())
        self.play_panel=self.panel(self.workspace_split,'PLAYBACK WALL')
        self.live_panel=self.panel(self.workspace_split,'LIVE DOCK')
        self.workspace_split.add(self.play_panel,weight=3)
        self.workspace_split.add(self.live_panel,weight=2)
        self.root.after(450,self._restore_workspace_sash)
        # Playback layout is independent from live layout.
        row=ttk.Frame(self.play_panel,style='Panel.TFrame');row.pack(fill='x',pady=(0,6))
        ttk.Label(row,text='Playback grid').pack(side='left')
        play_grid=self.workspace_state.get('playback_grid',{'columns':3,'rows':1})
        self.play_cols_var=tk.StringVar(value=str(play_grid.get('columns',3)))
        self.play_rows_var=tk.StringVar(value=str(play_grid.get('rows',1)))
        ttk.Label(row,text='Columns').pack(side='left',padx=(8,2));ttk.Entry(row,textvariable=self.play_cols_var,width=4).pack(side='left')
        ttk.Label(row,text='Rows').pack(side='left',padx=(6,2));ttk.Entry(row,textvariable=self.play_rows_var,width=4).pack(side='left')
        ttk.Button(row,text='Apply grid',command=lambda:self._apply_grid_layout('playback')).pack(side='left',padx=5)
        ttk.Button(row,text='Refresh video library',command=self.load_recordings).pack(side='right')
        # Shared 24-hour wall-clock timeline. Every click seeks all enabled camera
        # playback tiles to the same source timestamp.
        timeline_controls = ttk.Frame(self.play_panel, style='Panel.TFrame')
        timeline_controls.pack(fill='x', pady=(0,3))
        ttk.Label(timeline_controls, text='24-hour timeline').pack(side='left')
        ttk.Button(timeline_controls, text='◀ Day', width=7,
                   command=lambda:self._timeline_shift_day(-1)).pack(side='left', padx=(7,2))
        self.timeline_day_var = tk.StringVar(value=self.timeline_day.isoformat())
        ttk.Entry(timeline_controls, textvariable=self.timeline_day_var, width=12).pack(side='left', padx=2)
        ttk.Button(timeline_controls, text='Load day', command=self._timeline_load_day).pack(side='left', padx=2)
        ttk.Button(timeline_controls, text='Day ▶', width=7,
                   command=lambda:self._timeline_shift_day(1)).pack(side='left', padx=2)
        ttk.Button(timeline_controls, text='PLAY ALL', style='Accent.TButton',
                   command=self._timeline_play_all).pack(side='left', padx=(10,2))
        ttk.Button(timeline_controls, text='PAUSE ALL', command=self._timeline_pause_all).pack(side='left', padx=2)
        ttk.Button(timeline_controls, text='STOP ALL', command=self._timeline_stop_all).pack(side='left', padx=2)
        self.timeline_range_label = ttk.Label(timeline_controls, text='Day start 05:00 · click timeline to synchronize',
                                              style='Muted.TLabel')
        self.timeline_range_label.pack(side='right', padx=6)
        self.timeline_canvas = tk.Canvas(self.play_panel, height=86, bg=self.palette['panel'],
            highlightthickness=1, highlightbackground=self.palette['raised'])
        self.timeline_canvas.pack(fill='x', pady=(0,6))
        self.timeline_canvas.bind('<Button-1>', self._timeline_click)
        self.timeline_canvas.bind('<Configure>', lambda _e:self.root.after_idle(self._timeline_redraw))
        legend = ttk.Label(self.play_panel, text='RAW original  ·  MER compressed archive  ·  TLR regular time-lapse  ·  TLI intelligent time-lapse  ·  ! clock offset warning',
                           style='Muted.TLabel')
        legend.pack(anchor='w', pady=(0,4))
        self.play_wall=tk.Frame(self.play_panel,bg=self.palette['bg']);self.play_wall.pack(fill='both',expand=True)
        # Live dock layout is fully independent.
        row2=ttk.Frame(self.live_panel,style='Panel.TFrame');row2.pack(fill='x',pady=(0,6))
        ttk.Label(row2,text='Live grid').pack(side='left')
        live_grid=self.workspace_state.get('live_grid',{'columns':3,'rows':1})
        self.live_cols_var=tk.StringVar(value=str(live_grid.get('columns',3)))
        self.live_rows_var=tk.StringVar(value=str(live_grid.get('rows',1)))
        ttk.Label(row2,text='Columns').pack(side='left',padx=(8,2));ttk.Entry(row2,textvariable=self.live_cols_var,width=4).pack(side='left')
        ttk.Label(row2,text='Rows').pack(side='left',padx=(6,2));ttk.Entry(row2,textvariable=self.live_rows_var,width=4).pack(side='left')
        ttk.Button(row2,text='Apply grid',command=lambda:self._apply_grid_layout('live')).pack(side='left',padx=5)
        self.live_wall=tk.Frame(self.live_panel,bg=self.palette['bg']);self.live_wall.pack(fill='both',expand=True)
        self.play_tiles={};self.live_tiles={};self.video_tiles=[]
        self._rebuild_tile_group('playback')
        self._rebuild_tile_group('live')
        self.load_recordings()
        self._set_panels()
        self.root.after(200, self._timeline_redraw)

    def live_camera_is_enabled(self, camera_id):
        return bool(self.live_camera_enabled.get(str(camera_id), True))

    def set_live_camera_enabled(self, camera_id, enabled):
        cid = str(camera_id)
        self.live_camera_enabled[cid] = bool(enabled)
        tile = next((item for item in self.live_tiles.values() if item.camera_id == cid), None)
        if tile is not None:
            if enabled:
                if self.show_live_var.get():
                    if tile.player is None or tile.player.process is None or tile.player.process.poll() is not None:
                        if tile._start_after is None:
                            tile._start_after = self.root.after(120, tile.start_player)
                    tile.placeholder.configure(text='WAITING FOR LIVE VIDEO')
                    tile.placeholder.place(relx=.5, rely=.5, anchor='center')
                else:
                    tile.stop_player()
                    tile.status.configure(text='LIVE DOCK HIDDEN', fg=self.palette['muted'])
            else:
                tile.stop_player()
                tile.status.configure(text='LIVE OFF', fg=self.palette['muted'])
                tile.placeholder.configure(text='LIVE STREAM DISABLED')
                tile.placeholder.place(relx=.5, rely=.5, anchor='center')
        self._schedule_workspace_save()

    def _refresh_live_camera_switches(self):
        row = getattr(self, 'live_switch_row', None)
        if row is None or not row.winfo_exists():
            return
        for child in row.winfo_children(): child.destroy()
        ttk.Label(row, text='INDIVIDUAL LIVE STREAMS:').pack(side='left', padx=(2,8))
        self.live_camera_vars = {}
        for cam in self.cameras:
            cid = str(cam.get('id', ''))
            variable = tk.BooleanVar(value=self.live_camera_is_enabled(cid))
            self.live_camera_vars[cid] = variable
            label = f"{cam.get('name', cid)} ({cid})"
            ttk.Checkbutton(row, text=label, variable=variable,
                command=lambda camera_id=cid, var=variable: self.set_live_camera_enabled(camera_id, var.get())).pack(side='left', padx=5)

    def _layout_capacity(self, kind):
        try:
            col_var, row_var = self._grid_variables(kind)
            saved = self.workspace_state.get(f'{kind}_grid', {'columns': 3, 'rows': 1})
            col_text, row_text = col_var.get().strip(), row_var.get().strip()
            cols=max(1,min(32,int(col_text, 10) if col_text else int(saved.get('columns',3))))
            rows=max(1,min(32,int(row_text, 10) if row_text else int(saved.get('rows',1))))
            if cols*rows>64: raise ValueError('Grid capacity exceeds 64 tiles.')
            return cols,rows,cols*rows
        except Exception:
            grid=self.workspace_state.get(f'{kind}_grid',{'columns':3,'rows':1})
            cols=max(1,min(32,int(grid.get('columns',3))));rows=max(1,min(32,int(grid.get('rows',1))))
            return cols,rows,min(64,cols*rows)

    def _destroy_group(self, kind):
        group = self.play_tiles if kind == 'playback' else self.live_tiles
        for tile in list(group.values()):
            tile.destroy()
            if tile in self.video_tiles: self.video_tiles.remove(tile)
        group.clear()

    def _rebuild_tile_group(self, kind):
        if not hasattr(self, 'play_wall'): return
        cams = [c for c in self.cameras if c.get('enabled')]
        cols, rows, capacity = self._layout_capacity(kind)
        target = cams[:min(capacity, len(cams))]
        parent = self.play_wall if kind == 'playback' else self.live_wall
        group = self.play_tiles if kind == 'playback' else self.live_tiles
        existing = {tile.camera_id: tile for tile in group.values()}
        wanted_ids = {str(cam.get('id')) for cam in target}
        for cid, tile in list(existing.items()):
            if cid not in wanted_ids:
                tile.destroy()
                if tile in self.video_tiles: self.video_tiles.remove(tile)
                existing.pop(cid, None)
        group.clear()
        # Clear stale weighted rows/columns from a previous larger grid. Empty
        # configured cells must not reserve half the wall after a resize.
        for row in range(32): parent.grid_rowconfigure(row, weight=0, uniform='')
        for col in range(32): parent.grid_columnconfigure(col, weight=0, uniform='')
        used_rows = (len(target) + cols - 1) // cols if target else 0
        used_cols = min(cols, len(target))
        for row in range(used_rows): parent.grid_rowconfigure(row, weight=1, uniform=f'{kind}-row')
        for col in range(used_cols): parent.grid_columnconfigure(col, weight=1, uniform=f'{kind}-col')
        for index, cam in enumerate(target):
            cid = str(cam.get('id'))
            tile = existing.get(cid)
            if tile is None:
                tile = VideoTile(self, parent, cam, kind, index, self.recordings if kind == 'playback' else [])
                self.video_tiles.append(tile)
            else:
                tile.camera = cam; tile.index = index
                tile.title.configure(text=f"{cam.get('name', cid)} · {cid}")
            tile.frame.grid(row=index // cols, column=index % cols, sticky='nsew', padx=4, pady=4)
            group[index] = tile
        self._schedule_workspace_save()

    def _sync_tile_groups_if_needed(self):
        if not self._media_page_built or not hasattr(self, 'play_wall'): return
        for kind in ('playback', 'live'):
            group = self.play_tiles if kind == 'playback' else self.live_tiles
            _cols, _rows, capacity = self._layout_capacity(kind)
            expected = [str(c.get('id')) for c in self.cameras if c.get('enabled')][:capacity]
            actual = [t.camera_id for _, t in sorted(group.items())]
            if expected != actual: self._rebuild_tile_group(kind)

    def _set_panels(self):
        if not hasattr(self,'workspace_split'):return
        panes=set(self.workspace_split.panes())
        for panel,var,weight in ((self.play_panel,self.show_playback_var,3),(self.live_panel,self.show_live_var,2)):
            exists=str(panel) in panes
            if var.get() and not exists:self.workspace_split.add(panel,weight=weight)
            elif not var.get() and exists:self.workspace_split.forget(panel)
        for tile in self.live_tiles.values():
            if self.show_live_var.get():
                if self.live_camera_is_enabled(tile.camera_id):
                    if (tile.player is None or tile.player.process is None or tile.player.process.poll() is not None) and tile._start_after is None:
                        tile._start_after = self.root.after(180, tile.start_player)
                else:
                    tile.stop_player()
                    tile.status.configure(text='LIVE OFF', fg=self.palette['muted'])
                    tile.placeholder.configure(text='LIVE STREAM DISABLED')
                    tile.placeholder.place(relx=.5, rely=.5, anchor='center')
            else:
                tile.stop_player()
        for tile in self.play_tiles.values():
            if self.show_playback_var.get():
                if tile.player and tile.player.connected():
                    tile.player.set('pause', False if tile.play_state == 'PLAYING' else True)
                elif (tile.player is None or tile.player.process is None or tile.player.process.poll() is not None) and tile._start_after is None:
                    tile._start_after = self.root.after(180, tile.start_player)
            else:
                if tile.player: tile.player.set('pause', True)
        self.workspace_state['show_live']=bool(self.show_live_var.get())
        self.workspace_state['show_playback']=bool(self.show_playback_var.get())
        self._schedule_workspace_save()

    def _refresh_camera_tile_status(self):
        cams={c.get('id'):c for c in self.cameras}
        for tile in self.live_tiles.values():
            cam = cams.get(tile.camera_id, {})
            source_state = cam.get('status', 'WAITING')
            detail = cam.get('source_details', '')
            if not (tile.player and tile.player.connected()):
                color = self.palette['bad'] if source_state in ('ENGINE OFFLINE','OFFLINE','NO VIDEO') else self.palette['warn']
                if self.show_live_var.get(): tile.status.configure(text=source_state, fg=color)
                else: tile.status.configure(text='LIVE DOCK HIDDEN', fg=self.palette['muted'])
            if detail and not (tile.player and tile.player.connected() and tile._last_video_at):
                tile.footer.configure(text=detail)

    def tile_volume_changed(self, source_tile, value):
        if getattr(self,'link_controls_var',None) and self.link_controls_var.get():
            for tile in self.video_tiles:
                if tile is not source_tile and tile.link_var.get():
                    tile.volume_var.set(value);tile.volume=float(value)
                    if tile.player:tile.player.set('volume',float(value))
                    tile.volume_label.configure(text=f'{int(value)}%')

    def tile_speed_changed(self, source_tile, value):
        if getattr(self,'link_controls_var',None) and self.link_controls_var.get():
            for tile in self.play_tiles.values():
                if tile is not source_tile and tile.link_var.get():
                    tile.speed=float(value)
                    if hasattr(tile,'speed_var'):tile.speed_var.set(f'{float(value):g}x')
                    if tile.player:tile.player.set('speed',float(value))

    def apply_master_volume(self,value):
        try:value=max(0,min(100,float(value)))
        except Exception:return
        for tile in self.video_tiles:
            if not tile.link_var.get():continue
            tile.volume=value;tile.volume_var.set(value);tile.volume_label.configure(text=f'{int(value)}%')
            if tile.player:tile.player.set('volume',value)

    def apply_master_speed(self):
        try:value=float(self.master_speed_var.get().rstrip('x'))
        except Exception:return
        for tile in self.play_tiles.values():
            if not tile.link_var.get():continue
            tile.speed=value;tile.speed_var.set(f'{value:g}x')
            if tile.player:tile.player.set('speed',value)

    def apply_master_fps(self):
        value=str(self.master_fps_var.get())
        for tile in self.video_tiles:
            if not tile.link_var.get():continue
            tile.fps=value
            if hasattr(tile,'fps_var'):tile.fps_var.set(value)
            if tile.player:tile.player.set_fps_cap(value)

    def load_recordings(self):
        def worker():
            try:r=rpc('recordings.list',{})
            except Exception as exc:r={'error':str(exc),'files':[]}
            def apply():
                self.recordings=r.get('files',[])
                for tile in self.play_tiles.values():tile.set_recordings(self.recordings)
                self._timeline_redraw()
                if r.get('error'):self.play_time_text='Recording index unavailable: '+r['error']
            self.root.after(0,apply)
        threading.Thread(target=worker,daemon=True).start()

    def set_focused_tile(self, tile):
        self.focused_tile = tile
        try: tile.video.focus_set()
        except tk.TclError: pass

    def toggle_tile_fullscreen(self, tile):
        if self._fullscreen_session:
            self._exit_tile_fullscreen(); return
        if not tile.player or not tile.player.connected():
            messagebox.showinfo('Fullscreen', 'This tile has no active player yet.', parent=self.root); return
        pane_list = list(self.workspace_split.panes())
        target_panel = self.play_panel if tile.kind == 'playback' else self.live_panel
        hidden_tiles = []
        for group in (self.play_tiles, self.live_tiles):
            for other in group.values():
                if other is not tile and other.frame.winfo_ismapped():
                    info = dict(other.frame.grid_info()); other.frame.grid_remove(); hidden_tiles.append((other, info))
        for panel in pane_list:
            if panel is not target_panel: self.workspace_split.forget(panel)
        tile.frame.grid_configure(row=0, column=0, sticky='nsew', padx=0, pady=0)
        wall = self.play_wall if tile.kind == 'playback' else self.live_wall
        for row in range(32): wall.grid_rowconfigure(row, weight=0)
        for col in range(32): wall.grid_columnconfigure(col, weight=0)
        wall.grid_rowconfigure(0, weight=1); wall.grid_columnconfigure(0, weight=1)
        self.sidebar.pack_forget(); self.header.pack_forget()
        self.media_top.pack_forget(); self.media_master.pack_forget()
        geometry = self.root.winfo_geometry()
        self.root.attributes('-fullscreen', True)
        self._fullscreen_session = {'tile':tile, 'panes':pane_list, 'hidden_tiles':hidden_tiles,
            'geometry':geometry, 'target_panel':target_panel}
        self.set_focused_tile(tile)

    def _exit_tile_fullscreen(self):
        session = self._fullscreen_session
        if not session: return
        self._fullscreen_session = None
        try:
            self.root.attributes('-fullscreen', False)
            if session.get('geometry'): self.root.geometry(session['geometry'])
            self.sidebar.pack(side='left', fill='y')
            self.header.pack(fill='x', padx=20, pady=(16,8))
            self.media_top.pack(fill='x', pady=(0,8), before=self.workspace_split)
            self.media_master.pack(fill='x', pady=(0,8), before=self.workspace_split)
            for panel in session['panes']:
                self.workspace_split.add(panel, weight=3 if panel is self.play_panel else 2)
            for tile, info in session['hidden_tiles']:
                try: tile.frame.grid(**{k:v for k,v in info.items() if k != 'in'})
                except tk.TclError: pass
            self._rebuild_tile_group('playback'); self._rebuild_tile_group('live')
            self.root.after(150, self._restore_workspace_sash)
        except tk.TclError as exc: log_error(exc)

    def _on_video_key(self, event):
        if self.page != 'Live & Playback' and not self._fullscreen_session: return None
        tile = self.focused_tile
        if tile is None: return None
        widget = getattr(event, 'widget', None)
        if isinstance(widget, (tk.Entry, tk.Text, ttk.Entry, ttk.Combobox, ttk.Treeview, tk.Spinbox)):
            return None
        key = str(getattr(event, 'keysym', '')).lower()
        char = str(getattr(event, 'char', '')).lower()
        if key == 'escape' and self._fullscreen_session:
            self._exit_tile_fullscreen(); return 'break'
        if key == 'space': tile.toggle_play_pause(); return 'break'
        if key == 'left':
            if tile.kind == 'playback' and tile.player: tile.player.command(['seek', -5, 'relative', 'exact'])
            return 'break'
        if key == 'right':
            if tile.kind == 'playback' and tile.player: tile.player.command(['seek', 5, 'relative', 'exact'])
            return 'break'
        if key == 'up':
            if tile.kind == 'playback' and tile.player: tile.player.command(['seek', 60, 'relative', 'exact'])
            return 'break'
        if key == 'down':
            if tile.kind == 'playback' and tile.player: tile.player.command(['seek', -60, 'relative', 'exact'])
            return 'break'
        if char == 'm': tile.toggle_mute(); return 'break'
        if char == 'f': self.toggle_tile_fullscreen(tile); return 'break'
        if char == 'i': tile.toggle_stats(); return 'break'
        if char == '[' and tile.kind == 'playback':
            tile.speed = max(0.1, tile.speed / 1.1); tile.speed_var.set(f'{tile.speed:.2g}x')
            if tile.player: tile.player.set('speed', tile.speed)
            return 'break'
        if char == ']' and tile.kind == 'playback':
            tile.speed = min(64.0, tile.speed * 1.1); tile.speed_var.set(f'{tile.speed:.2g}x')
            if tile.player: tile.player.set('speed', tile.speed)
            return 'break'
        if char == '9': tile._volume_step(-1); return 'break'
        if char == '0': tile._volume_step(1); return 'break'
        if key == 'period' and tile.player and tile.kind == 'playback': tile.player.command(['frame-step']); return 'break'
        if key == 'comma' and tile.player and tile.kind == 'playback': tile.player.command(['frame-back-step']); return 'break'
        # 'q' intentionally does not quit the application; use the master terminal's quit command.
        return None

    def stop_media_tiles(self):
        if self._fullscreen_session: self._exit_tile_fullscreen()
        for kind in ('live','playback'): self._destroy_group(kind)

    def page_cameras(self):
        top=ttk.Frame(self.body);top.pack(fill='x',pady=(0,8))
        ttk.Label(top,text='Camera credentials and optional external audio-source credentials are stored in config/cameras.json, including CAM3 UDX settings.',style='Muted.TLabel').pack(side='left')
        ttk.Button(top,text='Add camera',style='Accent.TButton',command=lambda:self.edit_camera()).pack(side='right')
        self.cam_tree=ttk.Treeview(self.body,columns=('id','type','enabled','address','state'),show='headings',height=8)
        for c,t,w in [('id','ID',100),('type','TYPE',100),('enabled','ENABLED',90),('address','SOURCE',500),('state','STATE',130)]:self.cam_tree.heading(c,text=t);self.cam_tree.column(c,width=w,anchor='w')
        self.cam_tree.pack(fill='x',pady=5)
        self.cam_tree.bind('<<TreeviewSelect>>',self._camera_selected)
        buttons=ttk.Frame(self.body);buttons.pack(fill='x',pady=6)
        ttk.Button(buttons,text='Edit selected',command=self._edit_selected_camera).pack(side='left')
        ttk.Button(buttons,text='Delete selected',style='Danger.TButton',command=self._delete_selected_camera).pack(side='left',padx=6)
        ttk.Button(buttons,text='Test selected',command=self._test_selected_camera).pack(side='left')
        ttk.Button(buttons,text='Open camera administration page',command=self._open_selected_admin).pack(side='left',padx=6)
        self.cam_info=ttk.Label(self.body,text='Select a camera to edit, test or open its administration page.',style='Muted.TLabel');self.cam_info.pack(anchor='w',pady=10)
        self._refresh_camera_table()

    def _refresh_camera_table(self):
        if not hasattr(self,'cam_tree') or not self.cam_tree.winfo_exists():return
        for x in self.cam_tree.get_children():self.cam_tree.delete(x)
        for c in self.cameras:
            addr=c.get('rtsp_url') or ('CAM3 proprietary RTMP client' if c.get('type')=='cam3' else '')
            state=str(c.get('status','UNKNOWN'))
            if c.get('clock_warning'):
                state += f" · CLOCK {int(c.get('clock_offset_seconds',0) or 0):+d}s"
            self.cam_tree.insert('', 'end', iid=c['id'], values=(c['id'],c['type'],'YES' if c.get('enabled') else 'NO',addr,state))

    def _selected_camera(self):
        sel=self.cam_tree.selection() if hasattr(self,'cam_tree') else ()
        return next((c for c in self.cameras if sel and c['id']==sel[0]),None)

    def _camera_selected(self,_event=None):
        c=self._selected_camera()
        if c and hasattr(self,'cam_info'):self.cam_info.configure(text=f"{c['name']} | {c['type']} | {'enabled' if c.get('enabled') else 'disabled'} | {c.get('live_url','')}")

    def _edit_selected_camera(self):
        c=self._selected_camera()
        if not c:messagebox.showinfo('Camera','Select a camera first.');return
        self.edit_camera(c)

    def edit_camera(self, camera=None):
        c = dict(camera or {
            'id': 'cam4', 'name': 'Camera 4', 'type': 'rtsp', 'rtsp_url': 'rtsp://',
            'username': '', 'password': '', 'enabled': False,
            'web_admin_url': '', 'web_admin_username': '', 'web_admin_password': '',
            'audio_source_url': '', 'audio_source_username': '', 'audio_source_password': '',
            'clock_offset_seconds': 0, 'rtmp_host': '192.168.10.104',
            'rtmp_port': 80, 'rtmp_username': '', 'rtmp_password': '',
            'rtmp_stream_name': '720p.264'
        })
        win = tk.Toplevel(self.root)
        win.title('Camera settings')
        win.configure(bg=self.palette['bg'])
        win.transient(self.root)
        win.grab_set()
        win.geometry('760x760')
        body = ttk.Frame(win, padding=18)
        body.pack(fill='both', expand=True)
        ttk.Label(body, text='CAMERA CONNECTION', style='Title.TLabel').pack(anchor='w', pady=(0, 10))
        type_row = ttk.Frame(body)
        type_row.pack(fill='x', pady=(0, 8))
        ttk.Label(type_row, text='Source type').pack(side='left', padx=(0, 12))
        type_var = tk.StringVar(value=str(c.get('type', 'rtsp')).lower())
        type_box = ttk.Combobox(type_row, textvariable=type_var, values=('rtsp', 'cam3'), state='readonly', width=18)
        type_box.pack(side='left')
        hint = ttk.Label(body, text='', style='Muted.TLabel', wraplength=650)
        hint.pack(fill='x', pady=(0, 8))
        form = ttk.Frame(body)
        form.pack(fill='both', expand=True)
        footer = ttk.Frame(body)
        footer.pack(fill='x', pady=(8, 0))
        enabled = tk.BooleanVar(value=bool(c.get('enabled', False)))

        defaults = {
            'id': c.get('id', ''), 'name': c.get('name', ''),
            'rtsp_url': c.get('rtsp_url', 'rtsp://'), 'username': c.get('username', ''),
            'password': c.get('password', ''), 'rtmp_host': c.get('rtmp_host', '192.168.10.104'),
            'rtmp_port': c.get('rtmp_port', 80), 'rtmp_username': c.get('rtmp_username', ''),
            'rtmp_password': c.get('rtmp_password', c.get('password', '')),
            'rtmp_stream_name': c.get('rtmp_stream_name', '720p.264'),
            'web_admin_url': c.get('web_admin_url', ''),
            'web_admin_username': c.get('web_admin_username', ''),
            'web_admin_password': c.get('web_admin_password', ''),
            'audio_source_url': c.get('audio_source_url', ''),
            'audio_source_username': c.get('audio_source_username', ''),
            'audio_source_password': c.get('audio_source_password', ''),
            'clock_offset_seconds': c.get('clock_offset_seconds', 0),
        }
        variables = {key: tk.StringVar(value=str(value if value is not None else ''))
                     for key, value in defaults.items()}
        current_keys = []

        def rebuild_form(_event=None):
            for widget in form.winfo_children():
                widget.destroy()
            current_keys.clear()
            typ = type_var.get().lower()
            rows = [('id', 'Camera ID'), ('name', 'Display name')]
            if typ == 'cam3':
                rows += [
                    ('rtmp_host', 'CAM3 UDX host'), ('rtmp_port', 'CAM3 UDX TCP port'),
                    ('rtmp_username', 'CAM3 UDX username'), ('rtmp_password', 'CAM3 UDX password'),
                    ('rtmp_stream_name', 'CAM3 stream name'),
                ]
                hint.configure(text='CAM3 UDX settings apply only to this CAM3 source. They are not used by RTSP cameras.')
            else:
                rows += [
                    ('rtsp_url', 'RTSP URL (credentials may be included)'),
                    ('username', 'RTSP username'), ('password', 'RTSP password'),
                ]
                hint.configure(text='RTSP connection settings apply only to this RTSP camera. No CAM3 UDX fields are stored for RTSP sources.')
            rows += [
                ('web_admin_url', 'Camera administration URL (optional)'),
                ('web_admin_username', 'Web administration username'),
                ('web_admin_password', 'Web administration password'),
                ('audio_source_url', 'Optional separate RTSP audio-only source URL'),
                ('audio_source_username', 'Audio source username'),
                ('audio_source_password', 'Audio source password'),
                ('clock_offset_seconds', 'Manual camera clock offset (seconds)'),
            ]
            for row, (key, label) in enumerate(rows):
                ttk.Label(form, text=label).grid(row=row, column=0, sticky='w', padx=(0, 12), pady=5)
                entry = ttk.Entry(form, textvariable=variables[key], width=58)
                entry.grid(row=row, column=1, sticky='ew', pady=5)
                current_keys.append(key)
            form.columnconfigure(1, weight=1)

        type_box.bind('<<ComboboxSelected>>', rebuild_form)
        rebuild_form()
        ttk.Checkbutton(body, text='Enable this camera source', variable=enabled).pack(anchor='e', pady=(8, 0))

        def save():
            data = {key: variables[key].get() for key in current_keys}
            data['type'] = type_var.get().lower()
            data['enabled'] = enabled.get()
            try:
                rpc('cameras.save', data)
                win.destroy()
                self.refresh_all()
            except Exception as exc:
                messagebox.showerror('Camera settings', str(exc), parent=win)

        ttk.Button(footer, text='Cancel', command=win.destroy).pack(side='right', padx=4)
        ttk.Button(footer, text='Save camera', style='Accent.TButton', command=save).pack(side='right', padx=4)

    def _delete_selected_camera(self):
        c=self._selected_camera()
        if not c:return
        if not messagebox.askyesno('Delete camera',f"Remove {c['name']} from the camera configuration? Recording files are not deleted."):return
        try:rpc('cameras.delete',{'id':c['id']});self.refresh_all()
        except Exception as exc:messagebox.showerror('Delete camera',str(exc))

    def _test_selected_camera(self):
        c=self._selected_camera()
        if not c:return
        try:
            r=rpc('cameras.test',{'camera_id':c['id']},timeout=25)
            messagebox.showinfo('Camera test',('TEST PASSED\n' if r.get('ok') else 'TEST FAILED\n')+r.get('details',''))
        except Exception as exc:messagebox.showerror('Camera test',str(exc))

    def _open_selected_admin(self):
        c=self._selected_camera()
        if not c:return
        url=c.get('web_admin_url','')
        if not url:messagebox.showinfo('Camera administration','No administration URL is configured.');return
        webbrowser.open(url)

    def _refresh_bookmarks_cache(self):
        data=self._safe_rpc('bookmarks.list',{},fallback={'bookmarks':[]})
        self.bookmarks=data.get('bookmarks',[])
        return list(self.bookmarks)

    def _open_cue_on_tile(self,tile,cue):
        if tile.kind != 'playback':
            self.show_playback_var.set(True)
            self._set_panels()
            tile = next(iter(self.play_tiles.values()), None)
            if tile is None:
                messagebox.showwarning('Cue', 'Enable at least one camera and allocate a playback tile first.')
                return
        try:
            start=datetime.fromisoformat(cue['start']).timestamp();end=datetime.fromisoformat(cue['end']).timestamp()
        except Exception:
            messagebox.showerror('Cue','This cue has invalid timestamps.');return
        cid=cue.get('camera_id')
        matches=[]
        for rec in self.recordings:
            if rec.get('camera_id')!=cid:continue
            rs=float(rec.get('start_ts',0));re=rs+float(rec.get('duration_seconds',0) or 0)
            if rs < end and re > start:
                matches.append(rec)
        priority={'RAW':0,'MER':1,'TLR':2,'TLI':3}
        matches.sort(key=lambda r:(priority.get(r.get('type_code'),9),abs(float(r.get('start_ts',0))-start)))
        if not matches:
            messagebox.showwarning('Cue footage unavailable',f"No remaining recording or archive was indexed for {cid} during this cue. The cue and its protection record remain saved.")
            return
        rec=matches[0]
        code=rec.get('type_code','')
        if code in ('TLR','TLI'):
            if not messagebox.askyesno('Time-lapse source',f'Only a time-lapse source is available for this cue ({code}). Playback will be accelerated and will not reproduce normal real-time motion. Open it?'):
                return
        tile.load_recording(rec)
        # For original/merged media, file position equals source wall-clock offset.
        # For time-lapse, use the stored original-time mapping where available.
        offset=max(0.0,start-float(rec.get('start_ts',0)))
        time_map=rec.get('time_map') or []
        if time_map:
            mapped=None
            for item in time_map:
                try:
                    a=datetime.fromisoformat(item['source_start']).timestamp()
                    b=datetime.fromisoformat(item['source_end']).timestamp()
                    if a<=start<=b:
                        mapped=float(item.get('output_start_seconds',0)) + (start-a)/max(0.001,float(item.get('speed',1)))
                        break
                except Exception:continue
            if mapped is not None:offset=mapped
        self.root.after(1200,lambda:tile.player.seek(offset) if tile.player else None)
        if float(rec.get('start_ts',0))>start or float(rec.get('start_ts',0))+float(rec.get('duration_seconds',0) or 0)<end:
            tile.status.configure(text='PARTIAL CUE',fg=self.palette['warn'])
            messagebox.showinfo('Partial cue source','The selected file covers only part of this cue. Other remaining files can be selected using the tile video picker.')

    def page_bookmarks(self):
        row=ttk.Frame(self.body);row.pack(fill='x',pady=(0,8))
        ttk.Label(row,text='CUE LIBRARY — every saved cue automatically protects its selected camera footage from purge.',style='Muted.TLabel').pack(side='left')
        ttk.Button(row,text='Create protected cue',style='Accent.TButton',command=self.add_bookmark).pack(side='right')
        self.book_tree=ttk.Treeview(self.body,columns=('camera','start','end','label','protected','comment'),show='headings')
        for key,title,width in [('camera','CAMERA',90),('start','START',170),('end','END',170),('label','CUE NAME',210),('protected','PROTECTION',110),('comment','VISIBLE COMMENT',460)]:self.book_tree.heading(key,text=title);self.book_tree.column(key,width=width,anchor='w')
        self.book_tree.pack(fill='both',expand=True)
        self.book_tree.bind('<Double-1>',lambda _e:self.edit_bookmark())
        buttons=ttk.Frame(self.body);buttons.pack(fill='x',pady=8)
        ttk.Button(buttons,text='Edit name / comment',command=self.edit_bookmark).pack(side='left')
        ttk.Button(buttons,text='Delete cue and release protection',style='Danger.TButton',command=self.delete_bookmark).pack(side='right')
        self._refresh_bookmarks()

    def _refresh_bookmarks(self):
        if not hasattr(self,'book_tree') or not self.book_tree.winfo_exists():return
        self._refresh_bookmarks_cache()
        for x in self.book_tree.get_children():self.book_tree.delete(x)
        for b in self.bookmarks:self.book_tree.insert('', 'end',iid=b['id'],values=(' + '.join(b.get('camera_ids') or [b.get('camera_id','')]),b.get('start',''),b.get('end',''),b.get('label',''), 'YES' if b.get('protected') else 'NO',b.get('notes','')))

    def add_bookmark(self):
        win=tk.Toplevel(self.root);win.title('Add bookmark');win.transient(self.root);win.grab_set();win.configure(bg=self.palette['bg']);win.geometry('650x450')
        cams=[c['id'] for c in self.cameras] or ['cam1'];cid=tk.StringVar(value=cams[0]);start=tk.StringVar(value=datetime.now().astimezone().replace(microsecond=0).isoformat());end=tk.StringVar(value=datetime.now().astimezone().replace(microsecond=0).isoformat());label=tk.StringVar(value='Protected event');notes=tk.StringVar()
        frm=ttk.Frame(win,padding=18);frm.pack(fill='both',expand=True)
        items=[('Camera',ttk.Combobox(frm,textvariable=cid,values=cams,state='readonly')),('Start (ISO local time)',ttk.Entry(frm,textvariable=start)),('End (ISO local time)',ttk.Entry(frm,textvariable=end)),('Label',ttk.Entry(frm,textvariable=label)),('Notes',ttk.Entry(frm,textvariable=notes))]
        for i,(text,w) in enumerate(items):ttk.Label(frm,text=text).grid(row=i,column=0,sticky='w',pady=7);w.grid(row=i,column=1,sticky='ew',pady=7)
        ttk.Label(frm,text='Protection is automatic: this cue protects the selected footage from purge.',style='Muted.TLabel').grid(row=5,column=1,sticky='w',pady=8);frm.columnconfigure(1,weight=1)
        def save():
            try:rpc('bookmarks.save',{'camera_id':cid.get(),'camera_ids':[cid.get()],'start':start.get(),'end':end.get(),'label':label.get(),'notes':notes.get(),'protected':True});win.destroy();self._refresh_bookmarks()
            except Exception as exc:messagebox.showerror('Bookmark',str(exc),parent=win)
        ttk.Button(frm,text='Save',style='Accent.TButton',command=save).grid(row=6,column=1,sticky='e',pady=12)

    def edit_bookmark(self):
        sel=self.book_tree.selection()
        if not sel:return
        item=next((x for x in self.bookmarks if x.get('id')==sel[0]),None)
        if not item:return
        win=tk.Toplevel(self.root);win.title('Edit cue');win.transient(self.root);win.grab_set();win.geometry('620x300')
        label=tk.StringVar(value=item.get('label','Cue'));notes=tk.StringVar(value=item.get('notes',''))
        frm=ttk.Frame(win,padding=18);frm.pack(fill='both',expand=True)
        ttk.Label(frm,text='Cue name').grid(row=0,column=0,sticky='w',pady=7);ttk.Entry(frm,textvariable=label,width=60).grid(row=0,column=1,sticky='ew')
        ttk.Label(frm,text='Visible comment').grid(row=1,column=0,sticky='w',pady=7);ttk.Entry(frm,textvariable=notes,width=60).grid(row=1,column=1,sticky='ew')
        ttk.Label(frm,text='Footage protection remains active.',style='Muted.TLabel').grid(row=2,column=1,sticky='w',pady=8);frm.columnconfigure(1,weight=1)
        def save():
            try:rpc('bookmarks.update',{'id':item['id'],'label':label.get(),'notes':notes.get()});win.destroy();self._refresh_bookmarks()
            except Exception as exc:messagebox.showerror('Edit cue',str(exc),parent=win)
        ttk.Button(frm,text='Save cue',style='Accent.TButton',command=save).grid(row=3,column=1,sticky='e',pady=12)

    def delete_bookmark(self):
        sel=self.book_tree.selection()
        if not sel:return
        if not messagebox.askyesno('Delete protected cue','This removes the cue and releases its automatic protection. Continue?'):return
        try:rpc('bookmarks.delete',{'id':sel[0]});self._refresh_bookmarks()
        except Exception as exc:messagebox.showerror('Bookmark',str(exc))

    def page_export_studio(self):
        self.export_queue=[]
        self.export_cues=[]
        intro=ttk.Frame(self.body);intro.pack(fill='x',pady=(0,8))
        ttk.Label(intro,text='Build one MP4 from ordered protected cues. Each section can use one, two or three cameras synchronized to the same source timestamps.',style='Muted.TLabel',wraplength=1050).pack(side='left',fill='x',expand=True)
        top=ttk.Panedwindow(self.body,orient='horizontal');top.pack(fill='both',expand=True)
        left=ttk.Frame(top);right=ttk.Frame(top)
        top.add(left,weight=3);top.add(right,weight=2)
        ttk.Label(left,text='SAVED CUES',style='PanelTitle.TLabel').pack(anchor='w',pady=(0,5))
        self.export_cue_tree=ttk.Treeview(left,columns=('camera','start','end','label','sources'),show='headings',selectmode='browse',height=9)
        for key,title,width in [('camera','CAMERA',65),('start','START',150),('end','END',150),('label','CUE',180),('sources','AVAILABLE SOURCE',150)]:
            self.export_cue_tree.heading(key,text=title);self.export_cue_tree.column(key,width=width,anchor='w')
        self.export_cue_tree.pack(fill='both',expand=True)
        self.export_cue_tree.bind('<<TreeviewSelect>>',self._export_cue_selected)
        cue_buttons=ttk.Frame(left);cue_buttons.pack(fill='x',pady=6)
        ttk.Button(cue_buttons,text='Refresh cues / footage',command=self._refresh_export_cues).pack(side='left')
        ttk.Label(left,text='CAMERAS IN THIS SECTION — select 1, 2 or 3; the cue range supplies the synchronized wall-clock interval.',style='Muted.TLabel',wraplength=620).pack(anchor='w',pady=(8,3))
        self.export_camera_list=tk.Listbox(left,selectmode='multiple',height=4,exportselection=False,
            bg=self.palette['panel'],fg=self.palette['text'],selectbackground=self.palette['button'],relief='flat',highlightthickness=1,highlightbackground=self.palette['raised'])
        self.export_camera_list.pack(fill='x')
        ttk.Button(left,text='Add selected cue as export section',style='Accent.TButton',command=self._export_add_section).pack(anchor='e',pady=7)

        ttk.Label(right,text='EXPORT ORDER',style='PanelTitle.TLabel').pack(anchor='w',pady=(0,5))
        self.export_queue_tree=ttk.Treeview(right,columns=('order','section'),show='headings',selectmode='browse',height=9)
        self.export_queue_tree.heading('order',text='#');self.export_queue_tree.column('order',width=38,anchor='center')
        self.export_queue_tree.heading('section',text='SECTION / CAMERAS');self.export_queue_tree.column('section',width=360,anchor='w')
        self.export_queue_tree.pack(fill='both',expand=True)
        queue_buttons=ttk.Frame(right);queue_buttons.pack(fill='x',pady=6)
        ttk.Button(queue_buttons,text='↑ Move up',command=lambda:self._export_move(-1)).pack(side='left')
        ttk.Button(queue_buttons,text='↓ Move down',command=lambda:self._export_move(1)).pack(side='left',padx=4)
        ttk.Button(queue_buttons,text='Remove',command=self._export_remove_section).pack(side='right')

        opts=self.panel(self.body,'OUTPUT SETTINGS')
        opts.pack(fill='x',pady=(8,6))
        row=ttk.Frame(opts,style='Panel.TFrame');row.pack(fill='x')
        self.export_resolution=tk.StringVar(value=str(self.control.get('EXPORT_DEFAULT_RESOLUTION','1920x1080')))
        self.export_fps=tk.StringVar(value=str(self.control.get('EXPORT_DEFAULT_FPS',25)))
        self.export_crf=tk.IntVar(value=int(self.control.get('EXPORT_DEFAULT_CRF',20)))
        self.export_preset=tk.StringVar(value=str(self.control.get('EXPORT_DEFAULT_PRESET','medium')))
        self.export_audio=tk.StringVar(value=str(self.control.get('EXPORT_DEFAULT_AUDIO_MODE','mix')))
        ttk.Label(row,text='Resolution').pack(side='left')
        ttk.Combobox(row,textvariable=self.export_resolution,values=['1280x720','1920x1080','2560x1440'],width=12,state='readonly').pack(side='left',padx=4)
        ttk.Label(row,text='FPS').pack(side='left',padx=(10,3))
        ttk.Combobox(row,textvariable=self.export_fps,values=['25','30','50','60'],width=5,state='readonly').pack(side='left')
        ttk.Label(row,text='H.264 CRF').pack(side='left',padx=(10,3))
        tk.Spinbox(row,from_=16,to=30,textvariable=self.export_crf,width=4,bg=self.palette['panel'],fg=self.palette['text'],insertbackground=self.palette['accent']).pack(side='left')
        ttk.Label(row,text='Preset').pack(side='left',padx=(10,3))
        ttk.Combobox(row,textvariable=self.export_preset,values=['ultrafast','veryfast','faster','fast','medium','slow'],width=10,state='readonly').pack(side='left')
        row2=ttk.Frame(opts,style='Panel.TFrame');row2.pack(fill='x',pady=(7,0))
        ttk.Label(row2,text='Audio').pack(side='left')
        ttk.Combobox(row2,textvariable=self.export_audio,values=['mix','first','mute'],width=10,state='readonly').pack(side='left',padx=4)
        self.export_allow_tl=tk.BooleanVar(value=bool(self.control.get('EXPORT_ALLOW_TIMELAPSE_DEFAULT',False)))
        ttk.Checkbutton(row2,text='Allow time-lapse fallback (reduced temporal detail)',variable=self.export_allow_tl).pack(side='left',padx=12)
        ttk.Label(opts,text='Source priority: RAW original → MER archive → TLR/TLI only when the time-lapse fallback is explicitly enabled. Output is H.264/AAC, yuv420p, MP4 faststart.',style='Muted.TLabel',wraplength=1100).pack(anchor='w',pady=(7,0))
        bottom=ttk.Frame(self.body);bottom.pack(fill='x',pady=(4,0))
        self.export_status=tk.StringVar(value='Ready. Add one or more saved cues to the export order.')
        ttk.Label(bottom,textvariable=self.export_status,style='Muted.TLabel',wraplength=800).pack(side='left',fill='x',expand=True)
        ttk.Button(bottom,text='Open export folder',command=self._open_export_folder).pack(side='right',padx=4)
        ttk.Button(bottom,text='EXPORT MP4',style='Accent.TButton',command=self._export_start).pack(side='right',padx=4)
        self._refresh_export_cues()
        self._export_poll_status()

    def _refresh_export_cues(self):
        if not hasattr(self,'export_cue_tree') or not self.export_cue_tree.winfo_exists():return
        try:
            self.export_cues=rpc('bookmarks.list',{}).get('bookmarks',[])
            self.recordings=rpc('recordings.list',{}).get('files',[])
        except Exception as exc:
            self.export_status.set('Unable to read cue/recording index: '+str(exc)[:180]);return
        self.export_cameras_all=[c for c in self.cameras]
        self.export_camera_list.delete(0,'end')
        for cam in self.export_cameras_all:self.export_camera_list.insert('end',f"{cam.get('id')} — {cam.get('name',cam.get('id'))}")
        for item in self.export_cue_tree.get_children():self.export_cue_tree.delete(item)
        for cue in self.export_cues:
            try:a=datetime.fromisoformat(cue['start']).timestamp();b=datetime.fromisoformat(cue['end']).timestamp()
            except Exception:a=b=0
            formats=set()
            for rec in self.recordings:
                if rec.get('camera_id')!=cue.get('camera_id'):continue
                rs=float(rec.get('start_ts',0));re=rs+float(rec.get('duration_seconds',0) or 0)
                if rs < b and re > a:formats.add(str(rec.get('type_code','?')))
            availability='/'.join(x for x in ('RAW','MER','TLR','TLI') if x in formats) or 'Not indexed'
            self.export_cue_tree.insert('','end',iid=str(cue['id']),values=(cue.get('camera_id',''),cue.get('start',''),cue.get('end',''),cue.get('label',''),availability))
        if not self.export_cues:self.export_status.set('No saved cues. Create a protected cue from a playback tile or Cue Library first.')
        self._export_refresh_queue()

    def _export_cue_selected(self,_event=None):
        selected=self.export_cue_tree.selection()
        if not selected:return
        cue=next((x for x in self.export_cues if x.get('id')==selected[0]),None)
        if not cue:return
        for index in self.export_camera_list.curselection():self.export_camera_list.selection_clear(index)
        for index,cam in enumerate(self.export_cameras_all):
            if cam.get('id')==cue.get('camera_id'):
                self.export_camera_list.selection_set(index);break

    def _export_add_section(self):
        selected=self.export_cue_tree.selection()
        if not selected:
            messagebox.showinfo('Export Studio','Select a saved cue first.',parent=self.root);return
        cue=next((x for x in self.export_cues if x.get('id')==selected[0]),None)
        if not cue:return
        selected_cameras=[self.export_cameras_all[i].get('id') for i in self.export_camera_list.curselection()]
        selected_cameras=list(dict.fromkeys(x for x in selected_cameras if x))
        if not 1<=len(selected_cameras)<=3:
            messagebox.showwarning('Export Studio','Select exactly one, two or three cameras for this section.',parent=self.root);return
        self.export_queue.append({'cue_id':cue['id'],'camera_ids':selected_cameras,'label':cue.get('label','Cue')})
        self._export_refresh_queue()

    def _export_refresh_queue(self):
        if not hasattr(self,'export_queue_tree') or not self.export_queue_tree.winfo_exists():return
        for item in self.export_queue_tree.get_children():self.export_queue_tree.delete(item)
        for index,section in enumerate(self.export_queue,1):
            self.export_queue_tree.insert('','end',iid=str(index-1),values=(index,f"{section['label']}  ·  {' + '.join(section['camera_ids'])}"))

    def _export_remove_section(self):
        selected=self.export_queue_tree.selection()
        if not selected:return
        index=int(selected[0]);self.export_queue.pop(index);self._export_refresh_queue()

    def _export_move(self,direction):
        selected=self.export_queue_tree.selection()
        if not selected:return
        index=int(selected[0]);target=index+direction
        if target<0 or target>=len(self.export_queue):return
        self.export_queue[index],self.export_queue[target]=self.export_queue[target],self.export_queue[index]
        self._export_refresh_queue();self.export_queue_tree.selection_set(str(target))

    def _export_start(self):
        if not self.export_queue:
            messagebox.showinfo('Export Studio','Add at least one cue section.',parent=self.root);return
        try:
            settings={'resolution':self.export_resolution.get(),'fps':int(self.export_fps.get()),
                      'crf':int(self.export_crf.get()),'preset':self.export_preset.get(),
                      'audio_mode':self.export_audio.get(),'allow_timelapse':self.export_allow_tl.get()}
            result=rpc('exports.start',{'sections':[{'cue_id':x['cue_id'],'camera_ids':x['camera_ids']} for x in self.export_queue], 'settings':settings})
            self.export_status.set(f"Export {result.get('id')} started. Closing the GUI will not stop the export; the master terminal owns the engine.")
            self._export_poll_status()
        except Exception as exc:messagebox.showerror('Export Studio',str(exc),parent=self.root)

    def _export_poll_status(self):
        if self.page!='Export Studio' or not hasattr(self,'export_status') or not self.root.winfo_exists():return
        try:
            state=rpc('exports.status',{},timeout=3)
            st=state.get('state','IDLE');msg=state.get('message','')
            if st=='RUNNING':
                self.export_status.set(f"RUNNING {state.get('completed_sections',0)}/{state.get('total_sections',0)} — {msg}")
            elif st=='COMPLETE':
                self.export_status.set(f"COMPLETE — {state.get('output','')} · {float(state.get('duration_seconds',0)):.1f}s")
                if state.get('time_lapse_sources_used'):
                    self.export_status.set(self.export_status.get()+' · WARNING: time-lapse source used; temporal detail is reduced.')
            elif st=='FAILED':self.export_status.set('FAILED — '+msg[:500])
        except Exception as exc:self.export_status.set('Export status unavailable: '+str(exc)[:160])
        self.root.after(1500,self._export_poll_status)

    def _open_export_folder(self):
        folder=ROOT/'data'/'exports';folder.mkdir(parents=True,exist_ok=True)
        try:subprocess.Popen(['xdg-open',str(folder)],cwd=ROOT)
        except Exception as exc:messagebox.showerror('Export folder',str(exc),parent=self.root)

    def page_archives(self):
        box=self.panel(self.body,'DAILY ARCHIVE / TIME-LAPSE')
        box.pack(fill='x',pady=8)
        cams=[c['id'] for c in self.cameras] or ['cam1'];self.archive_cam=tk.StringVar(value=cams[0]);self.archive_day=tk.StringVar(value=datetime.now().date().isoformat());self.archive_mode=tk.StringVar(value='regular')
        row=ttk.Frame(box,style='Panel.TFrame');row.pack(fill='x')
        ttk.Label(row,text='Camera').pack(side='left');ttk.Combobox(row,textvariable=self.archive_cam,values=cams,state='readonly',width=12).pack(side='left',padx=5)
        ttk.Label(row,text='Archive day').pack(side='left',padx=(15,4));ttk.Entry(row,textvariable=self.archive_day,width=15).pack(side='left')
        ttk.Label(row,text='Time-lapse').pack(side='left',padx=(15,4));ttk.Combobox(row,textvariable=self.archive_mode,values=['regular','intelligent'],state='readonly',width=14).pack(side='left')
        actions=ttk.Frame(box,style='Panel.TFrame');actions.pack(fill='x',pady=12)
        ttk.Button(actions,text='Build daily archive',style='Accent.TButton',command=lambda:self._queue_archive('build','')).pack(side='left')
        ttk.Button(actions,text='Build time-lapse',command=lambda:self._queue_archive('timelapse',self.archive_mode.get())).pack(side='left',padx=6)
        ttk.Button(actions,text='Estimate / inspect',command=lambda:self._queue_archive('estimate','')).pack(side='left')
        self.archive_result=tk.Text(box,height=9,bg=self.palette['bg'],fg=self.palette['text'],insertbackground=self.palette['accent'],relief='flat',wrap='word');self.archive_result.pack(fill='x',pady=5)
        self.record_tree=ttk.Treeview(self.body,columns=('camera','group','start','duration','size','path','directory'),show='headings',height=12)
        for k,t,w in [('camera','CAMERA',80),('group','TYPE',85),('start','START',165),('duration','DURATION',85),('size','SIZE',90),('path','FILE',420),('directory','CONTAINING DIRECTORY',420)]:self.record_tree.heading(k,text=t);self.record_tree.column(k,width=w,anchor='w')
        self.record_tree.pack(fill='both',expand=True,pady=(10,4))
        self.record_tree.bind('<Double-1>', self._archive_directory_double_click)
        archive_open=ttk.Frame(self.body);archive_open.pack(fill='x',pady=(2,8))
        ttk.Button(archive_open,text='OPEN CONTAINING DIRECTORY',style='Accent.TButton',command=self._open_selected_archive_directory).pack(side='left')
        self.archive_directory_label=ttk.Label(archive_open,text='Select an archive to see its containing directory.',style='Muted.TLabel')
        self.archive_directory_label.pack(side='left',padx=12)
        self.record_tree.bind('<<TreeviewSelect>>', lambda _e: self._update_archive_directory_label(), add='+')
        self._refresh_recordings_table()

    def _queue_archive(self,task,mode):
        try:
            r=rpc('archive.run',{'task':task,'camera_id':self.archive_cam.get(),'day':self.archive_day.get(),'mode':mode})
            self.archive_result.insert('end',r.get('message','Job queued.')+'\n');self.archive_result.see('end')
        except Exception as exc:messagebox.showerror('Archive job',str(exc))

    def _refresh_recordings_table(self):
        if not hasattr(self,'record_tree') or not self.record_tree.winfo_exists():return
        data=self._safe_rpc('recordings.list',{},fallback={'files':[]});self.recordings=data.get('files',[])
        for x in self.record_tree.get_children():self.record_tree.delete(x)
        for i,item in enumerate(self.recordings):
            start=datetime.fromtimestamp(item['start_ts']).astimezone().strftime('%Y-%m-%d %H:%M:%S')
            dur=f"{item.get('duration_seconds',0):.0f}s";size=f"{item.get('size',0)/1048576:.1f} MB"
            directory = str((ROOT / item['path']).resolve().parent)
            self.record_tree.insert('', 'end',iid=str(i),values=(item['camera_id'],item['group'],start,dur,size,item['path'],directory))
        self._update_archive_directory_label()

    def _selected_archive_item(self):
        if not hasattr(self, 'record_tree') or not self.record_tree.winfo_exists(): return None
        selection = self.record_tree.selection()
        if not selection: return None
        try: return self.recordings[int(selection[0])]
        except (ValueError, IndexError, TypeError): return None

    def _update_archive_directory_label(self):
        label = getattr(self, 'archive_directory_label', None)
        if label is None or not label.winfo_exists(): return
        item = self._selected_archive_item()
        label.configure(text=str((ROOT / item['path']).resolve().parent) if item else 'Select an archive to see its containing directory.')

    def _open_selected_archive_directory(self):
        item = self._selected_archive_item()
        if not item:
            messagebox.showinfo('Archive directory', 'Select an archive first.', parent=self.root); return
        folder = (ROOT / item['path']).resolve().parent
        try:
            folder.relative_to(ROOT.resolve())
            if not folder.is_dir(): raise FileNotFoundError(folder)
            subprocess.Popen(['xdg-open', str(folder)], cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            messagebox.showerror('Archive directory', str(exc), parent=self.root)

    def _archive_directory_double_click(self, event):
        if self.record_tree.identify_column(event.x) == '#7':
            self._open_selected_archive_directory()
            return 'break'
        return None

    def page_zones(self):
        box=self.panel(self.body,'DETECTION EXCLUSION ZONES');box.pack(fill='x',pady=8)
        enabled=bool(self.control.get('ENABLE_DETECTION_EXCLUSION_ZONES',False))
        ttk.Label(box,text=f"Detection exclusion zones are {'enabled' if enabled else 'disabled'} in USER_CONTROL.py. They affect motion analysis only, never original recording.",style='Muted.TLabel').pack(anchor='w')
        cams=[c['id'] for c in self.cameras]
        self.zone_camera=tk.StringVar(value=cams[0] if cams else 'cam1')
        row=ttk.Frame(box,style='Panel.TFrame');row.pack(fill='x',pady=8)
        ttk.Label(row,text='Camera').pack(side='left')
        self.zone_camera_combo=ttk.Combobox(row,textvariable=self.zone_camera,values=cams,state='readonly',width=12)
        self.zone_camera_combo.pack(side='left',padx=6)
        self.zone_camera_combo.bind('<<ComboboxSelected>>',self._zone_camera_changed)
        ttk.Button(row,text='Load live snapshot',style='Accent.TButton',command=self._zone_load_snapshot).pack(side='left',padx=6)
        ttk.Button(row,text='Finish polygon',command=self._zone_finish_polygon).pack(side='left',padx=3)
        ttk.Button(row,text='Undo point',command=self._zone_undo_point).pack(side='left',padx=3)
        ttk.Button(row,text='Remove last polygon',style='Danger.TButton',command=self._zone_remove_polygon).pack(side='left',padx=3)
        self.zone_status=tk.StringVar(value='Load a camera snapshot, click to add polygon points, then finish and save.')
        ttk.Label(box,textvariable=self.zone_status,style='Muted.TLabel').pack(anchor='w',pady=(0,6))
        self.zone_canvas=tk.Canvas(self.body,width=640,height=360,bg='#03060c',highlightthickness=1,highlightbackground=self.palette['accent'])
        self.zone_canvas.pack(anchor='w',padx=10,pady=8)
        self.zone_canvas.bind('<Button-1>',self._zone_add_point)
        bottom=ttk.Frame(self.body);bottom.pack(fill='x',pady=6)
        ttk.Button(bottom,text='Save polygons for this camera',style='Accent.TButton',command=self._zone_save).pack(side='left')
        ttk.Label(bottom,text='Polygons are normalized to the preview image and stored in config/cameras.json.',style='Muted.TLabel').pack(side='left',padx=10)
        self.zone_photo=None;self.zone_polygons=[];self.zone_current=[];self.zone_width=640;self.zone_height=360
        self._zone_load_saved()

    def _zone_camera_changed(self,_event=None):
        self._zone_load_saved()

    def _zone_load_saved(self):
        cam=next((c for c in self.cameras if c.get('id')==self.zone_camera.get()),None)
        self.zone_polygons=[[[float(x),float(y)] for x,y in poly] for poly in (cam or {}).get('exclude_zones',[])]
        self.zone_current=[]
        self._zone_redraw()
        if hasattr(self,'zone_status'):self.zone_status.set(f"{len(self.zone_polygons)} saved polygon(s). Load a snapshot to draw or inspect the mask.")

    def _zone_load_snapshot(self):
        try:
            result=rpc('cameras.snapshot',{'camera_id':self.zone_camera.get()},timeout=20)
            self.zone_photo=tk.PhotoImage(data=result['png_base64'])
            self.zone_width=self.zone_photo.width();self.zone_height=self.zone_photo.height()
            self.zone_canvas.configure(width=self.zone_width,height=self.zone_height,scrollregion=(0,0,self.zone_width,self.zone_height))
            self._zone_redraw()
            if hasattr(self,'zone_status'):self.zone_status.set('Snapshot loaded. Click inside the image to add polygon points; finish each polygon before starting another.')
        except Exception as exc:messagebox.showerror('Detection zones','Unable to load camera snapshot: '+str(exc))

    def _zone_redraw(self):
        if not hasattr(self,'zone_canvas') or not self.zone_canvas.winfo_exists():return
        self.zone_canvas.delete('all')
        if self.zone_photo:self.zone_canvas.create_image(0,0,anchor='nw',image=self.zone_photo)
        for index,poly in enumerate(self.zone_polygons):
            coords=[]
            for x,y in poly:coords.extend((x*self.zone_width,y*self.zone_height))
            if len(coords)>=6:self.zone_canvas.create_polygon(*coords,outline=self.palette['accent2'],fill='',width=2)
            for i in range(0,len(coords),2):self.zone_canvas.create_oval(coords[i]-3,coords[i+1]-3,coords[i]+3,coords[i+1]+3,fill=self.palette['accent2'],outline='')
        if self.zone_current:
            coords=[]
            for x,y in self.zone_current:coords.extend((x*self.zone_width,y*self.zone_height))
            if len(coords)>=4:self.zone_canvas.create_line(*coords,fill=self.palette['accent'],width=2)
            for i in range(0,len(coords),2):self.zone_canvas.create_oval(coords[i]-3,coords[i+1]-3,coords[i]+3,coords[i+1]+3,fill=self.palette['accent'],outline='')

    def _zone_add_point(self,event):
        if not self.zone_photo:
            self.zone_status.set('Load a snapshot before drawing.')
            return
        x=max(0,min(self.zone_width-1,int(event.x)));y=max(0,min(self.zone_height-1,int(event.y)))
        self.zone_current.append([round(x/max(1,self.zone_width-1),6),round(y/max(1,self.zone_height-1),6)])
        self._zone_redraw()
        self.zone_status.set(f"Current polygon: {len(self.zone_current)} point(s). At least three are required.")

    def _zone_finish_polygon(self):
        if len(self.zone_current)<3:
            messagebox.showinfo('Detection zones','A polygon needs at least three points.')
            return
        self.zone_polygons.append(self.zone_current);self.zone_current=[];self._zone_redraw()
        self.zone_status.set(f"{len(self.zone_polygons)} polygon(s) ready to save.")

    def _zone_undo_point(self):
        if self.zone_current:self.zone_current.pop()
        self._zone_redraw()
        self.zone_status.set(f"Current polygon: {len(self.zone_current)} point(s).")

    def _zone_remove_polygon(self):
        if self.zone_polygons:self.zone_polygons.pop()
        self._zone_redraw()
        self.zone_status.set(f"{len(self.zone_polygons)} polygon(s) remain. Save to apply the change.")

    def _zone_save(self):
        if self.zone_current:
            messagebox.showinfo('Detection zones','Finish or undo the current polygon before saving.')
            return
        try:
            rpc('zones.save',{'camera_id':self.zone_camera.get(),'polygons':self.zone_polygons})
            for cam in self.cameras:
                if cam.get('id')==self.zone_camera.get():cam['exclude_zones']=self.zone_polygons
            self.zone_status.set(f"Saved {len(self.zone_polygons)} polygon(s) for {self.zone_camera.get()}.")
            messagebox.showinfo('Detection zones','Polygon mask saved. It affects intelligent time-lapse motion analysis only.')
        except Exception as exc:messagebox.showerror('Detection zones',str(exc))

    def page_appearance(self):
        box=self.panel(self.body,'DESKTOP APPEARANCE');box.pack(fill='x',pady=8)
        self.preset_var=tk.StringVar(value=self.appearance.get('preset','cyberpunk'))
        row=ttk.Frame(box,style='Panel.TFrame');row.pack(fill='x')
        ttk.Label(row,text='Theme preset').pack(side='left');ttk.Combobox(row,textvariable=self.preset_var,values=list(PALETTES),state='readonly',width=18).pack(side='left',padx=8)
        ttk.Button(row,text='Apply and save',style='Accent.TButton',command=self.save_appearance).pack(side='left')
        ttk.Label(box,text='Each panel, accent, button, border and effect is persisted in config/appearance.json. Edit individual color/effect tokens in that file; the preset control provides the four complete palettes.',style='Muted.TLabel',wraplength=1000).pack(anchor='w',pady=12)
        sample=tk.Frame(box,bg=self.palette['raised'],height=100);sample.pack(fill='x',pady=8);sample.pack_propagate(False)
        tk.Label(sample,text='CYBERPUNK NEON  •  NATIVE DESKTOP',bg=self.palette['raised'],fg=self.palette['accent'],font=('DejaVu Sans',15,'bold')).pack(anchor='w',padx=20,pady=15)
        tk.Button(sample,text='NEON ACTION',bg=self.palette['button'],fg=self.palette['accent'],activebackground=self.palette['accent2'],relief='flat',padx=14,pady=6).pack(anchor='w',padx=20)

    def save_appearance(self):
        preset=self.preset_var.get();current=self.appearance if isinstance(self.appearance,dict) else {'colors':{},'effects':{}}
        colors=dict(current.get('colors',{}));effects=dict(current.get('effects',{}))
        p=PALETTES[preset]
        aliases={'background':'bg','surface':'panel','surface_raised':'raised','text':'text','muted':'muted','accent':'accent','accent_alt':'accent2','success':'ok','warning':'warn','danger':'bad','button_primary_bg':'button'}
        for dst,src in aliases.items():colors[dst]=p[src]
        try:
            self.appearance=rpc('appearance.save',{'preset':preset,'colors':colors,'effects':effects}).get('appearance',{})
            self.palette=self._palette_for(self.appearance);self._style();self.show_page('Appearance');messagebox.showinfo('Appearance','Theme saved to config/appearance.json.')
        except Exception as exc:messagebox.showerror('Appearance',str(exc))

    def page_commands(self):
        box=self.panel(self.body,'FFMPEG / FFPROBE COMMAND CATALOG');box.pack(fill='both',expand=True,pady=8)
        ttk.Label(box,text='Every command is an argument array and is executed without a shell. The native UI edits the actual file used by the engine.',style='Muted.TLabel').pack(anchor='w')
        self.command_text=tk.Text(box,bg=self.palette['bg'],fg=self.palette['text'],insertbackground=self.palette['accent'],wrap='none',font=('DejaVu Sans Mono',9));self.command_text.pack(fill='both',expand=True,pady=8)
        data=self._safe_rpc('commands.get',{},fallback={});self.command_text.insert('1.0',json.dumps(data,indent=2))
        row=ttk.Frame(box,style='Panel.TFrame');row.pack(fill='x')
        ttk.Button(row,text='Save catalog',style='Accent.TButton',command=self.save_commands).pack(side='right')
        ttk.Button(row,text='Reload',command=self.page_commands).pack(side='right',padx=5)

    def save_commands(self):
        try:rpc('commands.save',{'commands':json.loads(self.command_text.get('1.0','end'))});messagebox.showinfo('Commands','Command catalog saved.')
        except Exception as exc:messagebox.showerror('Commands',str(exc))

    def page_settings(self):
        box=self.panel(self.body,'USER CONTROL PANEL');box.pack(fill='x',pady=8)
        ttk.Label(box,text='USER_CONTROL.py is the documented master settings file. Each implemented setting identifies the Python function or configuration file that applies it. Options marked RESERVED are not active.',style='Muted.TLabel',wraplength=1050).pack(anchor='w')
        row=ttk.Frame(box,style='Panel.TFrame');row.pack(fill='x',pady=12)
        ttk.Button(row,text='Edit USER_CONTROL.py',style='Accent.TButton',command=lambda:self._open_editor(ROOT/'USER_CONTROL.py')).pack(side='left')
        ttk.Button(row,text='Open camera config',command=lambda:self._open_editor(ROOT/'config'/'cameras.json')).pack(side='left',padx=6)
        ttk.Button(row,text='Open bookmarks',command=lambda:self._open_editor(ROOT/'config'/'bookmarks.json')).pack(side='left')
        ttk.Button(row,text='Open project folder',command=lambda:self._open_editor(ROOT)).pack(side='left',padx=6)
        tree=ttk.Treeview(self.body,columns=('value','description'),show='tree headings')
        tree.heading('#0',text='SETTING');tree.heading('value',text='VALUE');tree.heading('description',text='DESCRIPTION / EFFECT')
        tree.column('#0',width=300);tree.column('value',width=180);tree.column('description',width=700)
        tree.pack(fill='both',expand=True,pady=10)
        for key,val in sorted(self.control.items()):
            if key.isupper():tree.insert('', 'end',text=key,values=(str(val)[:100],'See USER_CONTROL.py comments for implementation notes.'))

    def _open_editor(self,path):
        try:
            if path.is_dir():cmd=[os.environ.get('FILE_MANAGER') or 'xdg-open',str(path)]
            else:
                editor=os.environ.get('EDITOR')
                if editor:cmd=editor.split()+[str(path)]
                elif shutil_which('gedit'):cmd=['gedit',str(path)]
                else:cmd=['xdg-open',str(path)]
            subprocess.Popen(cmd,cwd=str(ROOT),start_new_session=True)
        except Exception as exc:messagebox.showerror('Open file',str(exc))

    def page_logs(self):
        box=self.panel(self.body,'ENGINE / MEDIAMTX / CAMERA LOGS');box.pack(fill='both',expand=True,pady=8)
        self.logs_text=tk.Text(box,bg=self.palette['bg'],fg=self.palette['text'],insertbackground=self.palette['accent'],wrap='word',font=('DejaVu Sans Mono',9));self.logs_text.pack(fill='both',expand=True)
        ttk.Button(box,text='Refresh logs',command=self.refresh_logs).pack(anchor='e',pady=6);self.refresh_logs()

    def refresh_logs(self):
        try:txt=rpc('logs.tail',{'lines':500}).get('text','')
        except Exception as exc:txt=str(exc)
        if hasattr(self,'logs_text') and self.logs_text.winfo_exists():self.logs_text.delete('1.0','end');self.logs_text.insert('1.0',txt)

    def page_maintenance(self):
        box=self.panel(self.body,'SAFE MAINTENANCE');box.pack(fill='x',pady=8)
        ttk.Label(box,text='Purging is disabled by default. Preview never deletes data. Actual purge requires all switches enabled in USER_CONTROL.py, dry-run disabled, and a typed confirmation in the master terminal.',style='Muted.TLabel',wraplength=1050).pack(anchor='w',pady=4)
        ttk.Button(box,text='Preview purge candidates',style='Accent.TButton',command=self.purge_preview).pack(anchor='w',pady=8)
        self.purge_text=tk.Text(box,height=15,bg=self.palette['bg'],fg=self.palette['text'],insertbackground=self.palette['accent'],wrap='word');self.purge_text.pack(fill='x',pady=6)
        ttk.Label(box,text='Backup/restore: use the persistent master terminal commands: backup, backup with-cache, backup without-cache, restore filename.tar.gz.',style='Muted.TLabel').pack(anchor='w',pady=8)

    def purge_preview(self):
        try:r=rpc('purge.preview',{});self.purge_text.delete('1.0','end');self.purge_text.insert('1.0',r.get('report','No report.'))
        except Exception as exc:messagebox.showerror('Purge preview',str(exc))

    def close(self):
        self.persist_workspace()
        if getattr(self, 'inspector_mode', False):
            self.inspector_mode = False
            try:
                self.root.unbind_class(self._inspector_bindtag, '<ButtonPress-1>')
                self.root.unbind_class(self._inspector_bindtag, '<ButtonRelease-1>')
                self._inspector_tag_tree(self.root, False)
            except tk.TclError:
                pass
        self._restore_inspector_callback_tracking()
        self.stop_media_tiles()
        self.timeline_sync_active=False
        timeline_after = getattr(self, '_timeline_after', None)
        if timeline_after is not None:
            try:self.root.after_cancel(timeline_after)
            except tk.TclError:pass
            self._timeline_after=None
        try:self.root.destroy()
        except tk.TclError:pass


def shutil_which(name):
    import shutil
    return shutil.which(name)


def main():
    (ROOT / 'runtime').mkdir(parents=True, exist_ok=True)
    common = 'SPACE cycle pause\nm cycle mute\nf cycle fullscreen\ni script-binding stats/display-stats\nMBTN_LEFT_DBL cycle fullscreen\nq ignore\nQ ignore\n'
    (ROOT / 'runtime' / 'mpv-playback-input.conf').write_text(common + 'LEFT seek -5 exact\nRIGHT seek 5 exact\nUP seek 60 exact\nDOWN seek -60 exact\n9 add volume -5\n0 add volume 5\n[ multiply speed 0.9091\n] multiply speed 1.1\n. frame-step\n, frame-back-step\nWHEEL_UP seek 5 exact\nWHEEL_DOWN seek -5 exact\n', encoding='utf-8')
    (ROOT / 'runtime' / 'mpv-live-input.conf').write_text(common + '9 add volume -5\n0 add volume 5\n', encoding='utf-8')
    if not shutil.which('mpv'):
        raise SystemExit('mpv is required for continuous native video playback. Install it with: sudo apt install mpv')
    if not SOCKET.exists():
        raise SystemExit('The eYeYaYe engine socket is not available. Start eYeYaYe with ./launcher first.')
    root=tk.Tk();desktop=CCTVDesktop(root)
    def request_close(_signum=None,_frame=None):
        try:root.after(0,desktop.close)
        except tk.TclError:pass
    signal.signal(signal.SIGTERM,request_close)
    if hasattr(signal,'SIGHUP'):signal.signal(signal.SIGHUP,request_close)
    root.mainloop()

if __name__=='__main__':
    try:main()
    except Exception as exc:
        log_error(exc)
        try:messagebox.showerror('eYeYaYe',str(exc))
        except Exception:print(f'ERROR: {exc}',file=sys.stderr)
        raise
