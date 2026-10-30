import os
import subprocess

import pytest

from app.shared import ffmpeg
from app.shared.ffmpeg import MediaToolError, extract_cover, extract_frame, run_ffprobe

FORMATS = 'mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,mp3,ogg'
FIX = os.path.join(os.path.dirname(__file__), 'fixtures', 'media')


def test_probe_reads_a_real_mp4():
    probe = run_ffprobe(os.path.join(FIX, 'video_h264.mp4'))
    codecs = {s['codec_name'] for s in probe['streams']}
    assert 'h264' in codecs and float(probe['format']['duration']) > 0


def test_probe_of_non_media_raises():
    with pytest.raises(MediaToolError):
        run_ffprobe(os.path.join(FIX, 'not_media.mp4'))


def test_probe_command_is_sandboxed(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(cmd=cmd, **kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{"streams": [], "format": {}}', stderr=b'')
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    run_ffprobe('/x/y.mp4')
    assert seen['cmd'] == ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_format',
                           '-show_streams', '-protocol_whitelist', 'file',
                           '-format_whitelist', FORMATS, 'file:/x/y.mp4']
    assert seen['timeout'] == 60 and seen['preexec_fn'] is ffmpeg._limit_address_space
    assert seen['stdin'] is subprocess.DEVNULL
    assert seen['env'] == {'PATH': os.environ.get('PATH', '/usr/bin:/bin'),   # none of the app's secrets
                           'MALLOC_ARENA_MAX': '1'}


def test_the_tools_run_without_the_apps_environment(monkeypatch):
    """ffmpeg parses untrusted bytes: it gets PATH and nothing else of the app's environment (database URL, S3 keys,
    the secret key), plus one malloc arena: each thread's own arena reserves 64 MiB of the 512 MiB address space, and
    with them a 1080x1920 frame failed under 384 MiB about 4 times in 20 (with one arena, never)."""
    seen = {}

    def fake_run(cmd, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(cmd, 0, stdout=b'{}', stderr=b'')
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    monkeypatch.setenv('SECRET_KEY', 'not for ffmpeg')
    monkeypatch.delenv('PATH')
    run_ffprobe('/x/y.mp4')
    assert seen['env'] == {'PATH': '/usr/bin:/bin', 'MALLOC_ARENA_MAX': '1'}


def test_ffmpeg_commands_are_sandboxed(monkeypatch, tmp_path):
    seen = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs))
        open(cmd[-1][len('file:'):], 'wb').write(b'\xff\xd8')
        return subprocess.CompletedProcess(cmd, 0, stdout=b'', stderr=b'')
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    extract_frame('/x/y.mp4', 0.5, str(tmp_path / 'f.jpg'))
    extract_cover('/x/y.mp3', str(tmp_path / 'c.jpg'))
    frame, cover = seen
    assert frame[0] == ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file',
                        '-format_whitelist', FORMATS, '-threads', '1', '-ss', '0.500', '-i', 'file:/x/y.mp4',
                        '-threads', '1', '-frames:v', '1', '-y', 'file:' + str(tmp_path / 'f.jpg')]
    assert cover[0] == ['ffmpeg', '-nostdin', '-v', 'error', '-protocol_whitelist', 'file',
                        '-format_whitelist', FORMATS, '-threads', '1', '-i', 'file:/x/y.mp3', '-threads', '1',
                        '-an', '-map', '0:v:0', '-frames:v', '1', '-y', 'file:' + str(tmp_path / 'c.jpg')]
    assert all(kw['stdin'] is subprocess.DEVNULL and kw['timeout'] == 60 for _, kw in seen)
    assert all(kw['env'] == {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'MALLOC_ARENA_MAX': '1'}
               for _, kw in seen)


def _playlist(tmp_path, name):
    # A playlist is media for the hls demuxer; the whitelist must refuse it before any segment is read.
    path = tmp_path / name
    path.write_bytes(b'#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\nfile:///etc/hostname\n#EXT-X-ENDLIST\n')
    return str(path)


def test_probe_refuses_a_playlist_demuxer(tmp_path):
    with pytest.raises(MediaToolError, match='not on whitelist'):
        run_ffprobe(_playlist(tmp_path, 'evil.m3u8'))


def test_frame_refuses_a_playlist_demuxer(tmp_path):
    with pytest.raises(MediaToolError, match='not on whitelist'):
        extract_frame(_playlist(tmp_path, 'evil.m3u8'), 0, str(tmp_path / 'f.jpg'))


def test_id3_prefixed_playlist_is_refused(tmp_path):
    evil = tmp_path / 'evil.mp3'
    evil.write_bytes(b'ID3\x03\x00\x00\x00\x00\x00\x00#EXTM3U\n#EXTINF:1,\nfile:///etc/hostname\n#EXT-X-ENDLIST\n')
    with pytest.raises(MediaToolError):
        run_ffprobe(str(evil))
    with pytest.raises(MediaToolError):
        extract_frame(str(evil), 0, str(tmp_path / 'f.jpg'))


def test_error_text_hides_local_paths(tmp_path):
    bad = tmp_path / 'secret-name.mp4'
    bad.write_bytes(b'not media at all')
    with pytest.raises(MediaToolError) as caught:
        run_ffprobe(str(bad))
    assert str(tmp_path) not in str(caught.value) and '<file>' in str(caught.value)
    with pytest.raises(MediaToolError) as caught:
        extract_frame(str(bad), 0, str(tmp_path / 'out.jpg'))
    assert str(tmp_path) not in str(caught.value)


def test_frame_past_the_end_raises(tmp_path):
    # ffmpeg 7.1 exits non-zero here; the 'wrote no image' guard covers builds that exit 0 (next test).
    with pytest.raises(MediaToolError):
        extract_frame(os.path.join(FIX, 'video_h264.mp4'), 60, str(tmp_path / 'f.jpg'))


def test_empty_output_raises(monkeypatch, tmp_path):
    out = tmp_path / 'f.jpg'
    monkeypatch.setattr(ffmpeg.subprocess, 'run', lambda cmd, **k: (
        out.write_bytes(b''), subprocess.CompletedProcess(cmd, 0, stdout=b'', stderr=b''))[1])
    with pytest.raises(MediaToolError, match='wrote no image'):
        extract_frame('/x/y.mp4', 0, str(out))


def test_probe_timeout_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, 60)
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    with pytest.raises(MediaToolError, match='timed out'):
        run_ffprobe('/x/y.mp4')


def test_probe_bad_json_raises(monkeypatch):
    monkeypatch.setattr(ffmpeg.subprocess, 'run',
                        lambda cmd, **k: subprocess.CompletedProcess(cmd, 0, stdout=b'not json', stderr=b''))
    with pytest.raises(MediaToolError, match='unreadable'):
        run_ffprobe('/x/y.mp4')


def test_tool_missing_raises(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])
    monkeypatch.setattr(ffmpeg.subprocess, 'run', fake_run)
    with pytest.raises(MediaToolError, match='not installed'):
        run_ffprobe('/x/y.mp4')


def test_rlimit_is_set(monkeypatch):
    calls = []
    monkeypatch.setattr(ffmpeg.resource, 'setrlimit', lambda which, lim: calls.append((which, lim)))
    ffmpeg._limit_address_space()
    assert calls == [(ffmpeg.resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))]


def test_frame_and_cover_are_written(tmp_path):
    frame, cover = tmp_path / 'f.jpg', tmp_path / 'c.jpg'
    extract_frame(os.path.join(FIX, 'video_h264.mp4'), 0.1, str(frame))
    extract_cover(os.path.join(FIX, 'audio_cover.mp3'), str(cover))
    assert frame.read_bytes()[:2] == b'\xff\xd8' and cover.read_bytes()[:2] == b'\xff\xd8'


def test_frame_of_a_full_size_video_fits_the_address_space_limit(tmp_path):
    """A phone-size video decodes and encodes inside the 512 MiB limit. ffmpeg starts one thread per core for the
    decoder and again for the JPEG encoder; on a 32-core host the decoder's ran out of address space
    (get_buffer() failed) and, with only those limited, the encoder's failed to open about 4 extractions in 10. Ten
    extractions here, so an unlimited side cannot pass by luck."""
    clip = tmp_path / 'portrait.mp4'
    subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1080x1920:rate=30:duration=1',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-y', str(clip)], check=True, stdin=subprocess.DEVNULL)
    for attempt in range(10):
        frame = tmp_path / f'f{attempt}.jpg'
        extract_frame(str(clip), 0.5, str(frame))
        assert frame.read_bytes()[:2] == b'\xff\xd8'


def test_cover_of_audio_without_picture_raises(tmp_path):
    with pytest.raises(MediaToolError):
        extract_cover(os.path.join(FIX, 'audio.mp3'), str(tmp_path / 'c.jpg'))
