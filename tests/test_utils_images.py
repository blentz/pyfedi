import pytest
from PIL import Image, ImageCms

from app.utils import get_new_frames, save_new_gif, scale_gif, to_srgb


def make_gif(path, frames=3, size=(64, 64)):
    images = [Image.new('RGBA', size, (i * 60 % 255, 0, 0, 255)) for i in range(frames)]
    images[0].save(path, save_all=True, append_images=images[1:], duration=40, loop=0)
    return path


class TestToSrgb:
    def test_cmyk_is_converted_to_rgb(self):
        """The CMYK arm: profileToProfile cannot take CMYK directly."""
        result = to_srgb(Image.new('CMYK', (8, 8)))
        assert result.mode == 'RGB'

    def test_rgb_without_a_profile_is_returned_as_rgb(self):
        result = to_srgb(Image.new('RGB', (8, 8), (10, 20, 30)))
        assert result.mode == 'RGB'

    def test_an_embedded_profile_is_used_as_the_source(self):
        """The icc_bytes arm, rather than the assume= fallback."""
        image = Image.new('RGB', (8, 8), (10, 20, 30))
        image.info['icc_profile'] = ImageCms.ImageCmsProfile(
            ImageCms.createProfile('sRGB')).tobytes()

        result = to_srgb(image)

        assert result.mode == 'RGB'
        assert 'icc_profile' in result.info

    def test_output_carries_an_srgb_tag(self):
        result = to_srgb(Image.new('RGB', (8, 8), (10, 20, 30)))
        assert 'icc_profile' in result.info

    def test_an_incompatible_embedded_profile_falls_back_to_a_plain_convert(self):
        """The `except ImageCms.PyCMSError` arm, reached with a real, valid,
        but incompatible ICC profile rather than a corrupt one.

        A LAB profile paired with an RGB image makes profileToProfile's
        transform-build fail with PyCMSError ("cannot build transform") --
        proven interactively before writing this test. Profile construction
        (the line outside the try) succeeds because the bytes are a
        genuinely valid ICC profile; only the transform itself fails, so
        this exercises the guarded call, not the unguarded one.

        Discriminated from the plain-convert fallback by checking the
        resulting icc_profile bytes are untouched (still the LAB profile),
        rather than re-tagged with the sRGB profile the success path writes
        -- if the fallback arm were deleted (or the LAB profile happened not
        to trigger it), this would fail because the emitted profile would
        differ.
        """
        image = Image.new('RGB', (8, 8), (10, 20, 30))
        lab_bytes = ImageCms.ImageCmsProfile(ImageCms.createProfile('LAB')).tobytes()
        image.info['icc_profile'] = lab_bytes

        result = to_srgb(image)

        assert result.mode == 'RGB'
        # The fallback (`im.convert("RGB")`) does not rewrite info, so the
        # original (incompatible) profile bytes survive untouched -- proof
        # the except arm ran rather than the success path.
        assert result.info.get('icc_profile') == lab_bytes

    CORRUPT_PROFILES = [
        # b'' is deliberately NOT here: it is falsy, so it never reaches the
        # profile construction at all -- `if icc_bytes:` already routes it to
        # the `assume` branch, and it passed before this fix.
        ('not_a_profile', b'this is not an ICC profile at all'),
        ('truncated_header', b'\x00\x00\x02\x0c' + b'A' * 100),
        ('valid_prefix_then_garbage',
         ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()[:64] + b'\xff' * 64),
    ]

    @pytest.mark.parametrize('icc_bytes', [b for _, b in CORRUPT_PROFILES],
                             ids=[i for i, _ in CORRUPT_PROFILES])
    def test_a_corrupt_embedded_profile_falls_back_instead_of_escaping(self, icc_bytes):
        """A corrupt ICC profile must reach the `assume` fallback, not the caller.

        The ICC bytes come out of an uploaded or federated image, so they are
        attacker-controlled. littlecms refuses bytes it cannot parse by raising
        OSError('cannot open profile from string') -- NOT PyCMSError, and not
        AttributeError -- so while the profile was built outside the guarded
        region, neither `except` arm of to_srgb could see it and the OSError
        escaped the function entirely. Both callers of to_srgb turn that into a
        raw error message shown to the user: the web upload path flashes
        `reason=str(ex)` and the API returns `{"message": str(e)}` at 400.

        The function already has the fallback this needs -- `assume` -- so the
        fix is only that the construction now sits inside the guard.
        """
        image = Image.new('RGB', (8, 8), (10, 20, 30))
        image.info['icc_profile'] = icc_bytes

        result = to_srgb(image)

        assert result.mode == 'RGB'

    def test_a_corrupt_profile_on_a_cmyk_image_still_converts(self):
        """The conversion still happens; the fallback is not a bail-out.

        CMYK is the mode to_srgb exists for, and the one whose pixels visibly
        change: a mode of 'RGB' here means the image was really converted rather
        than handed back as it arrived.
        """
        image = Image.new('CMYK', (8, 8), (10, 20, 30, 40))
        image.info['icc_profile'] = b'this is not an ICC profile at all'

        result = to_srgb(image)

        assert result.mode == 'RGB'
        assert result.size == (8, 8)

    def test_a_corrupt_profile_falls_back_to_the_assume_profile_not_a_plain_convert(self):
        """Discriminates the `assume` fallback from a bare `im.convert("RGB")`.

        Both leave mode 'RGB', so mode alone cannot tell them apart. The
        profileToProfile success path re-tags the result with sRGB; a plain
        convert leaves whatever `info` held. Asserting the emitted profile is
        the sRGB tag proves the transform ran against the substituted `assume`
        profile rather than the corrupt bytes being routed to a bail-out.
        """
        srgb_tag = ImageCms.ImageCmsProfile(ImageCms.createProfile('sRGB')).tobytes()
        image = Image.new('RGB', (8, 8), (10, 20, 30))
        image.info['icc_profile'] = b'this is not an ICC profile at all'

        result = to_srgb(image)

        assert result.info.get('icc_profile') == srgb_tag


class TestGetNewFrames:
    def test_every_frame_is_returned(self, tmp_path):
        """Fails if the loop stops short of gif.n_frames."""
        with Image.open(make_gif(tmp_path / 'frames.gif', frames=4)) as gif:
            assert len(get_new_frames(gif, (16, 16))) == 4

    def test_frames_are_thumbnailed_to_fit_the_scale(self, tmp_path):
        with Image.open(make_gif(tmp_path / 'frames2.gif', size=(64, 64))) as gif:
            frames = get_new_frames(gif, (16, 16))
        assert all(f.width <= 16 and f.height <= 16 for f in frames)

    def test_frames_are_rgba(self, tmp_path):
        with Image.open(make_gif(tmp_path / 'frames3.gif')) as gif:
            frames = get_new_frames(gif, (16, 16))
        assert all(f.mode == 'RGBA' for f in frames)


class TestScaleGif:
    def test_scaling_in_place_shrinks_the_file(self, tmp_path):
        path = tmp_path / 'anim.gif'
        make_gif(path, frames=3, size=(128, 128))

        scale_gif(str(path), (16, 16))

        with Image.open(path) as result:
            assert result.width <= 16
            assert result.n_frames == 3

    def test_scaling_to_a_new_path_leaves_the_original(self, tmp_path):
        source = tmp_path / 'source.gif'
        target = tmp_path / 'target.gif'
        make_gif(source, size=(128, 128))

        scale_gif(str(source), (16, 16), new_path=str(target))

        with Image.open(source) as original:
            assert original.width == 128
        with Image.open(target) as scaled:
            assert scaled.width <= 16


class TestSaveNewGif:
    def test_writes_an_animation_with_every_frame(self, tmp_path):
        target = tmp_path / 'out.gif'
        frames = [Image.new('RGBA', (8, 8), (i * 80 % 255, 0, 0, 255)) for i in range(3)]
        info = {'loop': True, 'duration': 40, 'background': 223,
                'extension': b'NETSCAPE2.0', 'transparency': 223}

        save_new_gif(frames, info, str(target))

        with Image.open(target) as result:
            assert result.n_frames == 3
