"""ffprobe and ffmpeg in a sandbox (spec §5.2, A13.3): local files only, 60 s, 1 GiB of address space. A transcode
(W2.1b) gets a time limit that grows with the video's length instead."""
import json
import os
import resource
import subprocess

PROBE_TIMEOUT_S = 60
ADDRESS_SPACE_BYTES = 1024 * 1024 * 1024   # 512 MiB until 2026-10-30: too tight for ffmpeg 7 on a phone-size video
# Protocols are not enough: ffmpeg probes content to pick a demuxer, so a playlist demuxer (hls, concat,
# ...) could follow `file:` references to other local files. Only the demuxers of accepted media may run.
_INPUT_LIMITS = ['-protocol_whitelist', 'file',
                 '-format_whitelist', 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,mp3,ogg']
# One thread for the decoder (before -i) and one for the JPEG encoder (after it): ffmpeg's default is one per core
# for each, and on a many-core host their buffers can outgrow ADDRESS_SPACE_BYTES (under 512 MiB, on 32 cores, a
# portrait 1080p frame failed with get_buffer() in the decoder, then 'Error while opening encoder' about 4 times in
# 10). One frame needs no more.
_DECODE_LIMITS = ['-threads', '1']
_ENCODE_LIMITS = ['-threads', '1']
# A rendition (W2.1b): two encoder threads at 480p or less stay well inside ADDRESS_SPACE_BYTES; the time it may take
# is TRANSCODE_BASE_S plus TRANSCODE_PER_SECOND per second of video, at most TRANSCODE_MAX_S
TRANSCODE_BASE_S, TRANSCODE_PER_SECOND, TRANSCODE_MAX_S = 120, 4, 4 * 60 * 60
_TRANSCODE_ENCODE = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '26', '-profile:v', 'main', '-pix_fmt', 'yuv420p',
                     '-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-movflags', '+faststart', '-threads', '2', '-f', 'mp4']


class MediaToolError(Exception):
    pass


def _limit_address_space():
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))


def _run(cmd: list, private_paths: tuple = (), timeout_s: int = PROBE_TIMEOUT_S) -> bytes:
    try:
        # PATH and no secret: the tools parse untrusted bytes, and the app's environment holds its secrets. One malloc
        # arena: glibc's default of one per thread reserves 64 MiB of ADDRESS_SPACE_BYTES for each of ffmpeg's threads
        done = subprocess.run(cmd, capture_output=True, timeout=timeout_s, preexec_fn=_limit_address_space,
                              stdin=subprocess.DEVNULL, check=False,
                              env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'MALLOC_ARENA_MAX': '1'})
    except subprocess.TimeoutExpired:
        raise MediaToolError(f'{cmd[0]} timed out after {timeout_s} s')
    except FileNotFoundError:
        raise MediaToolError(f'{cmd[0]} is not installed')
    if done.returncode != 0:
        reason = done.stderr.decode(errors='replace')
        for private in private_paths:
            reason = reason.replace(private, '<file>')
        raise MediaToolError(f'{cmd[0]} failed: {reason[:200]}')
    return done.stdout


def run_ffprobe(path: str) -> dict:
    out = _run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams',
                *_INPUT_LIMITS, 'file:' + path], (path,))
    try:
        return json.loads(out)
    except ValueError:
        raise MediaToolError('ffprobe output was unreadable')


def _require_image(out_path: str) -> None:
    # ffmpeg exits 0 and writes nothing when -ss is past the end.
    if not (os.path.getsize(out_path) if os.path.exists(out_path) else 0):
        raise MediaToolError('ffmpeg wrote no image')


def extract_frame(path: str, at_s: float, out_path: str) -> None:
    _run(['ffmpeg', '-nostdin', '-v', 'error', *_INPUT_LIMITS, *_DECODE_LIMITS, '-ss', f'{at_s:.3f}', '-i', 'file:' + path,
          *_ENCODE_LIMITS, '-frames:v', '1', '-y', 'file:' + out_path], (path, out_path))
    _require_image(out_path)


def extract_cover(path: str, out_path: str) -> None:
    _run(['ffmpeg', '-nostdin', '-v', 'error', *_INPUT_LIMITS, *_DECODE_LIMITS, '-i', 'file:' + path,
          *_ENCODE_LIMITS, '-an', '-map', '0:v:0',
          '-frames:v', '1', '-y', 'file:' + out_path], (path, out_path))
    _require_image(out_path)


def transcode_timeout(duration_s: int) -> int:
    return min(TRANSCODE_MAX_S, TRANSCODE_BASE_S + duration_s * TRANSCODE_PER_SECOND)


def transcode(path: str, out_path: str, side: int, portrait: bool, timeout_s: int) -> None:
    """An H.264/AAC MP4 of `path` whose short side is `side` (its width when `portrait`): the first video stream and,
    if there is one, the first audio stream."""
    scale = f'scale={side}:-2' if portrait else f'scale=-2:{side}'
    _run(['ffmpeg', '-nostdin', '-v', 'error', *_INPUT_LIMITS, *_DECODE_LIMITS, '-i', 'file:' + path,
          '-map', '0:v:0', '-map', '0:a:0?', '-vf', scale, *_TRANSCODE_ENCODE, '-y', 'file:' + out_path],
         (path, out_path), timeout_s)
