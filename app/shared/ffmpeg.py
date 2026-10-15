"""ffprobe and ffmpeg in a sandbox (spec §5.2, A13.3): local files only, 60 s, 512 MiB of address space."""
import json
import resource
import subprocess

PROBE_TIMEOUT_S = 60
ADDRESS_SPACE_BYTES = 512 * 1024 * 1024
_FILE_ONLY = ['-protocol_whitelist', 'file']


class MediaToolError(Exception):
    pass


def _limit_address_space():
    resource.setrlimit(resource.RLIMIT_AS, (ADDRESS_SPACE_BYTES, ADDRESS_SPACE_BYTES))


def _run(cmd: list) -> bytes:
    try:
        done = subprocess.run(cmd, capture_output=True, timeout=PROBE_TIMEOUT_S,
                              preexec_fn=_limit_address_space, check=False)
    except subprocess.TimeoutExpired:
        raise MediaToolError(f'{cmd[0]} timed out after {PROBE_TIMEOUT_S} s')
    except FileNotFoundError:
        raise MediaToolError(f'{cmd[0]} is not installed')
    if done.returncode != 0:
        raise MediaToolError(f'{cmd[0]} failed: {done.stderr.decode(errors="replace")[:200]}')
    return done.stdout


def run_ffprobe(path: str) -> dict:
    out = _run(['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format', '-show_streams',
                *_FILE_ONLY, 'file:' + path])
    try:
        return json.loads(out)
    except ValueError:
        raise MediaToolError('ffprobe output was unreadable')


def extract_frame(path: str, at_s: float, out_path: str) -> None:
    _run(['ffmpeg', '-v', 'error', *_FILE_ONLY, '-ss', f'{at_s:.3f}', '-i', 'file:' + path,
          '-frames:v', '1', '-y', 'file:' + out_path])


def extract_cover(path: str, out_path: str) -> None:
    _run(['ffmpeg', '-v', 'error', *_FILE_ONLY, '-i', 'file:' + path, '-an', '-map', '0:v:0',
          '-frames:v', '1', '-y', 'file:' + out_path])
