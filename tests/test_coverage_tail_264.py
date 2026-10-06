"""Round 264: re-fetching a remote actor, and the Announce a community publishes.

`refresh_user_profile_task` re-reads a remote account's Person document and writes what it finds
onto the local row. It is the only path by which a peer's ROTATED SIGNING KEY reaches this
instance, so an arm that returns early or raises is an actor whose key, bio, avatar and indexable
flag all stop being picked up. `tests/test_ap_refresh_profiles.py` covers the document-reading; the
arms here are the ones around it:

    the failure      one `httpx.HTTPError` is `return`, with nothing written and no second
                     attempt (D224) -- NOT an exception, because this task re-raises and a raised
                     task is retried by celery.
    the old file     an avatar or cover whose url CHANGED deletes the bytes it replaces. Without
                     it every refresh of every actor leaves another orphan under `app/static`.
    the rollback     `except: session.rollback(); raise`. This task runs on its own session, and a
                     half-applied profile is what the rollback exists to prevent.

`post_to_activity` is the other half: the `Announce` envelope a community's outbox publishes for
each of its posts. Every existing test doubles it (`tests/test_ap_collections.py`), so its shape
had no assertion anywhere -- and the shape is what other servers read.
"""
from unittest.mock import patch

import httpx
import pytest

from app import db
from app.activitypub import util as ap_util
from app.activitypub.util import post_to_activity, post_to_page, refresh_user_profile_task
from app.models import File, Language, User, utcnow
from tests.factories import (make_community, make_post, make_user, seed_community_owner,
                             seed_signing_site)
from tests.app_source import app_trees

PEER = 'peer.example'


@pytest.fixture
def remote_user(db_session):
    """A remote account on an online instance, ready to refresh.

    `ap_public_url` is what the task fetches. `seed_signing_site` supplies the Site row the
    signed-request fallback reads, so a test that reaches it fails on the fetch rather than on a
    missing key.
    """
    seed_signing_site()
    instance = seed_community_owner(PEER)
    user = make_user(instance, 'wakko')
    user.ap_public_url = f'https://{PEER}/u/wakko'
    user.ap_profile_id = f'https://{PEER}/u/wakko'
    db.session.commit()
    return user


def person_document(**fields):
    """The peer's Person document, with only the keys the task reads unconditionally."""
    document = {
        'type': 'Person',
        'id': f'https://{PEER}/u/wakko',
        'preferredUsername': 'wakko',
        'publicKey': {'publicKeyPem': '-----BEGIN PUBLIC KEY-----refreshed'},
    }
    document.update(fields)
    return document


def answered_with(*responses):
    """Patch the task's fetch with a scripted sequence.

    `get_request` is patched rather than the transport because what is under test is the task's own
    retry, and `get_request` is where its `httpx.HTTPError` contract is established -- every
    transport failure is normalised to that one class before it reaches here.
    """
    return patch.object(ap_util, 'get_request', side_effect=list(responses))


def a_document(**fields):
    return httpx.Response(200, json=person_document(**fields))


@pytest.fixture(autouse=True)
def no_sleeping():
    """A sleep in the task would be real time in a test, and D224 asserts there is none."""
    with patch.object(ap_util.time, 'sleep') as sleeping:
        yield sleeping


# --------------------------------------------------------------------------
# A peer that does not answer the first time
# --------------------------------------------------------------------------


class TestWhenTheFirstFetchFails:

    def test_one_failure_leaves_the_profile_alone(self, remote_user, no_sleeping):
        """D224, fixed (owner ruling): `except httpx.HTTPError: return`, with no inline
        `time.sleep(randint(3, 10))` and no second fetch -- `get_request` already retries a read
        error itself, and the sleep parked a celery worker. Not a raise: this task re-raises from
        its outer handler, and celery would retry a raised task. The profile this instance holds
        is kept as it stands.
        """
        remote_user.public_key = '-----BEGIN PUBLIC KEY-----the one we hold'
        remote_user.ap_fetched_at = None
        db.session.commit()

        with answered_with(httpx.HTTPError('first attempt timed out'),
                           a_document()) as fetching:
            refresh_user_profile_task(remote_user.id)

        assert fetching.call_count == 1
        assert not no_sleeping.called
        db.session.expire_all()
        refreshed = db.session.get(User, remote_user.id)
        assert refreshed.public_key == '-----BEGIN PUBLIC KEY-----the one we hold'
        assert refreshed.ap_fetched_at is None

    def test_a_peer_whose_instance_is_offline_is_not_fetched_at_all(self, remote_user):
        """`user.instance.online()` gates the whole task. An instance marked gone is not asked
        again on every page that renders one of its accounts."""
        remote_user.instance.gone_forever = True
        db.session.commit()

        with answered_with(a_document()) as fetching:
            refresh_user_profile_task(remote_user.id)

        assert fetching.call_count == 0


class TestWhenSomethingRaisesMidRefresh:

    def test_the_session_is_rolled_back_and_the_error_re_raised(self, remote_user):
        """`except Exception: session.rollback(); raise`.

        This task runs on its OWN session (`get_task_session`), so a failure halfway through
        leaves writes that no request will ever roll back -- the name updated and the key not, say.
        The re-raise is what lets celery record the failure rather than logging a success.

        `session.rollback()` is an EQUIVALENT MUTANT here, and provably so rather than for want of
        a row: `finally: session.close()` follows it, and closing a SQLAlchemy session releases its
        transaction, which discards everything uncommitted. Removing the rollback changes nothing
        no test could see. It is kept for saying what is meant at the point the failure is caught,
        and because a `close()` that stops being the last word would leave nothing behind it. The
        assertion below is on the ROW, so it holds either way -- what it distinguishes is the
        `raise`.
        """
        remote_user.title = 'the title we hold'
        db.session.commit()

        with answered_with(a_document(name='A new title', summary='a bio')), \
                patch.object(ap_util, 'allowlist_html',
                             side_effect=RuntimeError('rendering blew up')):
            with pytest.raises(RuntimeError, match='rendering blew up'):
                refresh_user_profile_task(remote_user.id)

        db.session.expire_all()
        assert db.session.get(User, remote_user.id).title == 'the title we hold'


# --------------------------------------------------------------------------
# What the document is allowed to say
# --------------------------------------------------------------------------


class TestTheBioAsThePeerSentIt:

    def test_a_summary_with_no_markup_is_wrapped_in_a_paragraph(self, remote_user):
        """The `# PeerTube` comment names the peer: a bio arriving as plain text would otherwise
        be rendered as one unbroken line beside every other actor's paragraphs."""
        with answered_with(a_document(summary='just some plain text')):
            refresh_user_profile_task(remote_user.id)

        db.session.expire_all()
        refreshed = db.session.get(User, remote_user.id)
        assert refreshed.about_html == '<p>just some plain text</p>'
        assert refreshed.about == 'just some plain text'

    def test_a_summary_that_is_already_markup_is_left_as_it_is(self, remote_user):
        """The False side, and the reason the test is on `startswith('<')` rather than on the
        peer's software: double-wrapping would be `<p><p>...`."""
        with answered_with(a_document(summary='<p>already a paragraph</p>')):
            refresh_user_profile_task(remote_user.id)

        db.session.expire_all()
        assert db.session.get(User, remote_user.id).about_html == \
            '<p>already a paragraph</p>'

    def test_a_deleted_title_is_stored_as_empty(self, remote_user):
        """`if user.title and user.title.strip().lower() == '[deleted]': user.title = ''`.

        Lemmy publishes `[deleted]` as the display name of a deleted account, and it is a
        LITERAL that would otherwise be rendered as somebody's name on every post they made. The
        comparison is stripped and case-folded, so the variants peers send all reach it.
        """
        with answered_with(a_document(name=' [DELETED] ')):
            refresh_user_profile_task(remote_user.id)

        db.session.expire_all()
        assert db.session.get(User, remote_user.id).title == ''

    def test_an_ordinary_title_survives(self, remote_user):
        with answered_with(a_document(name='Wakko Warner')):
            refresh_user_profile_task(remote_user.id)

        db.session.expire_all()
        assert db.session.get(User, remote_user.id).title == 'Wakko Warner'


# --------------------------------------------------------------------------
# The bytes a changed avatar replaces
# --------------------------------------------------------------------------


class TestWhatHappensToTheOldImage:
    """Both blocks are `if <thing>_id and <url> != <thing>.source_url: delete_from_disk()`, then a
    new `File`. Without the delete, every refresh of every remote actor whose avatar changed leaves
    the replaced bytes under `app/static` with no row naming them -- unreachable and unsweepable.
    """

    def _with_avatar(self, user, url):
        avatar = File(source_url=url, file_path='app/static/media/users/ab/cd/old-avatar.png')
        db.session.add(avatar)
        user.avatar = avatar
        db.session.commit()
        return avatar

    def _with_cover(self, user, url):
        cover = File(source_url=url, file_path='app/static/media/users/ab/cd/old-cover.png')
        db.session.add(cover)
        user.cover = cover
        db.session.commit()
        return cover

    def test_a_changed_avatar_deletes_the_bytes_it_replaces(self, remote_user):
        old = self._with_avatar(remote_user, f'https://{PEER}/avatars/one.png')
        old_id = old.id
        deleted = []

        with patch.object(File, 'delete_from_disk',
                          side_effect=lambda self=None: deleted.append('called')), \
                answered_with(a_document(icon={'type': 'Image',
                                               'url': f'https://{PEER}/avatars/two.png'})):
            refresh_user_profile_task(remote_user.id)

        assert deleted != []
        db.session.expire_all()
        refreshed = db.session.get(User, remote_user.id)
        assert refreshed.avatar_id != old_id
        assert db.session.get(File, refreshed.avatar_id).source_url == \
            f'https://{PEER}/avatars/two.png'

    def test_an_unchanged_avatar_is_neither_deleted_nor_replaced(self, remote_user):
        """The False side, and the one that matters for cost: the same url on every refresh must
        not re-download the image or churn a `File` row per refresh."""
        old = self._with_avatar(remote_user, f'https://{PEER}/avatars/one.png')
        old_id = old.id
        deleted = []

        with patch.object(File, 'delete_from_disk',
                          side_effect=lambda self=None: deleted.append('called')), \
                answered_with(a_document(icon={'type': 'Image',
                                               'url': f'https://{PEER}/avatars/one.png'})):
            refresh_user_profile_task(remote_user.id)

        assert deleted == []
        db.session.expire_all()
        assert db.session.get(User, remote_user.id).avatar_id == old_id

    def test_a_changed_cover_deletes_the_bytes_it_replaces(self, remote_user):
        """The second block, which is the first one again for `image` rather than `icon` -- two
        copies, so a fix applied to one says nothing about the other."""
        old = self._with_cover(remote_user, f'https://{PEER}/covers/one.png')
        old_id = old.id
        deleted = []

        with patch.object(File, 'delete_from_disk',
                          side_effect=lambda self=None: deleted.append('called')), \
                answered_with(a_document(image={'type': 'Image',
                                                'url': f'https://{PEER}/covers/two.png'})):
            refresh_user_profile_task(remote_user.id)

        assert deleted != []
        db.session.expire_all()
        assert db.session.get(User, remote_user.id).cover_id != old_id

    def test_an_actor_with_no_avatar_yet_just_gets_one(self, remote_user):
        """`if not user.avatar_id or ...`. Nothing to delete, and the new `File` still has to be
        attached -- the first refresh after an actor adds an avatar."""
        deleted = []

        with patch.object(File, 'delete_from_disk',
                          side_effect=lambda self=None: deleted.append('called')), \
                answered_with(a_document(icon={'type': 'Image',
                                               'url': f'https://{PEER}/avatars/first.png'})):
            refresh_user_profile_task(remote_user.id)

        assert deleted == []
        db.session.expire_all()
        refreshed = db.session.get(User, remote_user.id)
        assert refreshed.avatar_id is not None


class TestNothingShellsOutToOpensslForAKey:
    """D1430. `app/activitypub/util.py` carried a `public_key()` with NO CALLERS anywhere in the
    repository, whose first arm ran

        os.system('openssl genrsa -out private.pem 2048')
        os.system('openssl rsa -in private.pem -outform PEM -pubout -out public.pem')

    -- writing a private key into the process's working directory, which for this application is
    the repository root beside `app/static`. Its other arm read `./public.pem` back and returned it
    with the newlines escaped. Nothing produced or consumed those files: this instance's keypair
    lives in `Site.private_key` / `Site.public_key`, generated by `RsaKeys.generate_keypair()` in
    `app/cli.py`.

    The function is deleted. This row is the guard against it coming back, and against a second
    copy appearing elsewhere: a key this application uses is generated in-process and stored in the
    database, never shelled out to a file beside the code it serves.
    """

    def test_no_module_under_app_shells_out_to_openssl(self):
        import ast
        import pathlib

        offenders = []
        for path, _source, tree in app_trees():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                name = ast.unparse(node.func)
                if name not in ('os.system', 'subprocess.run', 'subprocess.call',
                                'subprocess.Popen', 'subprocess.check_output'):
                    continue
                rendered = ast.unparse(node)
                if 'openssl' in rendered:
                    offenders.append(f'{path}:{node.lineno}: {rendered[:60]}')
        assert offenders == [], f'openssl shelled out: {offenders}'

    def test_the_dead_public_key_helper_is_gone(self):
        """Named rather than only pattern-matched, so a reader of a future diff finds the reason
        here. `public_key` remains a COLUMN on Site, User, Community and Feed, and a method name in
        `app/activitypub/signature.py`; what must not exist is a module-level function of that name
        in `app/activitypub/util.py`."""
        import ast

        tree = ast.parse(open('app/activitypub/util.py').read())
        module_level = [node.name for node in tree.body
                        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
        assert 'public_key' not in module_level


# --------------------------------------------------------------------------
# The Announce a community's outbox publishes
# --------------------------------------------------------------------------


class TestTheAnnounceACommunityPublishes:
    """`post_to_activity` builds what `/c/<name>/outbox` returns for each post. Every other test
    doubles this function, so nothing asserted its shape -- and the shape is what a peer reads to
    decide who posted what, and on whose authority.
    """

    @pytest.fixture
    def a_post(self, db_session):
        seed_signing_site()
        instance = seed_community_owner(PEER)
        author = make_user(instance, 'poster')
        community = make_community('announceland')
        db.session.commit()
        post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
        db.session.commit()
        return post

    def test_the_envelope_is_an_announce_from_the_community(self, a_post):
        """The community is the ACTOR of the Announce and the author is the actor of the Create
        inside it. That pair is what says "this community relayed that person's post", and
        swapping them would have the community claiming authorship."""
        activity = post_to_activity(a_post, a_post.community)

        assert activity['type'] == 'Announce'
        assert activity['actor'] == a_post.community.public_url()
        assert activity['object']['type'] == 'Create'
        assert activity['object']['actor'] == a_post.author.public_url()

    def test_both_layers_are_addressed_to_the_public_collection(self, a_post):
        """An outbox is public by definition, and a peer reading a missing `to` has to guess."""
        public = 'https://www.w3.org/ns/activitystreams#Public'
        activity = post_to_activity(a_post, a_post.community)

        assert activity['to'] == [public]
        assert activity['object']['to'] == [public]
        assert activity['cc'] == [f'{a_post.community.public_url()}/followers']
        assert activity['object']['cc'] == [a_post.community.public_url()]

    def test_the_post_itself_is_the_inner_object(self, a_post):
        activity = post_to_activity(a_post, a_post.community)

        assert activity['object']['object'] == post_to_page(a_post)
        assert activity['object']['object']['id'] == a_post.ap_id

    def test_a_remote_posts_own_ids_are_reused(self, a_post):
        """`post.ap_create_id if post.ap_create_id else <generated>`. A post that arrived from
        another server already HAS a create and an announce id, and publishing new ones would
        make this instance's copy a different activity from everybody else's."""
        a_post.ap_create_id = f'https://{PEER}/activities/create/1'
        a_post.ap_announce_id = f'https://{PEER}/activities/announce/1'
        db.session.commit()

        activity = post_to_activity(a_post, a_post.community)

        assert activity['id'] == f'https://{PEER}/activities/announce/1'
        assert activity['object']['id'] == f'https://{PEER}/activities/create/1'

    def test_a_local_post_gets_ids_on_this_instance(self, a_post):
        """The other side: a locally created post has neither, and both generated ids have to be
        on this instance's own domain, because a peer dereferences them."""
        a_post.ap_create_id = None
        a_post.ap_announce_id = None
        db.session.commit()
        server_url = ap_util.current_app.config['SERVER_URL']

        activity = post_to_activity(a_post, a_post.community)

        assert activity['id'].startswith(f'{server_url}/activities/announce/')
        assert activity['object']['id'].startswith(f'{server_url}/activities/create/')

    def test_two_calls_for_a_local_post_do_not_agree(self, a_post):
        """`gibberish(15)` per call, which is worth pinning as OBSERVED: the ids are not stored,
        so the same post announced twice is two activities. A peer deduplicating on activity id
        sees both."""
        a_post.ap_create_id = None
        a_post.ap_announce_id = None
        db.session.commit()

        first = post_to_activity(a_post, a_post.community)
        second = post_to_activity(a_post, a_post.community)

        assert first['id'] != second['id']


class TestWhatThePagePublishesAboutItself:

    @pytest.fixture
    def a_post(self, db_session):
        seed_signing_site()
        instance = seed_community_owner(PEER)
        author = make_user(instance, 'poster')
        community = make_community('pageland')
        db.session.commit()
        post = make_post(community, author, ap_id=f'https://{PEER}/post/2')
        db.session.commit()
        return post

    def test_a_language_adds_a_content_map(self, a_post):
        """`contentMap` is how a peer knows which language the body is in, and it is keyed by the
        code -- so the key has to be the post's own language rather than the instance default."""
        language = Language(code='en', name='English')
        db.session.add(language)
        db.session.commit()
        a_post.language_id = language.id
        db.session.commit()

        page = post_to_page(a_post)
        assert page['contentMap'] == {a_post.language_code(): page['content']}

    def test_no_language_publishes_no_content_map(self, a_post):
        a_post.language_id = None
        db.session.commit()

        assert 'contentMap' not in post_to_page(a_post)

    def test_an_edited_post_publishes_its_edit_time(self, a_post):
        """`updated` is what tells a peer to replace the copy it holds. Without it an edit made
        here never reaches the servers that already have the post."""
        edited = utcnow()
        a_post.edited_at = edited
        db.session.commit()

        page = post_to_page(a_post)

        assert page['updated'] == ap_util.ap_datetime(edited)

    def test_an_unedited_post_publishes_no_edit_time(self, a_post):
        assert a_post.edited_at is None
        assert 'updated' not in post_to_page(a_post)
