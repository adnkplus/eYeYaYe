# eYeYaYe — CCTV Desktop

**Version:** 9.0.0  
**Platform:** Linux amd64 / x86_64 (Parrot OS / Debian 13 family)  
**Interface:** Native Tk desktop with continuous embedded MPV players. No application website, browser GUI, local HTTP control page or REST API.

## First-time runtime preparation

The release contains the builder and the unchanged CAM3 bridge/client sources.
Before the first launch, build the project-local media binaries once (internet
is required only for this explicit step):

```bash
python3 BUILD_RUNTIME.py
```

This downloads MediaMTX v1.21.3 and static FFmpeg/FFprobe v9.0.2, verifies both
upstream SHA-256 manifests, then creates `cam3/CAM3_LIVE_STANDALONE/bin/`
containing `mediamtx`, `ffmpeg` and `ffprobe`. It also copies the CAM3 bridge/client
plus the project command catalog. The
builder validates the binaries before replacing any previous bundle. The
launcher never downloads software.

Host requirements are Linux amd64, Python 3 with Tkinter, and MPV for continuous
native video playback. MediaMTX, FFmpeg and FFprobe do not need to be installed
on the host.

## Start and stop

```bash
./launcher           # Starts the master terminal and opens the GUI by default
./launcher -G        # Same, explicitly graphical
./launcher -T        # Terminal-only; type gui to open the GUI
```

The build creates `cam3/CAM3_LIVE_STANDALONE/` with its own `bin/mediamtx`, `bin/ffmpeg` and `bin/ffprobe`. It does not install MediaMTX or FFmpeg into `/usr/local/bin` or `/usr/bin`, and it does not start a server. The main engine launches the single bundled MediaMTX when the application starts. The launcher itself performs no downloads.

The master terminal owns this project's lifecycle. Closing the GUI keeps capture, RTSP routing and recording running. Navigating between Cameras, Cue Library, Export Studio, Archives, Settings and other sections does not stop the Live/Playback players. **Show Live Dock** controls only the local live preview players.

Typing `quit`, pressing Ctrl+C, or closing the master terminal stops the GUI players, local Python engine, CAM3 bridge, FFmpeg recorders and the one project-contained MediaMTX process. The application runs as the current user; it does not require Docker, a container daemon, group changes or elevated launch privileges. Tkinter and MPV are host desktop dependencies; install them once through the Linux package manager if they are missing.


## V9.0 — Live switches, archive folders and authoritative Inspector

The Live & Playback page now has an independent **LIVE ON/OFF** switch for each
configured camera. These are viewer switches: turning one off closes only that
camera's local MPV live viewer. Camera ingest, recording, CAM3 UDX, and the shared
project-local MediaMTX routing remain running. The individual states are saved in
`config/workspace.json`. The existing **Show Live Dock** switch still controls the
whole live panel.

The Archives table now shows the containing directory for every recording. Select
a row and use **OPEN CONTAINING DIRECTORY**, or double-click the directory cell,
to open it in the desktop file manager.

The bottom of the left navigation sidebar contains the **INSPECTOR** control and a
small source preview. Activate it, then click an application control or area. The
inspector resolves the Tk callback (or the active page builder) to the actual
project-owned Python function and shows its file, line range and source. The preview
expands while a source is selected; the full function is editable in the inspector
window. The four actions are:

- **OK** — syntax-check, atomically commit the edited function and create a rollback
  copy in `backups/inspector/`. A running Python process does not hot-reload source;
  use Restart Everything to load the committed code.
- **Cancel** — discard the staged editor text without writing it to the project.
- **Restart Everything** — commit valid pending edits (when present), then ask the
  master terminal to stop and restart the GUI, engine, camera bridge, recorders and
  the one project-contained MediaMTX instance. The master terminal stays open.
- **Dry Run** — apply the staged edit only in a disposable project copy, compile all
  Python source files and run the complete test suite. The live project is not
  modified. The inspector reports the compile/test result.

Source editing is limited to Python functions that can be resolved from a clicked
control's registered callback or its page builder. Controls without an editable
project callback still show their widget identity and page. The editor never silently
claims that a non-code setting or external binary has been changed.

## Cameras and media routing

CAM1/CAM2 use the configured RTSP URLs in `config/cameras.json`. CAM3 uses the bundled proprietary UDX/RTMP challenge/login client `cam3/rtmp_cam.py` and the RTMP-to-RTSP bridge. The CAM3 protocol client is retained from the preceding release; no generic RTSP source URL or generic RTMP login is substituted. The supplied camera credentials are stored in the camera configuration as requested.

MediaMTX remains the shared local RTSP router at `127.0.0.1:8554`; its built-in recorder is disabled. One FFmpeg process per enabled camera reads that local MediaMTX path exactly once. That process uses the supplied recorder mechanism and `-fps_mode:v passthrough -c:v copy -c:a copy -copyinkf`, then duplicates the same encoded packets with the FFmpeg tee muxer: one branch creates RAW MKV segments and the other writes to a one-way Matroska FIFO for the Live Dock. There is no second camera connection for viewing and no video or audio re-encoding in RAW recording. MPV reads the FIFO only; it does not open RTSP. Live FIFO and recorded-file playback use MPV's Lavf demuxer; live stdin additionally forces Matroska format. An optional separate RTSP audio-only source can still be attached to a camera through the camera configuration.

The generated MediaMTX recording path follows the requested order:

```text
data/recordings/original/cam1/143200_02112026_RAW_C1.mkv
```

The local clock comes first, followed by day/month/year, recording type, and camera code. Files are stored directly in the per-camera original-recordings directory, following the supplied recorder's folder layout. The four-digit year is written in the filename so the date is self-contained.

## Live and Playback workspace

Live Dock and Playback Wall have separate manually entered row/column grids. There are no fixed grid presets. Enabled cameras occupy cells in configuration order; the layout dimensions are yours to choose. Grid sizes, window geometry, selected section, split orientation and panel visibility are saved to `config/workspace.json`.

The media page is created once and remains alive while other sections are shown. Grid changes re-layout existing tiles instead of restarting MPV. Each live or playback tile uses its own embedded continuous MPV player and IPC socket. Live MPV reads the one-way Matroska FIFO fed by the same per-camera FFmpeg process that records the RAW MKV files; playback MPV opens the selected media file. The GUI does not open a second camera/RTSP session. There is no periodic still-image extraction, PNG-frame transport or screenshot-based playback. A one-shot snapshot is used only for drawing detection-exclusion zones.

Every tile has its own visible player state and controls: recording picker, play/pause/stop, audio track, mute/volume, speed, seek and FPS cap. Playback MPV starts in idle mode and receives its file source only after its IPC socket is ready; live MPV reads from its Matroska FIFO through stdin; per-player MPV logs and the master-launched GUI console log expose startup failures. The video surface expands with the tile instead of retaining a fixed 190-pixel height, and stale empty grid rows/columns no longer reserve space. FPS is an actual MPV video-output frame-rate cap, separate from playback speed. Double-click any video surface to toggle fullscreen; double-click again or press Escape to return.

### Synchronized 24-hour playback timeline

The timeline shows all enabled cameras against one shared server/media clock. The default archive day runs from **05:00 to 05:00** the following day; its start time and timezone are configurable in `USER_CONTROL.py`. Select an archive date or move to the previous/next day. Clicking the timeline loads the corresponding point for each camera. Playback prefers available RAW footage, then MER, then TLI/TLR time-lapse sources. The timeline continues across segment and archive-day boundaries when the next recorded interval is available.

Camera clock offsets can be recorded and displayed as a small advisory warning. Playback alignment uses the common server/media timestamps rather than forcing unverified offsets onto each stream.

### Keyboard and mouse controls

| Key / gesture | Action |
|---|---|
| Space | Play / pause |
| Left / Right | Seek backward / forward 5 seconds |
| Down / Up | Seek backward / forward 60 seconds |
| `m` | Mute / unmute |
| `f` or double-click | Fullscreen toggle |
| `i` | MPV statistics overlay |
| `[` / `]` | Decrease / increase playback speed |
| `9` / `0` | Decrease / increase volume |
| `.` / `,` | Step forward / backward one frame |
| Mouse wheel over playback video | Seek ±5 seconds |
| Mouse wheel over speed or volume | Adjust that control |

`q` does not exit the application. Use `quit` in the supervising terminal to stop eYeYaYe.

## On-video technical statistics

Every Live/Playback tile has a **STATS** toggle. It displays available video/audio codec, dimensions, source/output FPS, bitrate, container/demuxer, playback position, cache, dropped frames and volume. Press STATS again or `i` to hide it.

## Recording, daily archives and filenames

Original streams are recorded by the per-camera FFmpeg process into MKV using the exact stream-copy flags `-c:v copy -c:a copy -copyinkf`. Encoded H.264/HEVC video and AAC audio are not re-encoded for RAW. The 600-second FFmpeg segment target closes at a source keyframe; without re-encoding, a segment boundary cannot be forced between camera keyframes, so actual duration can exceed ten minutes by the GOP interval. Daily MER archive files remain a separate, lossy MP4 encode and are held until **two full days after the 05:00 archive day closes**. Building an archive does not delete or modify its RAW source files. Video-only archive clips are disabled.

The default RAW minimum-retention setting is **two days**. Purging is disabled and dry-run is enabled by default. Original segments are eligible for deletion only if the separate purge switches are deliberately enabled, a verified archive exists, the source checksum is unchanged, the retention period has elapsed and no protected cue overlaps the segment.

New filename order is **HHMMSS, DDMMYYYY, type, camera**:

- `143200_02112026_RAW_C1.mkv` — original camera stream, with encoded video and audio copied unchanged.
- `050000_02112026_MER_C1_P01.mp4` — merged daily archive part.
- `050000_02112026_TLR_C1.mp4` — regular time-lapse.
- `050000_02112026_TLI_C1.mp4` — intelligent time-lapse.

Earlier V3/V4/V6 naming formats remain readable. Existing recordings are indexed in place; the application does not rename them during startup.

## Time-lapse generation

The normal MER archive is independent of the time-lapse outputs. Regular TLR and intelligent TLI are independently selectable in the top-level switches in `USER_CONTROL.py`. The default is intelligent TLI enabled and regular TLR disabled. With `TIMELAPSE_AUTO_BUILD=True`, each enabled mode is generated once for a closed archive day. The manual Archives page and terminal commands remain available.

Intelligent time-lapse adapts speed using sampled image motion and the configured exclusion polygons. These are pixel-motion regions, not semantic person/vehicle recognition; no object-detection model is bundled. Exclusion zones affect time-lapse motion analysis only. Original 24-hour recordings and normal MER archives retain everything. The separate zone-based archive compression switch remains disabled/reserved.

Time-lapse manifests include a source-time map so the playback interface can jump from an accelerated moment to the corresponding normal-speed source time when that source is available.

## Cue Library and Export Studio

Saved cues contain timestamps, camera references, labels and comments—not only a fixed filename. Saving a cue protects the corresponding source interval from purge. The Cue Library keeps cues available for later use and reports which source is available (RAW, MER or time-lapse).

Export Studio can assemble multiple cue sections in a chosen order, with one, two or three synchronized camera panes per section. It creates a portable H.264/AAC MP4, verifies the resulting streams, and writes an export manifest. If only a time-lapse source remains, it warns that motion is accelerated and temporal detail is reduced.

## Configuration, commands and backups

`USER_CONTROL.py` is the educational settings file. Its comments map active settings to their implementing modules and distinguish reserved settings from implemented ones. `control/COMMANDS.json` contains the actual FFmpeg/FFprobe arguments used by archive, time-lapse, snapshots, probes, audio muxing and CAM3 publishing. Commands are editable; the camera snapshot command is a one-shot zone-calibration image only.

All project configuration, recordings, logs, cache, runtime files and backups are under this project directory. MediaMTX, FFmpeg and FFprobe are always launched from `cam3/CAM3_LIVE_STANDALONE/bin/`; no host-wide MediaMTX/FFmpeg installation is used. CAM1, CAM2 and the CAM3 publisher use one shared local MediaMTX process at `127.0.0.1:8554`. The CAM3 bridge only publishes to that server and never starts a second instance.

Use `backup` in the master terminal. It can include or exclude the builder download cache. Backups include camera credentials and the generated local media binaries; keep them private. Restore validates the backup, creates a rollback copy and restores within the project directory. Obsolete container descriptor files from an older project copy are removed; any non-empty legacy data folder is left untouched.

## Project-contained media runtime

The only MediaMTX executable used by the application is:

```text
cam3/CAM3_LIVE_STANDALONE/bin/mediamtx
```

The engine configuration is generated at `runtime/mediamtx.yml`. It is passed directly to that bundled executable. Do not run `/usr/local/bin/mediamtx`, install another MediaMTX service, or start a second server. To rebuild missing or outdated runtime binaries, run `python3 BUILD_RUNTIME.py` from the project root, then restart the application.

## Verification scope

The automated suite covers MediaMTX template generation, V3/V4 filename compatibility, camera credential editing, CAM3 client integrity, MPV player lifecycle and audio controls, continuous-player architecture, GUI navigation persistence, timeline mapping, cue protection, archive indexing, workspace persistence, backup/restore safety and synthetic MP4 export with H.264 video and AAC audio.

Actual camera connectivity, CAM3 authentication on the target camera, host audio-device output, GPU decode and long-duration playback still require testing on the target PC.
