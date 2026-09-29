"""D1405: a peer's `icon`/`image` url, rendered as an href.

`image_url_from` reads a peer's `icon` and `image` in four shapes and returns the string
it finds. That string becomes `File.source_url`, and nothing between there and the page
checks its scheme:

* `File.view_url()` returns `source_url` unchanged when there is no local copy, and
  twelve templates put `view_url()` in a bare `href` -- `post/_post_full.html:80, 86`,
  three sites in `post/post_teaser/_macros.html`, three in the dillo theme's copy,
  `_post_teaser_masonry.html:32`, `post_edit.html:146, 150` and `admin/media.html:34`.
* `User.avatar_image()` and `User.cover_image()` return it through `served_path`, which
  rewrites only this instance's own `app/` paths and passes anything else back
  untouched. Eight templates put those in an `href`, including
  `user/show_profile.html:40` -- the profile page of the actor who sent the icon.

Measured end to end, through `actor_json_to_model`:

    PROBE actor icon      source_url='javascript:alert(document.domain)'
                          view_url='javascript:alert(document.domain)'
    PROBE actor image     source_url='javascript:alert(2)'
    PROBE avatar_image()  'javascript:alert(document.domain)'
    PROBE cover_image()   'javascript:alert(2)'

So a remote actor whose `icon` named a `javascript:` url had a clickable javascript: link
on its own profile page, for every visitor. The same reader serves `actor_json_to_model`,
the three profile-refresh tasks and `Post.new`'s image and icon branches (18 call sites
between them), which is why the rule goes in the reader and not in any of them.

AN ALLOWLIST HERE, WHERE `Post.url` GOT A BLOCKLIST ONE ROUND EARLIER (D1404), and the
difference is what the value is FOR. `Post.url` is a link a person clicks, chosen by
remote software, so `magnet:`, `matrix:` and the long tail have to keep working and the
blocklist is the honest choice (`app/utils.py:404` records that argument). An image url is
one THIS INSTANCE FETCHES, with httpx, in `make_image_sizes`. A scheme httpx cannot fetch
is not a picture this instance could ever display, so there is nothing legitimate to
protect and nothing to audit -- and 1024, `File.source_url`'s own width, comes with it.

TWO OTHER PRODUCERS OF THE SAME COLUMN ARE FIXED WITH IT:

* `og:image`, read from a page this instance fetched, at four sites whose guard was
  `not filename.startswith('/')` -- a relative-path check that admitted every scheme.
  `_as_url` refuses the relative path too, for the same reason it refuses the rest: no
  http scheme.
* The API's `avatar` and `cover`, whose schema field is `fields.String` with
  `metadata={"format": "url"}`. Marshmallow does not validate `metadata`, and the code's
  own comment read "valid url passed". Any authenticated user could set their avatar to
  `javascript:...`. That one is REFUSED rather than dropped, because a caller is waiting
  for an answer and can fix the value, where a peer's document is ingested as far as it
  can be.
"""
import pytest
from flask import g

from app import db
from app.models import File, Site, User, image_url_from
from tests.factories import make_community, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.test'
HOST = 'test.piefed.local'
HOSTILE = 'javascript:alert(document.domain)'

NOT_FETCHABLE = [
    HOSTILE,
    'JavaScript:alert(1)',
    ' javascript:alert(1)',
    'data:image/svg+xml,<svg onload="alert(1)"/>',
    'vbscript:msgbox(1)',
    'file:///etc/passwd',
    # Every one of these is a scheme httpx will not fetch, so none of them could ever
    # have produced a picture -- there is no legitimate use to weigh against the risk.
    'magnet:?xt=urn:btih:0123456789abcdef',
    'matrix:r/room:example.test',
    'mailto:someone@example.test',
    '//evil.example/x.png',
    '/relative/x.png',
    'example.test/x.png',
    '',
]

FETCHABLE = [
    'https://peer.test/media/a.png',
    'http://peer.test/media/a.png',
    'HTTPS://PEER.TEST/MEDIA/A.PNG',
]


def _icon(url):
    """The four shapes `image_url_from` reads, each carrying `url`."""
    return {'a bare string': url,
            'a dict': {'type': 'Image', 'url': url},
            'a list of dicts': [{'type': 'Image', 'url': url}],
            'a list of strings': [url]}


@pytest.fixture
def env(app, db_session):
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    local = make_instance(HOST, software='piefed')
    make_user(local, 'founder', local=True)
    peer = make_instance(PEER)
    db.session.commit()
    return SimpleNamespace(peer=peer)


@pytest.fixture(autouse=True)
def no_image_fetch(monkeypatch):
    """`actor_json_to_model` queues `make_image_sizes` for an icon it accepted, which
    fetches the url. Nothing here is about that fetch, and a control row would otherwise
    need a route registered for it."""
    monkeypatch.setattr('app.activitypub.util.make_image_sizes', lambda *a, **k: None)


# --------------------------------------------------------------------------
# The reader
# --------------------------------------------------------------------------


class TestTheReader:
    @pytest.mark.parametrize('shape', list(_icon('x')))
    @pytest.mark.parametrize('url', NOT_FETCHABLE)
    def test_a_url_httpx_cannot_fetch_is_dropped_in_every_shape(self, shape, url):
        """One rule for all four shapes. A guard on the dict case alone would pass a
        peer that sent a list, which is what most software sends for an icon."""
        assert image_url_from(_icon(url)[shape]) is None

    @pytest.mark.parametrize('shape', list(_icon('x')))
    @pytest.mark.parametrize('url', FETCHABLE)
    def test_a_fetchable_url_is_kept_in_every_shape(self, shape, url):
        assert image_url_from(_icon(url)[shape]) == url

    def test_which_end_of_a_list_is_read_is_unchanged(self):
        """`prefer_last` is why an icon and an image read opposite ends of a list, and a
        scheme check must not quietly become a search for the first acceptable entry:
        an entry that is unusable gives None rather than a look at the other end."""
        pair = [{'url': 'https://peer.test/first.png'},
                {'url': 'https://peer.test/last.png'}]

        assert image_url_from(pair) == 'https://peer.test/first.png'
        assert image_url_from(pair, prefer_last=True) == 'https://peer.test/last.png'
        assert image_url_from([{'url': HOSTILE}, {'url': 'https://peer.test/ok.png'}]) is None

    def test_the_column_width_is_applied(self):
        """`File.source_url` is String(1024), and a peer choosing a longer url would be
        a DataError at commit -- which loses the whole actor, not just its icon."""
        long_url = 'https://peer.test/' + 'a' * 5000

        assert len(image_url_from({'url': long_url})) == 1024

    @pytest.mark.parametrize('value', [None, 42, [], {}, [None], [{}], [{'url': 5}]])
    def test_a_shape_with_no_url_in_it_is_still_None(self, value):
        """The shapes this function already existed to survive (D1341): `icon: []` was an
        IndexError and `icon: [5]` a TypeError, and the post or actor was lost."""
        assert image_url_from(value) is None


# --------------------------------------------------------------------------
# End to end, through the actor the probe used
# --------------------------------------------------------------------------


def _actor(name, icon=None, image=None):
    actor = {'id': f'https://{PEER}/u/{name}', 'type': 'Person',
             'preferredUsername': name,
             'inbox': f'https://{PEER}/u/{name}/inbox',
             'outbox': f'https://{PEER}/u/{name}/outbox',
             'publicKey': {'id': f'https://{PEER}/u/{name}#main-key',
                           'owner': f'https://{PEER}/u/{name}',
                           'publicKeyPem': '-----BEGIN PUBLIC KEY-----\nx\n-----END PUBLIC KEY-----\n'}}
    if icon is not None:
        actor['icon'] = icon
    if image is not None:
        actor['image'] = image
    return actor


class TestAnActorsIconAndCover:
    def test_a_hostile_icon_and_image_are_not_stored(self, env):
        from app.activitypub.util import actor_json_to_model

        user = actor_json_to_model(_actor('hostile', icon={'url': HOSTILE},
                                         image={'url': 'javascript:alert(2)'}),
                                  'hostile', PEER)
        db.session.commit()

        assert user is not None
        assert user.avatar_id is None
        assert user.cover_id is None

    def test_the_rendered_urls_are_empty_rather_than_dangerous(self, env):
        """What the templates get. `avatar_image()` returns '' for an actor with no
        avatar, which is the state every actor that sent no icon is already in."""
        from app.activitypub.util import actor_json_to_model

        user = actor_json_to_model(_actor('hostile2', icon={'url': HOSTILE},
                                          image={'url': HOSTILE}),
                                   'hostile2', PEER)
        db.session.commit()

        assert user.avatar_image() == ''
        assert user.cover_image() == ''

    def test_a_real_icon_still_arrives_and_still_renders(self, env):
        """The control. A fix that dropped every icon would pass both rows above."""
        from app.activitypub.util import actor_json_to_model

        user = actor_json_to_model(_actor('good', icon={'url': 'https://peer.test/a.png'},
                                          image={'url': 'https://peer.test/b.png'}),
                                   'good', PEER)
        db.session.commit()

        assert db.session.get(File, user.avatar_id).source_url == 'https://peer.test/a.png'
        assert user.avatar_image() == 'https://peer.test/a.png'
        assert user.cover_image() == 'https://peer.test/b.png'

    def test_the_actor_is_still_created(self, env):
        """A dropped icon is not a dropped actor. `refresh_user_profile_task` is what
        picks up a rotated `publicKey`, so an actor this instance refuses to model is an
        actor whose signatures stop verifying."""
        from app.activitypub.util import actor_json_to_model

        user = actor_json_to_model(_actor('kept', icon={'url': HOSTILE}), 'kept', PEER)
        db.session.commit()

        assert isinstance(user, User)
        assert user.user_name == 'kept'
        assert user.public_key is not None


# --------------------------------------------------------------------------
# The sinks, and the API producer
# --------------------------------------------------------------------------


def test_the_templates_still_render_these_values_as_hrefs():
    """Why the reader is the boundary: nothing downstream checks. `view_url()` and
    `avatar_image()`/`cover_image()` appear in bare `href` attributes across the app, and
    if that stops being true this row should fail so the boundary can be reconsidered."""
    from pathlib import Path

    templates = Path(__file__).resolve().parent.parent / 'app' / 'templates'
    text = '\n'.join(p.read_text() for p in templates.rglob('*.html'))

    assert text.count('href="{{ post.image.view_url()') >= 6
    assert text.count('href="{{ user.avatar_image() }}"') >= 5
    assert text.count('href="{{ user.cover_image() }}"') >= 2


def test_served_path_does_not_make_a_hostile_url_safe():
    """The step between `source_url` and the page. It rewrites this instance's own
    `app/...` paths and returns everything else unchanged, so it is not a guard -- which
    is worth stating, because its name suggests it produces a local path."""
    from app.models import served_path

    assert served_path(HOSTILE) == HOSTILE
    assert served_path('app/media/x.png') == '/media/x.png'


class TestTheApiProducer:
    """`avatar` and `cover` on PUT /user/save_user_settings.

    `fields.String(allow_none=True, metadata={"format": "url"})` documents a url and
    validates nothing -- marshmallow does not read `metadata` -- so this boundary needs
    its own check rather than relying on the schema.
    """

    @pytest.mark.parametrize('field', ['avatar', 'cover'])
    @pytest.mark.parametrize('url', [HOSTILE, 'data:image/svg+xml,<svg/>', '/relative.png'])
    def test_a_url_that_is_not_http_is_refused(self, env, field, url, monkeypatch):
        from app.api.alpha.utils import user as user_utils

        user = make_user(env.peer, f'api_{field}_{abs(hash(url))}', local=True)
        db.session.commit()
        # `authorise_api_user(auth, return_type='model')` hands the function a User,
        # not an id -- `user_in_restricted_country(user)` reads `.ip_address_country`
        # off it further down, so a double returning an id fails there instead.
        monkeypatch.setattr(user_utils, 'authorise_api_user', lambda *a, **k: user)

        with pytest.raises(Exception, match='must be an http:// or https:// url'):
            user_utils.put_user_save_user_settings('token', {field: url})

    @pytest.mark.parametrize('field', ['avatar', 'cover'])
    def test_an_http_url_is_accepted(self, env, field, monkeypatch):
        """The control, and the reason this is a refusal rather than a drop: the caller
        is told, so a client sending a bad url can fix it."""
        from app.api.alpha.utils import user as user_utils

        user = make_user(env.peer, f'api_ok_{field}', local=True)
        db.session.commit()
        monkeypatch.setattr(user_utils, 'authorise_api_user', lambda *a, **k: user)
        monkeypatch.setattr(user_utils, 'make_image_sizes', lambda *a, **k: None)

        user_utils.put_user_save_user_settings('token', {field: 'https://peer.test/a.png'})

        stored = db.session.get(File, user.avatar_id if field == 'avatar' else user.cover_id)
        assert stored.source_url == 'https://peer.test/a.png'


# --------------------------------------------------------------------------
# og:image, the second producer
# --------------------------------------------------------------------------


class TestAnOpengraphImage:
    """The four `og:image` sites, whose guard was `not filename.startswith('/')`.

    `opengraph_parse` fetches the post's own url and returns the page's meta tags, so
    what it returns is chosen by whoever controls that page. Patched here, because these
    rows are about the value's scheme and not about the fetch.
    """

    @pytest.fixture
    def og(self, monkeypatch):
        def _set(value):
            page = {'og:image': value, 'og:title': 'A page'}
            # Two bindings, because the two call sites resolve the name differently:
            # `Post.new` imports `opengraph_parse` from app.utils inside the method,
            # while app/activitypub/util.py imports it at module level. Patching only
            # app.utils left the Update path calling the real one -- which made the
            # first version of the Update rows measure nothing.
            monkeypatch.setattr('app.utils.opengraph_parse', lambda url: page)
            monkeypatch.setattr('app.activitypub.util.opengraph_parse', lambda url: page)
            # `Post.new` imports these from app.utils INSIDE the method, so the
            # module attribute is what it looks up; patching app.models would be an
            # AttributeError, since the name never lands there.
            monkeypatch.setattr('app.utils.is_image_url', lambda url: False)
            monkeypatch.setattr('app.utils.is_video_url', lambda url: False)
            monkeypatch.setattr('app.utils.is_video_hosting_site', lambda url: False)

        return _set

    def _link_post(self, env, url, slug):
        from app.models import Post
        author = make_user(env.peer, f'og_{slug}')
        author.ap_profile_id = f'https://{PEER}/u/og_{slug}'
        community = make_community(f'c_{slug}')
        db.session.commit()
        activity = {'id': f'https://{PEER}/activities/{slug}', 'type': 'Create',
                    'actor': author.ap_profile_id,
                    'object': {'id': f'https://{PEER}/p/{slug}', 'type': 'Page',
                               'name': 'A link post', 'attributedTo': author.ap_profile_id,
                               'audience': community.ap_profile_id,
                               'attachment': [{'type': 'Link', 'href': url}]}}
        post = Post.new(author, community, activity)
        db.session.commit()
        return post

    @pytest.mark.parametrize('value', [HOSTILE, 'data:image/svg+xml,<svg/>', '/relative.png'])
    def test_an_ordinary_link_post_drops_a_hostile_og_image(self, env, og, value):
        og(value)

        post = self._link_post(env, 'https://example.test/article', f'a{abs(hash(value))}')

        assert post.image_id is None

    def test_an_ordinary_link_post_keeps_a_real_og_image(self, env, og):
        og('https://example.test/thumb.png')

        post = self._link_post(env, 'https://example.test/article', 'ok1')

        assert db.session.get(File, post.image_id).source_url == \
            'https://example.test/thumb.png'

    def test_the_pixelfed_branch_drops_a_hostile_og_image(self, env, og):
        """Its own copy of the idiom, reached only for a url on that host."""
        og(HOSTILE)

        post = self._link_post(env, 'https://pixelfed.social/p/someone/1', 'px')

        assert post.image_id is None

    def test_the_loops_branch_drops_a_hostile_og_image(self, env, og):
        """The third copy, which additionally rewrites `.jpg` to `.720p.mp4` -- so it
        reads the value AFTER the guard and a guard that returned the string would have
        handed it a hostile one to rewrite."""
        og(HOSTILE)

        post = self._link_post(env, 'https://loops.video/v/1', 'loops')

        assert post.image_id is None

    def test_the_update_path_drops_a_hostile_og_image(self, env, og, monkeypatch):
        """The fourth copy, in `update_post_from_activity`'s image fallback -- reached
        when the Update names a new url and the object carries no usable `image`.

        THE POST IS CREATED WITH NO og:image AT ALL ('' fails the
        `opengraph.get('og:image', '') != ''` test both branches open with), because a
        post that already has an image from `Post.new` satisfies any assertion about
        `image_id` without the Update path running at all. That is how the first version
        of this row was vacuous: a mutation of THIS line survived while both rows passed.
        """
        import contextlib

        from app.activitypub.util import update_post_from_activity

        class _Lock:
            def lock(self, *a, **k):
                return contextlib.nullcontext()

        monkeypatch.setattr('app.redis_client', _Lock())
        monkeypatch.setattr('app.activitypub.util.is_image_url', lambda url: False)
        monkeypatch.setattr('app.activitypub.util.is_video_url', lambda url: False)
        monkeypatch.setattr('app.activitypub.util.is_video_hosting_site', lambda url: False)
        og('')
        post = self._link_post(env, 'https://example.test/one', 'upd')
        assert post.image_id is None  # the setup, not the assertion under test
        og(HOSTILE)

        update_post_from_activity(post, {'object': {
            'id': post.ap_id, 'type': 'Page', 'name': 'A link post',
            'attachment': [{'type': 'Link', 'href': 'https://example.test/two'}]}})
        db.session.commit()

        assert post.image_id is None

    # Controls for the three narrow branches. Without these, a row asserting
    # `image_id is None` would pass whenever the branch did not run at all -- the
    # vacuous-row failure this campaign keeps meeting. Each one proves its branch
    # executed, by a witness only that branch produces.

    def test_the_pixelfed_branch_ran_and_keeps_a_real_og_image(self, env, og):
        from app.constants import POST_TYPE_IMAGE
        og('https://pixelfed.social/storage/thumb.jpg')

        post = self._link_post(env, 'https://pixelfed.social/p/someone/2', 'pxok')

        assert post.type == POST_TYPE_IMAGE
        assert db.session.get(File, post.image_id).source_url == \
            'https://pixelfed.social/storage/thumb.jpg'

    def test_the_loops_branch_ran_and_rewrites_the_extension(self, env, og):
        """`.jpg` becomes `.720p.mp4` only in that branch, so the stored value names
        which copy of the idiom ran."""
        from app.constants import POST_TYPE_VIDEO
        og('https://loops.video/storage/thumb.jpg')

        post = self._link_post(env, 'https://loops.video/v/2', 'loopsok')

        assert post.type == POST_TYPE_VIDEO
        assert db.session.get(File, post.image_id).source_url == \
            'https://loops.video/storage/thumb.720p.mp4'

    def test_the_update_path_ran_and_keeps_a_real_og_image(self, env, og, monkeypatch):
        """The witness for the row above: same setup, a fetchable og:image on the Update,
        and the image appears -- so the branch really is reached with a post that had
        none, and the refusal above is a refusal rather than an unvisited branch."""
        import contextlib

        from app.activitypub.util import update_post_from_activity

        class _Lock:
            def lock(self, *a, **k):
                return contextlib.nullcontext()

        monkeypatch.setattr('app.redis_client', _Lock())
        monkeypatch.setattr('app.activitypub.util.is_image_url', lambda url: False)
        monkeypatch.setattr('app.activitypub.util.is_video_url', lambda url: False)
        monkeypatch.setattr('app.activitypub.util.is_video_hosting_site', lambda url: False)
        og('')
        post = self._link_post(env, 'https://example.test/three', 'updok')
        assert post.image_id is None
        og('https://example.test/thumb.png')

        update_post_from_activity(post, {'object': {
            'id': post.ap_id, 'type': 'Page', 'name': 'A link post',
            'attachment': [{'type': 'Link', 'href': 'https://example.test/four'}]}})
        db.session.commit()

        assert db.session.get(File, post.image_id).source_url == \
            'https://example.test/thumb.png'
