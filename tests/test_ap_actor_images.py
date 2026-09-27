"""The `icon` and `image` a peer offers on an actor, in every shape it can take.

Sub-project 117 -- `image_url_from` in `app/activitypub/util.py`, and the six
places that used to read those two keys by hand: `refresh_user_profile_task`,
`refresh_community_profile_task` and `refresh_feed_profile_task`, each for both
keys.

All six read the same way, and five of them guarded the dict case but not the
list they indexed into:

    elif isinstance(activity_json['icon'], list) and 'url' in activity_json['icon'][-1]:

`icon: []` is `IndexError` there, and `icon: [5]` is `TypeError: argument of type
'int' is not iterable`. The sixth -- the user task's `image` -- had no guards at
all. The task rolls back and re-raises, so a peer serving one of those documents
is never refreshed again while it keeps serving it, and
`refresh_user_profile_task` is what picks up a rotated `publicKey`.

Which end of a list is read is unchanged: the LAST entry for an icon, the FIRST
for an image.
"""
import httpx
import pytest

from app import db
from app.activitypub import util as ap_util
from app.activitypub.util import (image_url_from, refresh_community_profile_task,
                                  refresh_feed_profile_task,
                                  refresh_user_profile_task)
from app.models import Community, Feed, User
from tests.factories import (make_community, make_feed, make_instance, make_user,
                             seed_community_owner)

PEER = 'peer.example'
A_PNG = 'https://peer.example/media/a.png'
B_PNG = 'https://peer.example/media/b.png'

# Every shape a peer can put in `icon` or `image`, with the url each one names
# when read from the front of a list and from the back of it.
SHAPES = [
    ('a dict', {'type': 'Image', 'url': A_PNG}, A_PNG, A_PNG),
    ('a dict with no url', {'type': 'Image'}, None, None),
    ('a dict whose url is null', {'url': None}, None, None),
    ('a dict whose url is a number', {'url': 5}, None, None),
    ('a dict whose url is empty', {'url': ''}, None, None),
    ('a one-entry list', [{'url': A_PNG}], A_PNG, A_PNG),
    ('a two-entry list', [{'url': A_PNG}, {'url': B_PNG}], A_PNG, B_PNG),
    ('an empty list', [], None, None),
    # A bare url string, in the list or on its own. `actor_json_to_model` has
    # always taken these for `icon`; the refresh tasks ignored them, so an actor
    # created with one had an avatar until its first refresh. One reading now.
    ('a list of strings', [A_PNG], A_PNG, A_PNG),
    ('a list of two strings', [A_PNG, B_PNG], A_PNG, B_PNG),
    ('a list of numbers', [5], None, None),
    ('a list of empty dicts', [{}], None, None),
    ('a list whose entry url is a number', [{'url': 5}], None, None),
    ('a list whose entry url is null', [{'url': None}], None, None),
    ('a list whose entry url is empty', [{'url': ''}], None, None),
    ('a list whose other end is usable', [{}, {'url': B_PNG}], None, B_PNG),
    ('a bare string', A_PNG, A_PNG, A_PNG),
    ('an empty string', '', None, None),
    ('a number', 5, None, None),
    ('null', None, None, None),
    ('a nested list', [[{'url': A_PNG}]], None, None),
]


class TestReadingTheUrlOutOfIt:
    @pytest.mark.parametrize('label,value,from_front,_from_back',
                             SHAPES, ids=[shape[0] for shape in SHAPES])
    def test_from_the_front(self, label, value, from_front, _from_back):
        assert image_url_from(value) == from_front

    @pytest.mark.parametrize('label,value,_from_front,from_back',
                             SHAPES, ids=[shape[0] for shape in SHAPES])
    def test_from_the_back(self, label, value, _from_front, from_back):
        assert image_url_from(value, prefer_last=True) == from_back

    def test_the_two_ends_differ_for_a_list(self):
        """The property that makes `prefer_last` worth having: an icon takes the
        last entry, where the largest is conventionally offered, and an image the
        first."""
        value = [{'url': A_PNG}, {'url': B_PNG}]
        assert image_url_from(value) == A_PNG
        assert image_url_from(value, prefer_last=True) == B_PNG

    def test_it_never_raises_whatever_it_is_given(self):
        """The defect was an exception, not a wrong answer, so the property is
        that nothing gets out."""
        for _label, value, _front, _back in SHAPES:
            image_url_from(value)
            image_url_from(value, prefer_last=True)
        for value in (object(), (), set(), b'bytes', {'url': ['x']}, [None]):
            assert image_url_from(value) is None
            assert image_url_from(value, prefer_last=True) is None


def _remote_user(name='wakko'):
    instance = seed_community_owner(PEER)
    user = make_user(instance, name)
    user.ap_public_url = f'https://{PEER}/u/{name}'
    user.ap_profile_id = f'https://{PEER}/u/{name}'
    db.session.commit()
    return user


def _person(username='wakko', **fields):
    """`username` is the preferredUsername; a `name=` in `fields` is the DISPLAY
    name, which the task writes to `user.title`. They are separate keys and
    cannot share one argument."""
    document = {'type': 'Person', 'id': f'https://{PEER}/u/{username}',
                'preferredUsername': username,
                'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'}}
    document.update(fields)
    return document


def _serve(http_mock, url, document):
    return http_mock.get(url).mock(return_value=httpx.Response(200, json=document))


# Shapes that used to raise inside a refresh task and name no image at all, so a
# refresh must now finish and store nothing.
BROKEN_SHAPES = [
    ('an empty list', []),
    ('a list of numbers', [5]),
    ('a list of empty dicts', [{}]),
    ('a dict with no url', {'type': 'Image'}),
    ('a number', 5),
]

# Shapes that used to raise and DO name an image, so a refresh must now store it.
BARE_STRING_SHAPES = [
    ('a bare string', A_PNG),
    ('a list of strings', [A_PNG]),
]


class TestRefreshingAUser:
    """`refresh_user_profile_task`. Its `image` arm is the one that had no
    guards at all, so a document a peer can serve took the whole refresh down --
    including the `publicKey` it carries."""

    @pytest.mark.parametrize('label,shape', BROKEN_SHAPES,
                             ids=[shape[0] for shape in BROKEN_SHAPES])
    @pytest.mark.parametrize('key', ['icon', 'image'])
    def test_a_document_that_used_to_raise(self, app, db_session, http_mock,
                                           key, label, shape):
        user = _remote_user()
        user.user_name = 'stale'
        db.session.commit()
        _serve(http_mock, user.ap_public_url,
               _person(**{key: shape, 'name': 'Wakko Warner'}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.title == 'Wakko Warner'
        assert user.avatar_id is None
        assert user.cover_id is None

    @pytest.mark.parametrize('label,shape', BARE_STRING_SHAPES,
                             ids=[shape[0] for shape in BARE_STRING_SHAPES])
    def test_a_bare_url_is_taken_as_the_avatar(self, app, db_session, http_mock,
                                              label, shape):
        """These used to raise in the refresh task and are accepted by
        `actor_json_to_model`, so a refresh now agrees with the creation."""
        user = _remote_user()
        _serve(http_mock, user.ap_public_url, _person(icon=shape))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.avatar.source_url == A_PNG

    def test_the_public_key_is_still_applied(self, app, db_session, http_mock):
        """Why the crash mattered: this is the column that makes a peer's
        signatures verifiable, and a refresh that raises never writes it."""
        user = _remote_user()
        user.public_key = 'stale key'
        db.session.commit()
        _serve(http_mock, user.ap_public_url, _person(image=[]))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.public_key == '-----BEGIN PUBLIC KEY-----refreshed'

    def test_an_avatar_is_taken_from_the_last_entry(self, app, db_session,
                                                   http_mock):
        user = _remote_user()
        _serve(http_mock, user.ap_public_url,
               _person(icon=[{'url': A_PNG}, {'url': B_PNG}]))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.avatar.source_url == B_PNG

    def test_a_cover_is_taken_from_the_first(self, app, db_session, http_mock):
        user = _remote_user()
        _serve(http_mock, user.ap_public_url,
               _person(image=[{'url': A_PNG}, {'url': B_PNG}]))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.cover.source_url == A_PNG

    def test_an_avatar_as_a_plain_dict(self, app, db_session, http_mock):
        user = _remote_user()
        _serve(http_mock, user.ap_public_url, _person(icon={'url': A_PNG}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.avatar.source_url == A_PNG

    def test_a_cover_as_a_plain_dict(self, app, db_session, http_mock):
        user = _remote_user()
        _serve(http_mock, user.ap_public_url, _person(image={'url': A_PNG}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.cover.source_url == A_PNG


class TestRefreshingACommunity:
    def _remote_community(self, name='memes'):
        seed_community_owner(PEER)
        community = make_community(name)
        community.ap_id = f'{name}@{PEER}'
        community.ap_profile_id = f'https://{PEER}/c/{name}'
        community.ap_public_url = f'https://{PEER}/c/{name}'
        community.ap_followers_url = None
        db.session.commit()
        return community

    def _group(self, name='memes', **fields):
        document = {'type': 'Group', 'id': f'https://{PEER}/c/{name}',
                    'preferredUsername': name, 'name': 'Memes, refreshed',
                    'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'}}
        document.update(fields)
        return document

    @pytest.mark.parametrize('label,shape', BROKEN_SHAPES,
                             ids=[shape[0] for shape in BROKEN_SHAPES])
    @pytest.mark.parametrize('key', ['icon', 'image'])
    def test_a_document_that_used_to_raise(self, app, db_session, http_mock,
                                          key, label, shape):
        community = self._remote_community()
        refresh_community_profile_task(community.id,
                                       self._group(**{key: shape}))
        db.session.refresh(community)
        assert community.title == 'Memes, refreshed'
        assert community.icon_id is None
        assert community.image_id is None

    def test_an_icon_comes_from_the_last_entry(self, app, db_session, http_mock):
        community = self._remote_community()
        refresh_community_profile_task(
            community.id, self._group(icon=[{'url': A_PNG}, {'url': B_PNG}]))
        db.session.refresh(community)
        assert community.icon.source_url == B_PNG

    def test_an_image_comes_from_the_first(self, app, db_session, http_mock):
        community = self._remote_community()
        refresh_community_profile_task(
            community.id, self._group(image=[{'url': A_PNG}, {'url': B_PNG}]))
        db.session.refresh(community)
        assert community.image.source_url == A_PNG


class TestRefreshingAFeed:
    def _remote_feed(self, name='news'):
        instance = seed_community_owner(PEER)
        feed = make_feed(instance, name)
        feed.ap_public_url = f'https://{PEER}/f/{name}'
        feed.ap_profile_id = f'https://{PEER}/f/{name}'
        feed.ap_followers_url = None
        db.session.commit()
        return feed

    def _feed_document(self, name='news', **fields):
        document = {'type': 'Feed', 'id': f'https://{PEER}/f/{name}',
                    'preferredUsername': name, 'name': 'News, refreshed',
                    'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'}}
        document.update(fields)
        return document

    @pytest.mark.parametrize('label,shape', BROKEN_SHAPES,
                             ids=[shape[0] for shape in BROKEN_SHAPES])
    @pytest.mark.parametrize('key', ['icon', 'image'])
    def test_a_document_that_used_to_raise(self, app, db_session, http_mock,
                                          key, label, shape):
        feed = self._remote_feed()
        _serve(http_mock, feed.ap_public_url,
               self._feed_document(**{key: shape}))

        refresh_feed_profile_task(feed.id)

        db.session.refresh(feed)
        assert feed.title == 'News, refreshed'
        assert feed.icon_id is None
        assert feed.image_id is None

    def test_an_icon_comes_from_the_last_entry(self, app, db_session, http_mock):
        feed = self._remote_feed()
        _serve(http_mock, feed.ap_public_url,
               self._feed_document(icon=[{'url': A_PNG}, {'url': B_PNG}]))

        refresh_feed_profile_task(feed.id)

        db.session.refresh(feed)
        assert feed.icon.source_url == B_PNG

    def test_an_image_comes_from_the_first(self, app, db_session, http_mock):
        feed = self._remote_feed()
        _serve(http_mock, feed.ap_public_url,
               self._feed_document(image=[{'url': A_PNG}, {'url': B_PNG}]))

        refresh_feed_profile_task(feed.id)

        db.session.refresh(feed)
        assert feed.image.source_url == A_PNG


class TestTheHandReadingIsGone:
    def test_no_code_indexes_into_an_icon_or_image_by_hand(self):
        """The property. Six copies of this read existed in
        `app/activitypub/util.py`; a seventh sat in `app/models.py`'s Video branch
        and survived this test for seven rounds, because the test read one file
        (D1341). It reads all of them now -- fact 687 is about exactly this, and
        the scan was still narrower than the defect.
        """
        import ast
        from pathlib import Path

        def prose_lines(tree):
            """Every line held by a string literal -- docstrings included.

            Skipping lines that merely START with `#` is not enough: this test
            found its own docstring, which quotes the pattern it forbids, and the
            production fix quotes it too in the comment that explains itself.
            """
            held = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    held.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
            return held

        offenders = []
        for path in sorted(Path('app').rglob('*.py')):
            source = path.read_text(encoding='utf8')
            prose = prose_lines(ast.parse(source))
            for number, line in enumerate(source.splitlines(), start=1):
                stripped = line.strip()
                if stripped.startswith('#') or number in prose:
                    continue
                for key in ("['icon']", "['image']"):
                    if f"{key}[-1]" in stripped or f"{key}[0]" in stripped:
                        offenders.append(f'{path}:{number}: {stripped}')
        assert offenders == []


class TestAnActorBecomingARowForTheFirstTime:
    """`actor_json_to_model` reads the same two keys, and had the same six
    copies of the hand-written read."""

    def test_a_new_user_takes_its_avatar_from_the_last_entry(self, app, db_session):
        from app.activitypub.util import actor_json_to_model
        from tests.factories import make_instance, peer_actor_json

        make_instance(PEER)
        document = peer_actor_json(name='alice', server=PEER,
                                   fields={'icon': [{'url': A_PNG}, {'url': B_PNG}]})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.avatar.source_url == B_PNG

    def test_a_new_user_takes_its_cover_from_the_first(self, app, db_session):
        from app.activitypub.util import actor_json_to_model
        from tests.factories import make_instance, peer_actor_json

        make_instance(PEER)
        document = peer_actor_json(name='alice', server=PEER,
                                   fields={'image': [{'url': A_PNG}, {'url': B_PNG}]})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user.cover.source_url == A_PNG

    @pytest.mark.parametrize('label,shape', BROKEN_SHAPES,
                             ids=[shape[0] for shape in BROKEN_SHAPES])
    @pytest.mark.parametrize('key', ['icon', 'image'])
    def test_a_document_that_used_to_raise(self, app, db_session, key, label, shape):
        """`icon: []` was an IndexError here too, and this is the path a peer
        reaches simply by being mentioned for the first time."""
        from app.activitypub.util import actor_json_to_model
        from tests.factories import make_instance, peer_actor_json

        make_instance(PEER)
        document = peer_actor_json(name='alice', server=PEER,
                                   fields={key: shape})
        user = actor_json_to_model(document, 'alice', PEER)
        assert user is not None
        assert user.avatar_id is None
        assert user.cover_id is None
