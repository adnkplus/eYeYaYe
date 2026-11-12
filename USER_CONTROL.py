"""eYeYaYe — USER CONTROL FILE

THIS IS THE MAIN EDUCATIONAL CONTROL PANEL.
Edit simple values here; the application reads this file at runtime and on
configuration reload. Keep the comments: they explain what each setting does.

WHERE ACTUAL SETTINGS LIVE
--------------------------
1. USER_CONTROL.py (this file)
   RAW retention, archive day boundary, codecs, time-lapse speeds, detection,
   exclusion masks, purge policy, security defaults, UI defaults and limits.

2. config/cameras.json
   Camera IDs, camera type (rtsp/cam3), RTSP URL, username/password,
   administration-page URL and per-camera exclusion polygons.

3. config/bookmarks.json
   Playback marks, labels, protected time intervals and notes.

4. control/COMMANDS.json
   ACTUAL FFmpeg/FFprobe argument arrays used by archive, time-lapse, playback preparation, snapshots, camera probes, CAM3 publishing and media verification.
   These are arrays, not shell strings: no shell expansion is performed.

5. runtime/mediamtx.yml (generated)
   Actual MediaMTX camera routing settings; recording is disabled there because FFmpeg owns recording. Do not edit this generated
   file by hand; change the MediaMTX-related values below or camera records.

6. app/core.py, app/engine.py and app/archive.py
   Media configuration, local Unix-socket control service and processing logic.
   Changing a constant cannot create an unimplemented feature.

7. master.py, launcher and BUILD_RUNTIME.py
   Native project lifecycle, local media-runtime preparation, help, backups,
   restores and command editing. The launcher runs directly as the current user.

8. app/gui.py
   Native Tk desktop GUI with embedded mpv live/playback tiles, separate layouts,
   per-tile audio/speed/FPS controls, camera editor, cues and maintenance.

9. config/appearance.json
    Persistent desktop colors and visual effects. The native Appearance page
    edits the preset; individual tokens can be changed in this JSON file.

10. logs/ and backups/
    Runtime logs and portable archives. Backups contain credentials; protect
    and preferably encrypt them when stored on removable media.

SETTING-TO-CODE MAP — where a value actually takes effect
---------------------------------------------------------
- RECORDING_SEGMENT_SECONDS / RECORDING_FORMAT / CAMERA_DEFAULT_RTSP_TRANSPORT:
    app/core.py -> build_recording_command(); FFmpeg tee writes original MKV segments and a separate Matroska live FIFO.
- ARCHIVE_DAY_START / ARCHIVE_TIMEZONE:
    app/archive.py -> archive_window(), zone_for(); app/core.py -> archive_scheduler().
- ARCHIVE_VIDEO_* / ARCHIVE_AUDIO_* / ARCHIVE_EXTRA_ARGS:
    app/archive.py -> build(), estimate(); actual process argv comes from
    control/COMMANDS.json -> archive_clip / archive_join. Video-only archive clips are not supported.
- TIMELAPSE_REGULAR_* and TIMELAPSE_VIDEO_*:
    app/archive.py -> generate_timelapse(); actual argv is timelapse_regular.
- TIMELAPSE_INTELLIGENT_* / TIMELAPSE_MOTION_*:
    app/archive.py -> motion_profile(), intelligent_filter(),
    build_timelapse_time_map(); app/core.py schedules enabled daily TLR/TLI jobs.
    Actual argv is timelapse_intelligent.
- ENABLE_DETECTION_EXCLUSION_ZONES:
    app/archive.py -> detection_mask(); masks live in config/cameras.json
    under each camera's exclude_zones field. This is detection-only.
- ENABLE_PROTECTED_INTERVALS / ENABLE_PLAYBACK_BOOKMARKS:
    RPC validation in app/engine.py; timestamps and protection flags live in
    config/bookmarks.json; archive.py -> protected_ranges(), subtract_ranges(), purge().
- PURGE_ORIGINAL_RETENTION_DAYS / PURGE_*:
    app/archive.py -> purge(); this is the RAW minimum age before deletion eligibility.
    Actual deletion is separately disabled by default and requires verified archives,
    checksum checks, no protected cue overlap, dry-run off and typed confirmation.
- BACKUP_* / RESTORE_*:
    master.py -> create_backup(), safe_extract(), create_rollback_copy(), restore_backup().
- LIVE_DOCK_* / DEFAULT_WORKSPACE / WORKSPACE_*:
    app/gui.py -> page_live(), editable live/playback grids, synchronized 24-hour timeline and playback dock.
- Appearance values:
    config/appearance.json; validation in app/core.py; native widgets in app/gui.py.
- FFMPEG_BINARY / FFPROBE_BINARY / FFMPEG_LOGLEVEL:
    app/archive.py -> archive/time-lapse/verification and app/engine.py -> source probes.
    Live and playback video are continuous mpv players; not FFmpeg still-frame extraction.
- CAM3 RTMP bridge (unchanged):
    cam3/rtmp_live_bridge.py; it loads cam3/rtmp_cam.py and publishes to MediaMTX.
    All CAM1/CAM2 RTSP credentials and CAM3 UDX credentials/settings are in
    config/cameras.json; the bridge uses the project-local media runtime.
- Live and playback tiles:
    app/gui.py -> MpvPlayer / VideoTile; each tile is a real embedded mpv player.

USER_CONTROL.py is parsed as literal values; do not add imports, functions,
expressions or executable Python. Comments and this documentation are safe.
An unknown setting can remain here for education, but it has no effect unless
its named reader is implemented in the code. Such values are marked RESERVED.

SAFETY
------
- Never remove the backup before checking it.
- PURGE is disabled by default and runs in dry-run mode by default.
- Original recordings are never deleted unless the separate deletion switch
  is explicitly enabled AND a verified archive exists AND no protection mark
  overlaps the source segment.
- The source camera stream is recorded by one FFmpeg process per camera with -c:v copy, -c:a copy and -copyinkf (initial non-key packets are retained). A separate one-way Matroska FIFO feeds the live MPV viewer; MPV never opens a second camera connection.
- CAM3 bridge and its RTMP reference client are both included under cam3/.
  All camera credentials and the CAM3 UDX stream settings are in config/cameras.json.
- Backups include camera credentials. Store them securely.
"""

# ============================================================================
# 00 — MASTER FEATURE SWITCHES
# These switches are intentionally at the top for quick enable/disable.
# ============================================================================
ENABLE_ORIGINAL_RECORDING = True
ENABLE_DAILY_ARCHIVE = True
ENABLE_REGULAR_TIMELAPSE = False
ENABLE_INTELLIGENT_TIMELAPSE = True
ENABLE_MOTION_DETECTION = True        # IMPLEMENTED: grayscale frame-difference sampling for intelligent time-lapse.
ENABLE_OBJECT_DETECTION = False       # RESERVED: no people/vehicle classifier is bundled in this build. Intelligent time-lapse currently uses pixel motion only.
ENABLE_DETECTION_EXCLUSION_ZONES = True  # IMPLEMENTED for motion-based intelligent time-lapse.
ENABLE_ZONE_BASED_ARCHIVE_COMPRESSION = False  # RESERVED: polygon/ROI quality control is not implemented.
ENABLE_CAMERA_WEB_ADMIN_LINKS = True
ENABLE_PLAYBACK_BOOKMARKS = True
ENABLE_PROTECTED_INTERVALS = True
AUDIO_DISABLE_ABOVE_SPEED = False      # Explicitly do NOT auto-mute at high playback speed.

# Camera/service tuning ideas below are RESERVED in this build unless marked IMPLEMENTED;
# the MediaMTX supervisor currently owns reconnection policy.
CAMERA_CONNECT_TIMEOUT_SECONDS = 8       # RESERVED: connection timeout is currently owned by MediaMTX/FFmpeg.
CAMERA_RECONNECT_INITIAL_SECONDS = 2     # RESERVED: retry backoff is currently owned by MediaMTX/bridge supervisor.
CAMERA_RECONNECT_MAX_SECONDS = 30         # RESERVED: not consumed by the supervisor yet.
CAMERA_RECONNECT_MULTIPLIER = 1.8         # RESERVED: not consumed by the supervisor yet.
CAMERA_STATUS_PROBE_SECONDS = 30           # IMPLEMENTED: ffprobe verifies each MediaMTX camera path and its video/audio streams.
CAMERA_MAX_PARALLEL_STARTS = 3             # RESERVED: camera start orchestration is currently serial.
CAMERA_ALLOW_SELF_SIGNED_TLS = False       # RESERVED: no RTSP TLS override is implemented.

# ============================================================================
# 01 — PROJECT / NATIVE DESKTOP GUI
# ============================================================================
GUI_FRAMEWORK = "tkinter"           # Native Linux desktop window.
GUI_VIDEO_BACKEND = "mpv embedded video output"  # Real continuous RTSP/file video; no still-frame extraction.
GUI_MPV_BINARY = "/usr/bin/mpv"       # Host executable; install with: sudo apt install mpv.
GUI_MPV_CACHE_SECONDS = 2.5           # Playback-player read-ahead; live preview receives the recorder Matroska FIFO.
GUI_MPV_HWDEC = "auto-safe"           # Safe automatic hardware decode where supported; falls back to software.
GUI_MPV_VIDEO_SYNC = "display-resample"
GUI_MPV_FULLSCREEN_ENABLED = True       # Per-tile double-click or full-screen button.
GUI_MPV_RTSP_TRANSPORT = "tcp"          # Reserved: live MPV consumes the local one-way Matroska pipe, not RTSP.
GUI_DEFAULT_TILE_VOLUME = 80           # Per-tile initial volume, 0-100.
GUI_WHEEL_VOLUME_STEP = 5             # Volume step when wheel is over a tile's volume control.
GUI_WHEEL_SPEED_STEP = 0.25           # Fine speed selection is provided by the adjacent values in GUI_SPEED_OPTIONS.
GUI_WHEEL_SEEK_SECONDS = 5            # Wheel over a playback video seeks this many seconds.
GUI_DEFAULT_FPS = "Auto"         # Default display frame-rate cap for every tile; Auto follows the source.
GUI_PLAYBACK_FPS_DEFAULT = "Auto"  # Independent per-playback-tile display frame-rate cap.
GUI_FPS_OPTIONS = ["Auto", "15", "25", "30", "50", "60"]
GUI_SPEED_OPTIONS = [0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0, 4.0, 8.0, 16.0, 30.0, 60.0]
DEFAULT_WORKSPACE = "playback-live"   # playback-live, live-only, settings-live, playback-only.
LIVE_DOCK_DEFAULT_WIDTH_PERCENT = 30
LIVE_DOCK_RESIZABLE = True
LIVE_DOCK_COLLAPSIBLE = True
LIVE_LAYOUT_DEFAULT = "manual"
PLAYBACK_LAYOUT_DEFAULT = "manual"
PLAYBACK_WALL_VISIBLE = True
LIVE_LAYOUTS = []  # Deprecated: enter Live Dock columns/rows directly in the GUI.
MAX_CAMERAS_SOFT_LIMIT = 32         # UI warning threshold; not a hard-coded camera count.
WORKSPACE_REMEMBER_LAYOUT = True  # IMPLEMENTED: config/workspace.json stores page, geometry, split and manual grid dimensions.
WORKSPACE_LIVE_DOCK_POSITION = "right"  # right, bottom, hidden; applied as first-visit default.
WORKSPACE_LIVE_DOCK_MIN_WIDTH_PERCENT = 20
WORKSPACE_LIVE_DOCK_MAX_WIDTH_PERCENT = 55
WORKSPACE_SHOW_STATUS_DETAILS = True

# ============================================================================
# 02 — RECORDING: ORIGINAL CAMERA STREAM
# ============================================================================
RECORDING_SEGMENT_SECONDS = 600     # FFmpeg segment muxer target; boundaries occur at source keyframes.
RECORDING_FORMAT = "mkv"             # Original video/audio packets are stream-copied into Matroska segments.
LIVE_PIPE_QUEUE_PACKETS = 512        # Isolated preview FIFO queue; preview packets drop on overflow so recording is not back-pressured.
RECORDING_TRANSPORT = "tcp"         # Reference only; active setting is CAMERA_DEFAULT_RTSP_TRANSPORT below.
RECORDING_MIN_FREE_GB_WARNING = 20
RECORDING_STOP_ON_CRITICAL_FREE_GB = 5   # RESERVED: warns are available; automatic stop is intentionally not enabled.
RECORDING_MIN_FREE_PERCENT_WARNING = 5   # RESERVED: current warning uses RECORDING_MIN_FREE_GB_WARNING.
RECORDING_KEEP_CAMERA_AUDIO = True        # Incoming audio tracks are preserved unchanged by MediaMTX; CAM3 currently has no microphone.
RECORDING_ALLOW_OVERWRITE = False          # RESERVED: original ingest never overwrites existing segment files.
RECORDING_FILENAME_TIMESTAMP_SOURCE = "server"  # Active invariant: filenames use MediaMTX/server recording time.
RECORDING_FILE_PERMISSION_MODE = 0o640    # RESERVED: recorder-created file mode is set by container/MediaMTX umask.
RECORDING_DIRECTORY_PERMISSION_MODE = 0o750 # RESERVED: recorder directory mode follows host/container permissions.

# ============================================================================
# 03 — 24-HOUR ARCHIVE BOUNDARY
# A recording day starts at this local wall-clock time. Default: 05:00 local.
# Example "03:00" means a day runs 03:00 -> 03:00 the following day.
# ============================================================================
ARCHIVE_DAY_START = "05:00"
ARCHIVE_TIMEZONE = "local"           # local or an IANA timezone e.g. Europe/Paris.
ARCHIVE_WAIT_UNTIL_DAY_CLOSED = True
ARCHIVE_OVERWRITE_EXISTING = False
ARCHIVE_AUTO_BUILD = True           # Build each 05:00-to-05:00 archive day after the RAW waiting period.
ARCHIVE_AUTO_BUILD_DELAY_DAYS = 2   # Wait two full days after the archive day closes before creating the lossy compressed MER archive; RAW stays untouched.
ARCHIVE_AUTO_SCAN_SECONDS = 1800   # Check for work every 30 minutes; at most one day/camera per scan.
ARCHIVE_MAX_PARALLEL_JOBS = 1              # Fixed safety limit: scheduler runs only one job per scan; this value is informational.
ARCHIVE_VERIFY_DECODE_FRAMES = 10
ARCHIVE_VERIFY_REQUIRE_VIDEO = True        # Hard invariant: verify() always requires a decodable video stream.
ARCHIVE_PRESERVE_SOURCE_AUDIO_BY_DEFAULT = True # Reference only; actual control is ARCHIVE_AUDIO_CODEC.
ARCHIVE_KEEP_MANIFESTS = True               # Hard invariant: purge relies on archive manifests.
ARCHIVE_KEEP_SOURCE_CHECKSUMS = True       # Hard invariant: purge relies on recorded source hashes.
ARCHIVE_RETRY_FAILED_JOBS = True           # Retry occurs on later scheduler scans; no separate retry queue exists.
ARCHIVE_RETRY_DELAY_MINUTES = 30           # RESERVED: retry cadence is currently ARCHIVE_AUTO_SCAN_SECONDS.

# ============================================================================
# 04 — ARCHIVE COMPRESSION
# Applies only to generated archive outputs. It never re-encodes original files.
# ============================================================================
ARCHIVE_VIDEO_CODEC = "libx264"     # Examples: libx265, libx264. Hardware codecs are host-dependent.
ARCHIVE_VIDEO_CRF = 23              # Lower = better quality/larger file; higher = smaller/worse.
ARCHIVE_VIDEO_PRESET = "medium"     # Slower presets generally compress better but take longer.
ARCHIVE_AUDIO_CODEC = "copy"        # Preserve source audio in MER archive clips; video-only archive output is disabled.
ARCHIVE_AUDIO_BITRATE = "96k"
ARCHIVE_PIXEL_FORMAT = "yuv420p"
ARCHIVE_ESTIMATE_SAMPLE_SECONDS = 300
ARCHIVE_EXTRA_ARGS = []             # Extra FFmpeg args appended by control/COMMANDS.json template.
ARCHIVE_ESTIMATE_MIN_SAMPLE_SECONDS = 60
ARCHIVE_ESTIMATE_MAX_SAMPLE_SECONDS = 900
ARCHIVE_OUTPUT_EXTENSION = "mp4"         # Fixed in this build: archive and time-lapse outputs use MP4.
ARCHIVE_CONCAT_REENCODE_FALLBACK = False   # RESERVED: incompatible join fails safely; no hidden re-encode fallback exists in this build.

# RESERVED: regional compression quality settings. The actual feature switch is above; do not enable until polygon-to-encoder ROI mapping and visual tests are added. Setting the flag True now does not change archive quality.
ZONE_COMPRESSION_STRENGTH = 1.25
ZONE_COMPRESSION_MIN_QP_OFFSET = 2
ZONE_COMPRESSION_MAX_QP_OFFSET = 12
ZONE_COMPRESSION_REQUIRE_VISUAL_REVIEW = True
ZONE_COMPRESSION_ONLY_AFTER_DAYS = 7

# ============================================================================
# 05 — TIME-LAPSE OUTPUTS
# Both are separate from the normal archive and can be independently enabled.
# ============================================================================
TIMELAPSE_REGULAR_SPEED = 120
TIMELAPSE_OUTPUT_FPS = 25
TIMELAPSE_VIDEO_CODEC = "libx264"
TIMELAPSE_CRF = 24
TIMELAPSE_PRESET = "fast"
TIMELAPSE_AUDIO_CODEC = "aac"
TIMELAPSE_AUDIO_BITRATE = "96k"
TIMELAPSE_EXTRA_ARGS = []
TIMELAPSE_INTELLIGENT_QUIET_SPEED = 120
TIMELAPSE_INTELLIGENT_LOW_MOTION_SPEED = 30
TIMELAPSE_INTELLIGENT_ACTIVITY_SPEED = 5
TIMELAPSE_INTELLIGENT_HIGH_ACTIVITY_SPEED = 2
TIMELAPSE_MOTION_SAMPLE_FPS = 1
TIMELAPSE_MOTION_SAMPLE_WIDTH = 160
TIMELAPSE_MOTION_SAMPLE_HEIGHT = 90
TIMELAPSE_MOTION_PIXEL_DIFFERENCE = 22
TIMELAPSE_MOTION_CHANGED_PIXEL_PERCENT = 1.5
TIMELAPSE_MOTION_HIGH_PERCENT = 12.0
TIMELAPSE_AUTO_BUILD = True         # Build daily time-lapses for each enabled mode after its archive day closes.
TIMELAPSE_KEEP_REGULAR_AND_INTELLIGENT = True # RESERVED: generated versions are retained unless manually removed.
TIMELAPSE_MAP_ORIGINAL_TIMESTAMPS = True
TIMELAPSE_MOTION_MIN_DURATION_SECONDS = 1  # RESERVED: event smoothing is not wired into the motion profile yet.
TIMELAPSE_MOTION_SMOOTHING_WINDOW_SECONDS = 3 # RESERVED: motion bins are currently classified independently.
TIMELAPSE_ACTIVITY_HYSTERESIS_SECONDS = 2  # RESERVED: activity hysteresis is not implemented yet.
TIMELAPSE_QUIET_SPEED_LIMIT = 300           # RESERVED: quiet speed is set by TIMELAPSE_INTELLIGENT_QUIET_SPEED.
TIMELAPSE_MAX_OUTPUT_FPS = 30                # RESERVED: use TIMELAPSE_OUTPUT_FPS for the active output rate.

# ============================================================================
# 06 — DETECTION AND PER-CAMERA EXCLUSION ZONES
# Polygons are stored in config/cameras.json as normalized 0..1 image points.
# One camera may have many polygons. Exclusion affects detection only, never
# the original 24-hour archive or original camera recordings.
# ============================================================================
OBJECT_DETECTION_MODEL_PATH = "models/objects.onnx" # RESERVED: no object-recognition model is bundled.
OBJECT_DETECTION_LABELS = ["person", "car", "truck", "bus", "bicycle", "motorcycle"]
OBJECT_DETECTION_CONFIDENCE = 0.45
OBJECT_DETECTION_DEVICE = "cpu"

# ============================================================================
# 07 — PLAYBACK / SYNCHRONIZATION
# ============================================================================
PLAYBACK_MAX_SPEED = 60
PLAYBACK_SHOW_ALL_CAMERAS = True             # Current behavior: timeline rows include every enabled camera.
PLAYBACK_SYNC_CLOCK_SOURCE = "server"  # Current timeline uses server/media segment timestamps.
PLAYBACK_ALLOW_INDIVIDUAL_CAMERA_OFFSET = False # Clock offsets stay advisory; the shared timeline uses server/media timestamps.
PLAYBACK_SHOW_CAMERA_CLOCK_WARNING = True
PLAYBACK_DEFAULT_SPEED = 1.0
PLAYBACK_PRELOAD_SECONDS = 3                 # RESERVED: desktop playback preloading is controlled by the desktop.
PLAYBACK_MAX_DECODE_THREADS = 0              # RESERVED: per-playback decoder thread limit is not wired yet.
PLAYBACK_CACHE_ENABLED = True                # Native playback cache switch; archive preparation still uses cache/playback.
PLAYBACK_CACHE_MAX_GB = 10                    # RESERVED: quota enforcement is not active yet.
PLAYBACK_CACHE_RETENTION_DAYS = 3             # Reference only; active cache purge age is PURGE_CACHE_RETENTION_DAYS.

# ============================================================================
# 08 — PURGING / RETENTION CLEANUP
# This is intentionally conservative. `purge preview` never deletes anything.
# Daily archive creation does NOT itself delete original camera recordings.
# To authorize deletion, you must deliberately change ALL three authorization
# switches below, set the dry-run switch to False, restart/reload the app, run
# `purge preview`, inspect the report, then run `purge execute` in the terminal.
# The purge engine additionally requires a verified archive manifest, intact
# archive part files/checksums, an unchanged source checksum, full segment
# coverage, age beyond the retention period and no protected interval overlap.
# It processes no more than PURGE_MAX_FILES_PER_RUN source segments per run.
# A segment intersecting even part of a protected interval is retained whole.
# ============================================================================
PURGE_ENABLED = False
PURGE_DRY_RUN = True
PURGE_ORIGINAL_AFTER_VERIFIED_ARCHIVE = False
PURGE_ORIGINAL_RETENTION_DAYS = 2  # Keep RAW/original footage for at least this many days before it may qualify for safe purge.
PURGE_KEEP_PROTECTED_ORIGINALS = True
PURGE_KEEP_AT_LEAST_ONE_VERIFIED_COPY = True  # Hard safety requirement; set False is not supported by the purge engine.
PURGE_MAX_FILES_PER_RUN = 500          # Limit deletion batch size; use smaller values on first evaluation.
PURGE_REQUIRE_TYPED_CONFIRMATION = True  # Keep True; exact phrase is also required by master terminal.
PURGE_CONFIRMATION_TEXT = "PURGE CCTV DATA"

# Separate cleanup classes. Each is OFF until deliberately enabled.
PURGE_CACHE_PLAYBACK_ENABLED = False       # Only cache/playback, cache/estimate and cache/timelapse-work.
PURGE_CACHE_RETENTION_DAYS = 7
PURGE_TEMP_JOB_FILES_ENABLED = False       # Only old *.partial*, *.tmp and known temporary job folders.
PURGE_TEMP_RETENTION_HOURS = 48
PURGE_ROTATED_LOGS_ENABLED = False        # Only rotated/archived logs; never current application.log or mediamtx.log.
PURGE_LOG_RETENTION_DAYS = 30
PURGE_REMOVE_EMPTY_JOB_DIRECTORIES = True
PURGE_KEEP_BACKUPS_ALWAYS = True          # Hard invariant: backups/ is never scanned or deleted.
PURGE_KEEP_BUILD_CACHE_ALWAYS = True          # Runtime download/build caches are never purged by the cleanup engine.
PURGE_MAX_TOTAL_FILES_PER_RUN = 500
PURGE_REPORT_MAX_ITEMS = 500

# ============================================================================
# 09 — BACKUP / RESTORE
# ============================================================================
BACKUP_INCLUDE_CACHE_DEFAULT = False
BACKUP_DIRECTORY = "backups"
RESTORE_CREATE_ROLLBACK_COPY = True

# ============================================================================
# 10 — CAMERA CONNECTIONS AND RTSP
# ============================================================================
CAMERA_DEFAULT_RTSP_TRANSPORT = "tcp"
CAMERA_CLOCK_WARNING_SECONDS = 5  # Discreet warning threshold for the manual alignment offset.
CAMERA_CREDENTIAL_FILE_MODE = 0o600      # Owner read/write only; camera passwords are stored in config/cameras.json.

# ============================================================================
# 11 — MEDIA / FFMPEG EXECUTION
# Actual templates for every FFmpeg/FFprobe invocation are in control/COMMANDS.json.
# Commands run as argument arrays, not through a shell. This section defines common defaults.
# ============================================================================
# Legacy defaults are mapped automatically to cam3/CAM3_LIVE_STANDALONE/bin/.
# The engine refuses to start until those project-local executables exist.
FFMPEG_BINARY = ""
FFPROBE_BINARY = ""
FFMPEG_LOGLEVEL = "warning"
FFMPEG_ARCHIVE_EXTRA_ARGS = []
FFMPEG_TIMELAPSE_EXTRA_ARGS = []

# ============================================================================
# 12 — RESOURCE LIMITS / MAINTENANCE
# ============================================================================
STATUS_REFRESH_SECONDS = 5
MAX_ARCHIVE_JOBS_PER_SCAN = 1             # Internal scheduler safety limit; do not increase until tested.
PLAYBACK_CACHE_MAX_GB = 10                    # RESERVED: quota enforcement is not active yet.

# ============================================================================
# 13 — VISUAL SETTINGS LOCATION
# Actual visual values are in config/appearance.json. The Appearance page
# edits that file. Native Tk widget styles and mpv tile controls are in
# app/gui.py. This split lets you alter colors without
# editing the application logic or losing changes during application updates.
#
# 14 — FEATURES RESERVED FOR A LATER BUILD (NOT ACTIVE IN v2.0)
# Keep disabled until a model/encoder is installed and a validation test passes.
# ============================================================================
ENABLE_GPU_ARCHIVE_ENCODING = False  # RESERVED: GPU encoder selection is not wired into this build.
GPU_ENCODER = "hevc_nvenc"           # Example encoder name; no hardware is assumed.
ENABLE_GPU_OBJECT_DETECTION = False  # RESERVED: no model runtime is bundled.
ENABLE_REMOTE_ACCESS = False         # RESERVED: ports remain bound to loopback on the host.
ENABLE_EXTERNAL_CLOUD_SERVICES = False

# ==========================================================================
# 14 — CUE EXPORT STUDIO
# Final MP4 exports are written below data/exports; scratch files stay in cache/export-work.
# The engine validates cue IDs and camera IDs before running the FFmpeg templates.
# ==========================================================================
EXPORT_DEFAULT_RESOLUTION = "1920x1080"
EXPORT_DEFAULT_FPS = 25
EXPORT_DEFAULT_CRF = 20              # H.264 quality: lower is larger/better; range 16-30.
EXPORT_DEFAULT_PRESET = "medium"
EXPORT_DEFAULT_AUDIO_MODE = "mix"    # mix, first, mute.
EXPORT_AUDIO_BITRATE = "192k"
EXPORT_ALLOW_TIMELAPSE_DEFAULT = False  # Off by default; user must explicitly accept reduced temporal detail.
