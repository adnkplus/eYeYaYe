#!/usr/bin/env python3
"""Build eYeYaYe's project-local MediaMTX/FFmpeg runtime bundle.

Run this once manually when preparing the project. The application launcher
never downloads binaries. After a successful build, runtime operation is local
and the entire CAM3 media toolchain lives below cam3/CAM3_LIVE_STANDALONE/.
"""
from __future__ import annotations
import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parent
CAM3 = ROOT / 'cam3'
TARGET = CAM3 / 'CAM3_LIVE_STANDALONE'
MEDIAMTX_VERSION = 'v1.21.3'
FFMPEG_VERSION = 'latest'
FFMPEG_VERSION_EXPECTED = None  # Keep None to follow binmgr's latest release; set a string to pin a version.
MEDIAMTX_URLS = {
    'amd64': f'https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_linux_amd64.tar.gz',
    'arm64': f'https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}/mediamtx_{MEDIAMTX_VERSION}_linux_arm64.tar.gz',
}
FFMPEG_URLS = {
    'amd64': 'https://github.com/binmgr/ffmpeg/releases/latest/download/ffmpeg-linux-amd64.tar.gz',
    'arm64': 'https://github.com/binmgr/ffmpeg/releases/latest/download/ffmpeg-linux-arm64.tar.gz',
}
FFMPEG_MANIFEST_URL = 'https://github.com/binmgr/ffmpeg/releases/latest/download/SHA256SUMS.txt'


def say(message: str) -> None:
    print(message, flush=True)


def arch_name() -> str:
    machine = platform.machine().lower()
    if machine in ('x86_64', 'amd64'):
        return 'amd64'
    if machine in ('aarch64', 'arm64'):
        return 'arm64'
    raise RuntimeError(f'Unsupported Linux architecture: {machine}. Supported: amd64 and arm64.')


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def verify_manifest_checksum(manifest_url: str, archive_path: Path, manifest_path: Path, force: bool = False) -> str:
    """Verify an upstream SHA-256 manifest entry before extracting binaries."""
    download(manifest_url, manifest_path, force=force, minimum_size=32)
    expected = None
    filename = archive_path.name
    for line in manifest_path.read_text(encoding='utf-8', errors='replace').splitlines():
        fields = line.strip().split()
        if len(fields) >= 2 and fields[-1].lstrip('*').rsplit('/', 1)[-1] == filename:
            candidate = fields[0].lower()
            if len(candidate) == 64 and all(ch in '0123456789abcdef' for ch in candidate):
                expected = candidate
                break
    if not expected:
        raise RuntimeError(f'No SHA-256 entry for {filename} in upstream manifest {manifest_url}.')
    actual = sha256(archive_path)
    if actual != expected:
        raise RuntimeError(f'SHA-256 mismatch for {filename}: expected {expected}, received {actual}.')
    say(f'Upstream SHA-256 verified: {filename}')
    return actual


def download(url: str, destination: Path, force: bool = False, minimum_size: int = 1024) -> Path:
    """Download one file with aria2c, preserving the builder's Path return API.

    The third positional argument and the ``force=`` keyword are both supported.
    A failed transfer is resumable because aria2c keeps its control file and the
    next attempt uses --continue=true. Final checksum verification is performed
    separately by verify_manifest_checksum().
    """
    aria2c = shutil.which('aria2c')
    if not aria2c:
        raise RuntimeError('aria2c is required. Install it with: sudo apt install aria2')

    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.is_file() and destination.stat().st_size >= minimum_size and not force:
        say(f'Using cached download: {destination.name}')
        return destination

    if force:
        destination.unlink(missing_ok=True)
        Path(str(destination) + '.aria2').unlink(missing_ok=True)

    last_error = None
    for attempt in range(1, 4):
        command = [
            aria2c,
            '--continue=true',
            '--allow-overwrite=true',
            '--auto-file-renaming=false',
            '--max-tries=3',
            '--retry-wait=3',
            '--connect-timeout=30',
            '--timeout=180',
            '--split=8',
            '--max-connection-per-server=8',
            '--min-split-size=1M',
            '--file-allocation=none',
            '--summary-interval=1',
            '--console-log-level=notice',
            '--dir', str(destination.parent),
            '--out', destination.name,
            url,
        ]
        try:
            say(f'Downloading ({attempt}/3): {url}')
            subprocess.run(command, cwd=ROOT, check=True, timeout=1200)
            if not destination.is_file():
                raise RuntimeError('aria2c reported success but did not create the destination file.')
            if destination.stat().st_size < minimum_size:
                raise RuntimeError(f'Downloaded file is unexpectedly small (<{minimum_size} bytes).')
            return destination
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, RuntimeError) as exc:
            last_error = exc
            if attempt < 3:
                say(f'Download attempt {attempt} failed: {exc}. Retrying/resuming...')
                time.sleep(attempt * 2)

    raise RuntimeError(f'Download failed: {url}: {last_error}')


def extract_member(archive_path: Path, wanted: str, output: Path) -> None:
    """Extract one regular file by basename; never extract arbitrary archive paths."""
    try:
        with tarfile.open(archive_path, 'r:gz') as archive:
            matches = [m for m in archive.getmembers()
                       if m.isfile() and PurePosixPath(m.name).name == wanted]
            if not matches:
                raise RuntimeError(f'{archive_path.name} does not contain {wanted}.')
            member = min(matches, key=lambda m: len(m.name))
            source = archive.extractfile(member)
            if source is None:
                raise RuntimeError(f'Could not read {wanted} from {archive_path.name}.')
            output.parent.mkdir(parents=True, exist_ok=True)
            temporary = output.with_suffix(output.suffix + '.part')
            with source, temporary.open('wb') as destination:
                shutil.copyfileobj(source, destination)
            if temporary.stat().st_size < 1024:
                temporary.unlink(missing_ok=True)
                raise RuntimeError(f'Extracted {wanted} is unexpectedly small.')
            os.replace(temporary, output)
            output.chmod(0o755)
    except tarfile.TarError as exc:
        raise RuntimeError(f'Invalid archive {archive_path}: {exc}') from exc


def run_version(binary: Path, *args: str) -> str:
    result = subprocess.run([str(binary), *args], cwd=ROOT, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=20)
    if result.returncode != 0:
        raise RuntimeError(f'{binary.name} validation failed ({result.returncode}): {result.stdout[-2000:]}')
    return result.stdout.strip()


def copy_runtime_state(old: Path, new: Path) -> None:
    for name in ('logs', 'runtime'):
        source = old / name
        if source.is_dir():
            shutil.copytree(source, new / name, dirs_exist_ok=True)
        else:
            (new / name).mkdir(parents=True, exist_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description='Build the project-contained eYeYaYe media runtime.')
    parser.add_argument('--force-download', action='store_true', help='Ignore cached upstream archives and download them again.')
    args = parser.parse_args()
    if platform.system() != 'Linux':
        raise RuntimeError('This release supports Linux only.')
    arch = arch_name()
    for source in (CAM3 / 'rtmp_live_bridge.py', CAM3 / 'rtmp_cam.py', ROOT / 'control' / 'COMMANDS.json'):
        if not source.is_file():
            raise RuntimeError(f'Required CAM3 source file is missing: {source.relative_to(ROOT)}')
    cache = ROOT / 'cache' / 'runtime-downloads'
    media_archive = download(MEDIAMTX_URLS[arch], cache / f'mediamtx_{MEDIAMTX_VERSION}_linux_{arch}.tar.gz', args.force_download)
    ffmpeg_archive = download(FFMPEG_URLS[arch], cache / f'ffmpeg-linux-{arch}.tar.gz', args.force_download)
    media_manifest = f'https://github.com/bluenviron/mediamtx/releases/download/{MEDIAMTX_VERSION}/checksums.sha256'
    ffmpeg_manifest = FFMPEG_MANIFEST_URL
    media_checksum = verify_manifest_checksum(media_manifest, media_archive, cache / f'mediamtx_{MEDIAMTX_VERSION}_checksums.sha256', args.force_download)
    ffmpeg_checksum = verify_manifest_checksum(ffmpeg_manifest, ffmpeg_archive, cache / 'ffmpeg_SHA256SUMS.txt', args.force_download)

    stage = Path(tempfile.mkdtemp(prefix='CAM3_LIVE_STANDALONE.build.', dir=CAM3))
    old = CAM3 / 'CAM3_LIVE_STANDALONE.previous'
    try:
        (stage / 'bin').mkdir(parents=True)
        (stage / 'logs').mkdir()
        (stage / 'runtime').mkdir()
        (stage / 'control').mkdir()
        shutil.copy2(ROOT / 'control' / 'COMMANDS.json', stage / 'control' / 'COMMANDS.json')
        shutil.copy2(CAM3 / 'rtmp_live_bridge.py', stage / 'rtmp_live_bridge.py')
        shutil.copy2(CAM3 / 'rtmp_cam.py', stage / 'rtmp_cam.py')
        (stage / 'rtmp_live_bridge.py').chmod(0o644)
        (stage / 'rtmp_cam.py').chmod(0o600)
        extract_member(media_archive, 'mediamtx', stage / 'bin' / 'mediamtx')
        extract_member(ffmpeg_archive, 'ffmpeg', stage / 'bin' / 'ffmpeg')
        extract_member(ffmpeg_archive, 'ffprobe', stage / 'bin' / 'ffprobe')

        media_version = run_version(stage / 'bin' / 'mediamtx', '--version')
        ffmpeg_version = run_version(stage / 'bin' / 'ffmpeg', '-version').splitlines()[0]
        ffprobe_version = run_version(stage / 'bin' / 'ffprobe', '-version').splitlines()[0]
        if MEDIAMTX_VERSION not in media_version:
            raise RuntimeError(f'Expected MediaMTX {MEDIAMTX_VERSION}, received: {media_version}')
        ffmpeg_release = ffmpeg_version.split()[2] if len(ffmpeg_version.split()) > 2 else ''
        ffprobe_release = ffprobe_version.split()[2] if len(ffprobe_version.split()) > 2 else ''
        if not ffmpeg_release or ffmpeg_release != ffprobe_release:
            raise RuntimeError(f'FFmpeg/FFprobe version mismatch: {ffmpeg_version} | {ffprobe_version}')
        if FFMPEG_VERSION_EXPECTED and ffmpeg_release != FFMPEG_VERSION_EXPECTED:
            raise RuntimeError(f'Expected FFmpeg {FFMPEG_VERSION_EXPECTED}, received: {ffmpeg_version}')
        encoder_check = subprocess.run([str(stage / 'bin' / 'ffmpeg'), '-hide_banner', '-encoders'],
                                       text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=30)
        if encoder_check.returncode or 'libx264' not in encoder_check.stdout:
            raise RuntimeError('Bundled FFmpeg does not expose libx264; archive/time-lapse encoding cannot be validated.')
        for name in ('rtmp_live_bridge.py', 'rtmp_cam.py'):
            py = subprocess.run([sys.executable, '-m', 'py_compile', str(stage / name)],
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            if py.returncode:
                raise RuntimeError(f'Python syntax validation failed for {name}: {py.stdout}')
        shutil.rmtree(stage / '__pycache__', ignore_errors=True)
        copy_runtime_state(TARGET, stage)
        (stage / 'BUILD_INFO.txt').write_text(
            'eYeYaYe project-contained media runtime\n'
            f'Build time: {datetime.now().astimezone().isoformat(timespec="seconds")}\n'
            f'Linux architecture: {arch}\n'
            f'MediaMTX archive SHA256 (upstream verified): {media_checksum}\n'
            f'FFmpeg archive SHA256 (upstream verified): {ffmpeg_checksum}\n'
            f'{media_version}\n{ffmpeg_version}\n{ffprobe_version}\n\n'
            'MediaMTX and FFmpeg/FFprobe are stored in bin/ and are launched by the main eYeYaYe engine.\n'
            f'FFmpeg asset: binmgr latest download; validated version: {ffmpeg_release}\n'
            'There is one shared MediaMTX process for CAM1, CAM2 and CAM3.\n'
            'The CAM3 bridge publishes into that process at 127.0.0.1:8554.\n'
            'No system-wide MediaMTX or FFmpeg path is required.\n', encoding='utf-8')
        (stage / 'README.txt').write_text(
            'eYeYaYe PROJECT-CONTAINED MEDIA RUNTIME\n\n'
            'This directory is owned by the main eYeYaYe engine. Do not launch a second MediaMTX here.\n'
            'MediaMTX routes CAM1/CAM2 RTSP sources and receives the live CAM3 bridge publisher.\n'
            'FFmpeg and FFprobe are used by recording, camera probes, archives and exports.\n'
            'All logs/runtime state remain inside this project directory.\n\n'
            'Rebuild manually from the project root with: python3 BUILD_RUNTIME.py\n', encoding='utf-8')
        (stage / 'THIRD_PARTY_NOTICES.txt').write_text(
            'MediaMTX: https://github.com/bluenviron/mediamtx — MIT license.\n'
            'FFmpeg static builds: https://github.com/binmgr/ffmpeg — builds include GPL components;\n'
            'consult the upstream license and build repository before redistributing binaries.\n', encoding='utf-8')

        if old.exists():
            shutil.rmtree(old)
        if TARGET.exists():
            os.replace(TARGET, old)
        try:
            os.replace(stage, TARGET)
        except Exception:
            if old.exists() and not TARGET.exists():
                os.replace(old, TARGET)
            raise
        if old.exists():
            shutil.rmtree(old)
        say('PROJECT RUNTIME BUILD COMPLETE')
        say(f'Architecture: {arch}')
        say(f'MediaMTX: {media_version}')
        say(f'FFmpeg: {ffmpeg_version}')
        say(f'FFprobe: {ffprobe_version}')
        say(f'Installed folder: {TARGET}')
        say('The main engine will start the single shared MediaMTX. This builder does not launch a stream or server.')
        return 0
    finally:
        if stage.exists():
            shutil.rmtree(stage, ignore_errors=True)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say('Build interrupted; the existing runtime bundle was left untouched.')
        raise SystemExit(130)
    except Exception as exc:
        say(f'BUILD ERROR: {type(exc).__name__}: {exc}')
        raise SystemExit(1)
