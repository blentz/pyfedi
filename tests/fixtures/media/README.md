# Media fixtures

Tiny 1 s (64x64 for video) files for the sniff and probe tests. Every file is under 30 KB; no bitrate had to be lowered.
They were made with the ffmpeg 7.1 in the test image (`docker build --target test`), from the repo root, with `ffmpeg -v error -y` in front of each command:

```bash
ffmpeg -f lavfi -i testsrc=size=64x64:rate=10 -f lavfi -i sine=frequency=440 -t 1 -c:v libx264 -pix_fmt yuv420p -c:a aac -shortest tests/fixtures/media/video_h264.mp4
ffmpeg -f lavfi -i testsrc=size=64x64:rate=10 -t 1 -c:v libvpx-vp9 tests/fixtures/media/video_vp9.webm
ffmpeg -f lavfi -i testsrc=size=64x64:rate=10 -t 1 -c:v mpeg4 tests/fixtures/media/video_mpeg4.mp4
ffmpeg -f lavfi -i sine=frequency=440 -t 1 -c:a libmp3lame -b:a 32k tests/fixtures/media/audio.mp3
ffmpeg -i tests/fixtures/media/audio.mp3 -f lavfi -i color=red:size=64x64:duration=1 -map 0:a -map 1:v -c:a copy -c:v mjpeg -disposition:v attached_pic -id3v2_version 3 tests/fixtures/media/audio_cover.mp3
ffmpeg -f lavfi -i sine=frequency=440 -t 1 -c:a aac tests/fixtures/media/audio.m4a
ffmpeg -f lavfi -i sine=frequency=440 -t 1 -c:a libopus tests/fixtures/media/audio_opus.ogg
printf 'this is not a video\n' > tests/fixtures/media/not_media.mp4
```

`audio_cover.mp3` differs from the plan's command. The plan's `-frames:v 1 -map 0 -map 1` cut the audio to 0.05 s,
so this one maps `0:a` and `1:v` and gives the colour source a `duration=1`.

`audio.m4a` has the `M4A ` major brand (`ftypM4A `), so no `-brand` flag is needed.

| File | Codec | Container | Purpose |
|---|---|---|---|
| `video_h264.mp4` | h264 + aac | mp4 | accepted video |
| `video_vp9.webm` | vp9 | webm | accepted video |
| `video_mpeg4.mp4` | mpeg4 | mp4 | rejected codec |
| `audio.mp3` | mp3 | mp3 | accepted audio, no picture |
| `audio_cover.mp3` | mp3 + mjpeg picture | mp3 | accepted audio with cover |
| `audio.m4a` | aac | mp4 (`M4A `) | accepted audio |
| `audio_opus.ogg` | opus | ogg | accepted audio |
| `not_media.mp4` | none | none | text bytes with a media name |
