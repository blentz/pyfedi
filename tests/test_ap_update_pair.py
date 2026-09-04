"""The mirrored core of update_post_from_activity and
update_post_reply_from_activity -- the halves of the two functions that do the
same job on a Post and on a PostReply.

Entry is a direct call. Both functions take an already-loaded row and a dict;
neither fetches the object it is applying, so no HTTP mock is needed for
anything in this file.

Both open with `with redis_client.lock(...)`, which the shared `redis_double`
fixture cannot serve: fakeredis without lupa has no Lua scripting, and
redis-py's `Lock.release()` issues an EVALSHA. Every test here takes
`redis_lock_only_double` instead, whose `.lock()` is a nullcontext.

Both functions commit on `db.session`, unlike the refresh tasks of
sub-project 13, which used a separate `get_task_session()`. `expire_on_commit`
is at SQLAlchemy's default True -- the app factory overrides only `autoflush`
-- so a commit inside the function under test expires these objects and the
next attribute access re-loads them. No explicit refresh is needed here, and
tests that would need one elsewhere say so.
"""
import contextlib

import pytest

from app import db
from app.activitypub.util import (update_post_from_activity,
                                  update_post_reply_from_activity)
from app.constants import NOTIF_MENTION
from app.models import Language, Notification, PostReply, User
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_post,
                             make_post_reply, make_site, make_user)

PEER = 'peer.example'


class _RedisLockOnlyDouble:
    """`app.redis_client` stand-in covering only `.lock(...)` as a context
    manager. Same shape as tests/test_inbox_dispatch_votes.py's, and for the
    same reason -- see this module's docstring.
    """

    def lock(self, *args, **kwargs):
        return contextlib.nullcontext()


@pytest.fixture
def redis_lock_only_double(monkeypatch):
    monkeypatch.setattr('app.redis_client', _RedisLockOnlyDouble())


def _seed_post(software='lemmy'):
    """A local community owned by user 1, a remote author on PEER, and one Post.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the same pattern
    tests/test_inbox_dispatch_votes.py documents.

    `software` reaches `post.instance.software`, which the flair branch reads.
    """
    make_site()
    instance = make_instance(PEER, software=software)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    author = make_user(instance, 'author')
    post = make_post(community, author, ap_id=f'https://{PEER}/objects/1')
    db.session.commit()
    return post


def _seed_reply(software='lemmy'):
    """The same scenario plus one PostReply on the Post.

    `software` reaches `reply.instance.software`, which gates the Mention
    de-duplication block: `make_instance` defaults to 'mastodon', which IS in
    MICROBLOG_APPS, so the default would silently take the de-dup path. This
    helper defaults to 'lemmy' so the simple path is the default and a test
    that wants de-duplication asks for it.
    """
    post = _seed_post(software=software)
    author = db.session.query(User).get(post.user_id)
    reply = make_post_reply(post, author, body='before')
    db.session.commit()
    return reply


def _update(**fields):
    """An Update activity's `object`, with only the keys a test names.

    Both functions read `request_json['object']` and nothing else, so the
    envelope carries no `type`, `actor` or `id`: adding them would suggest
    those are read, and they are not.
    """
    return {'object': fields}


def test_a_reply_html_content_is_wrapped_and_converted(app, db_session, redis_lock_only_double):
    """`update_post_reply_from_activity`'s content arm wraps bare content in
    `<p>` before allowlisting, then derives `body` from the html because no
    `source` was supplied.

    The seeded body is 'before', which the document does not contain, so
    "the document was applied" is distinguishable from "the seed survived".
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(content='hello there'))

    assert reply.body_html == '<p>hello there</p>'
    assert reply.body == 'hello there'


def test_a_reply_already_wrapped_content_is_not_double_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<p>')` half of the wrap guard. Its sibling half is
    `startswith('<blockquote>')`, covered by the next test -- two disjuncts,
    two tests, so dropping either one is attributable.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(content='<p>hello</p>'))

    assert reply.body_html == '<p>hello</p>'


def test_a_reply_blockquote_content_is_not_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<blockquote>')` disjunct. Without this test that
    disjunct can be deleted with the suite green, since the `<p>` case is
    caught by its own.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(content='<blockquote>q</blockquote>'))

    assert reply.body_html.startswith('<blockquote>')


def test_a_reply_markdown_source_overwrites_the_html_derived_body(app, db_session, redis_lock_only_double):
    """`source` with `mediaType: text/markdown` wins: `body` becomes the
    markdown and `body_html` is re-derived from it, overwriting the value the
    html arm just computed.

    The html and the markdown are deliberately DIFFERENT strings, so the
    assertion distinguishes which arm won. If both said the same thing the
    test would pass either way.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/markdown', 'content': 'from markdown'},
    ))

    assert reply.body == 'from markdown'
    assert 'from markdown' in reply.body_html
    assert 'from html' not in reply.body_html


def test_a_reply_source_that_is_not_markdown_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `mediaType == 'text/markdown'` conjunct's False side. A `source`
    that is a dict and has a mediaType, but the wrong one, must fall to the
    `else` and keep the html-derived body.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/plain', 'content': 'from markdown'},
    ))

    assert reply.body == 'from html'


def test_a_reply_source_that_is_not_a_dict_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The guard's normal skip path for a string `source`: it falls to the
    `else` and keeps the html-derived body.

    This does NOT prove the `isinstance(..., dict)` conjunct is load-bearing.
    With that conjunct removed, the next check becomes `'mediaType' in
    'not a dict'`, and `in` against a string is a substring test rather than
    a membership error -- `'mediaType' in 'not a dict'` is simply `False`, so
    the guard still short-circuits to `else` and this assertion still holds
    with or without the conjunct. See
    test_a_reply_none_source_does_not_leak_past_the_dict_check for the
    fixture that actually kills that mutant.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source='not a dict',
    ))

    assert reply.body == 'from html'


def test_a_reply_none_source_does_not_leak_past_the_dict_check(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct, proven by a fixture the guard's
    own downstream `in` check cannot coincidentally absorb.

    A string `source` does not prove this conjunct: `'mediaType' in
    'not a dict'` is a substring test that just returns `False`, so the guard
    short-circuits to the same place whether or not `isinstance` is checked
    first. `None` does not have that escape hatch -- `'mediaType' in None`
    raises `TypeError` rather than returning `False` -- so with the
    `isinstance` conjunct removed, this input crashes instead of quietly
    reproducing the guarded behaviour. With the conjunct in place (the
    production code, unmutated), `isinstance(None, dict)` is simply `False`
    and the guard short-circuits normally, same as the string case: this
    assertion holds either way when the guard is intact, and only the mutant
    diverges.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source=None,
    ))

    assert reply.body == 'from html'


def test_a_reply_source_missing_media_type_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `'mediaType' in ...` conjunct. A `source` that is a dict and
    carries `content` but no `mediaType` key must fall to the `else` and keep
    the html-derived body, rather than reaching the `['mediaType']`
    subscript that a peer omitting the key would otherwise crash on.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>from html</p>',
        source={'content': 'from markdown'},
    ))

    assert reply.body == 'from html'


def test_a_reply_language_is_applied(app, db_session, redis_lock_only_double):
    """`find_language_or_create` is called with the document's identifier and
    name, and the returned row's id lands on the reply.

    Both English and German are seeded and COMMITTED before the call, so
    German already has a real, flushed id when `find_language_or_create` finds
    it -- the app factory sets `autoflush=False` (see this module's
    docstring), so a language created fresh inside the function under test
    would still have `id is None` at the moment `reply.language_id = language.id`
    reads it, and the assignment would silently write NULL. Pre-seeding avoids
    exercising that unrelated flush-timing quirk and isolates the guard this
    test is about.

    The reply is seeded with a real, non-NULL `language_id` (English) first --
    not left at the factory's default `None` -- so "applied" is distinguishable
    from "was already non-NULL": the assertion checks the id actually became
    the German row's id, not merely that it changed from NULL to something.
    """
    reply = _seed_reply()
    english = Language(code='en', name='English')
    german = Language(code='de', name='German')
    db.session.add(english)
    db.session.add(german)
    db.session.commit()
    reply.language_id = english.id
    db.session.commit()
    seeded = reply.language_id

    update_post_reply_from_activity(reply, _update(
        language={'identifier': 'de', 'name': 'German'},
    ))

    assert reply.language_id == german.id
    assert reply.language_id != seeded


def test_a_reply_language_that_is_not_a_dict_is_ignored(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct. A peer sending `language` as a
    bare string must not reach the `['identifier']` subscript behind it.

    The reply is seeded with a real, non-NULL `language_id` (English) first,
    so "the guard skipped the arm" is distinguishable from "the arm never had
    anything to write" -- asserting equality against a seeded `None` would
    hold whether or not the guard fired.
    """
    reply = _seed_reply()
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    reply.language_id = english.id
    db.session.commit()
    seeded = reply.language_id

    update_post_reply_from_activity(reply, _update(language='de'))

    assert reply.language_id == seeded


def test_a_reply_distinguished_flag_is_applied(app, db_session, redis_lock_only_double):
    """`distinguished` is copied verbatim. Seeded False first -- the column's
    own default -- so the document's True is the only thing that could have
    set it.
    """
    reply = _seed_reply()
    reply.distinguished = False
    db.session.commit()

    update_post_reply_from_activity(reply, _update(distinguished=True))

    assert reply.distinguished is True


def test_a_reply_distinguished_flag_is_applied_when_false(app, db_session, redis_lock_only_double):
    """The contrary seed. Without this sibling, the gate could be replaced by
    `reply.distinguished = True` and the suite would stay green.
    """
    reply = _seed_reply()
    reply.distinguished = True
    db.session.commit()

    update_post_reply_from_activity(reply, _update(distinguished=False))

    assert reply.distinguished is False


def test_a_reply_replies_enabled_flag_is_applied(app, db_session, redis_lock_only_double):
    """`repliesEnabled` -> `replies_enabled`, the one renamed key in this
    function. Seeded to the opposite of what the document sends.
    """
    reply = _seed_reply()
    reply.replies_enabled = True
    db.session.commit()

    update_post_reply_from_activity(reply, _update(repliesEnabled=False))

    assert reply.replies_enabled is False


def test_a_reply_updated_timestamp_is_parsed(app, db_session, redis_lock_only_double):
    """`ap_updated` comes from the document's `updated` when it parses.
    Asserting the parsed VALUE, not merely that it is set -- `utcnow()` is
    what the fallback would give, so "is not None" would pass either way.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='x', updated='2020-01-02T03:04:05+00:00',
    ))

    assert reply.ap_updated.year == 2020
    assert reply.ap_updated.month == 1


def test_a_reply_unparseable_updated_falls_back_to_now(app, db_session, redis_lock_only_double):
    """The `except ValueError` arm. `datetime.fromisoformat` raises on a
    string it cannot read, and the handler substitutes `utcnow()`.

    Asserting the year is the CURRENT year rather than 2020 is what
    distinguishes the fallback from the parse.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='x', updated='not a timestamp',
    ))

    assert reply.ap_updated.year == utcnow().year
