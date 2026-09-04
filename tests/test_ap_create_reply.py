"""`create_post_reply` and `notify_about_post_reply` -- the create path's reply
half, and the mirror of the `update_post_reply_from_activity` that
sub-project 14 covered in full.

Entry is a direct call. `create_post_reply` takes an already-resolved
`community` and `user` plus the raw document, and resolves its parent from the
`in_reply_to` URI through `find_reply_parent`, which branches on whether the
string contains 'comment' or 'post'.

Both functions reach `redis_client.lock` -- `notify_about_post_reply` opens it
directly, `create_post_reply` through `PostReply.new`. The shared
`redis_double` fixture cannot serve a lock: fakeredis without lupa has no Lua
scripting and redis-py's `Lock.release()` issues an EVALSHA. Every test here
takes `redis_lock_only_double`, whose `.lock()` is a nullcontext.

`log_incoming_ap` writes an `ActivityPubLog` row only when
`LOG_ACTIVITYPUB_TO_DB` is true, and config.py defaults it to False. The head
guards are otherwise indistinguishable from each other -- each returns None
and creates nothing -- so the tests that pin them turn it on and assert the
message, which differs per guard.
"""
import contextlib

import pytest

from app import db
from app.activitypub.util import create_post_reply, notify_about_post_reply
from app.constants import NOTIF_MENTION, NOTIF_POST, NOTIF_REPLY
from app.models import ActivityPubLog, Language, Notification, PostReply, User
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_instance_block,
                             make_post, make_post_reply, make_site, make_user,
                             make_user_block)

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


@pytest.fixture
def ap_log(app):
    """Turn on the ActivityPubLog write so a head guard is attributable.

    Without this, all five head guards return None and create nothing, which
    makes them indistinguishable from each other and from a guard that was
    deleted. With it, each writes its own message.

    `app` is session-scoped (tests/conftest.py), so this mutation of
    `app.config` leaks to every later test in the process unless restored --
    the `yield` / reset below is load-bearing, not decorative.
    """
    app.config['LOG_ACTIVITYPUB_TO_DB'] = True
    yield
    app.config['LOG_ACTIVITYPUB_TO_DB'] = False


def _seed_scenario(local_only=False):
    """A local community owned by user 1, a remote author, a remote replier,
    and one Post to reply to.

    `make_community` hardcodes `instance_id=1` and `user_id=1`, so an instance
    and a user are seeded first to occupy those ids -- the pattern
    tests/test_inbox_dispatch_votes.py documents.

    The post's `ap_id` deliberately contains 'post', because `find_reply_parent`
    branches on that substring being present in `in_reply_to`.
    """
    make_site()
    instance = make_instance(PEER)
    make_user(instance, 'community_owner')
    community = make_community(host=PEER)
    community.ap_fetched_at = utcnow()
    community.local_only = local_only
    author = make_user(instance, 'author')
    replier = make_user(instance, 'replier')
    post = make_post(community, author, ap_id=f'https://{PEER}/post/1')
    db.session.commit()
    return community, post, replier


def _reply_doc(**fields):
    """A Create activity's envelope plus its `object`.

    `create_post_reply` reads `request_json['id']` for the log and
    `request_json['object']` for everything else, so both levels matter here --
    unlike sub-project 14's helper, whose functions read only the object.

    Defaults `to` to Public. `create_post_reply`'s visibility guard runs
    BEFORE parent resolution, the archived check and the block check, and
    `activitypub_visibility` classifies an object with no addressing at all as
    'direct' (confirmed by reading the function, and by
    tests/test_ap_create_resolved_object.py's `TestTheHelpersReturningFalsy`).
    A brief draft of this helper omitted `to` -- every test aimed at a LATER
    guard would have been refused by the visibility guard first instead,
    passing for the wrong reason. Tests that want to reach the visibility
    guard itself override `to`/`cc` via `**fields`.
    """
    obj = {'id': f'https://{PEER}/comment/1', 'type': 'Note',
           'to': ['https://www.w3.org/ns/activitystreams#Public'], 'cc': []}
    obj.update(fields)
    return {'id': f'https://{PEER}/activities/create/1',
            'type': 'Create',
            'object': obj}


def _create(community, post, replier, document=None, in_reply_to=None):
    """Call the function under test with the arguments its callers pass.

    `store_ap_json=True` so the log row carries the document, which is what
    makes a head-guard assertion able to name the document that provoked it.
    """
    return create_post_reply(
        store_ap_json=True,
        community=community,
        in_reply_to=in_reply_to if in_reply_to is not None else post.ap_id,
        request_json=document if document is not None else _reply_doc(content='hello'),
        user=replier,
    )


def test_a_local_only_community_discards_the_reply(app, db_session, redis_lock_only_double, ap_log):
    """The first guard. A local-only community takes no federated replies.

    Asserts the log row's message as well as the None return, because all five
    head guards return None and create nothing -- the message is the only thing
    that says WHICH guard fired.
    """
    community, post, replier = _seed_scenario(local_only=True)

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'failure'
    assert 'local only' in log.exception_message


@pytest.mark.parametrize('visibility_field,expected', [
    ({'to': [f'https://{PEER}/u/someone/followers'], 'cc': []}, 'followers'),
    ({'to': [], 'cc': []}, 'direct'),
])
def test_a_non_public_reply_is_refused(app, db_session, redis_lock_only_double, ap_log,
                                       visibility_field, expected):
    """The visibility guard, which reads `activitypub_visibility(request_json
    .get('object'))` and refuses 'followers' and 'direct'.

    Read directly from `activitypub_visibility`
    (app/activitypub/util.py): 'public' requires AS_PUBLIC in `to`; failing
    that, 'unlisted' requires AS_PUBLIC in `cc`; failing that, 'followers'
    requires some address in `to + cc` ending `/followers`; anything else --
    including no addressing at all -- is 'direct'. The brief's own starting
    case, `{'to': [], 'cc': []}` expecting 'followers', is wrong: empty `to`
    and `cc` fall through every branch to 'direct', confirmed both by reading
    the function and by tests/test_ap_create_resolved_object.py's
    `TestTheHelpersReturningFalsy`, which already pins exactly this ("a
    document with neither 'to' nor 'cc'" -> 'direct') on the sibling
    `create_post`/`resolved()` path. The 'followers' case here instead
    addresses a URI ending `/followers`, which is what the function actually
    keys on.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello', **visibility_field)

    result = _create(community, post, replier, document=document)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.result == 'ignored'
    assert expected in log.exception_message


def test_an_unresolvable_parent_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """`find_reply_parent` returns three Nones for a URI matching neither
    'comment' nor 'post' and resolving via neither `PostReply.get_by_ap_id`
    nor `Post.get_by_ap_id`. With `post_id`, `parent_comment_id` and `root_id`
    all falsy, `create_post_reply`'s `if post_id or parent_comment_id or
    root_id:` is False and control falls to that `if`'s own `else:`, logging
    'Unable to find parent post/comment' -- not the inner `if post_id is
    None:` guard nested inside the `if` body (that guard is reached only when
    ONE of the three is truthy but `post_id` specifically is not, e.g. a
    resolved comment with no post -- out of this test's scope, since the seed
    always resolves to a post).

    Asserts the message EXACTLY, not by substring. 'Could not find parent
    post' (the inner, out-of-scope guard's message) also contains the
    substring 'parent post', so a substring assertion here would still pass
    if the outer `if post_id or parent_comment_id or root_id:` were mutated
    to `if True:` -- that mutant falls through to the inner `if post_id is
    None:` guard and produces a different message that happens to share the
    same substring, surviving a substring check. Confirmed by mutation: with
    that condition forced `if True:`, this test still passed under a
    substring assertion. The exact-match assertion below kills it.
    """
    community, post, replier = _seed_scenario()

    result = _create(community, post, replier, in_reply_to=f'https://{PEER}/nothing/1')

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Unable to find parent post/comment'


def test_a_reply_to_an_archived_post_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """`post.archived` gates on truthiness (`if post.archived:`).

    Read from app/models.py: `Post.archived` is a `db.Column(db.String(100))`
    with no `default=` kwarg -- it stores a path/URL to where the post's
    content was archived off to (app/utils.py sets it to an S3 URL or a
    gzipped file path), not a boolean flag, and its column default is
    therefore `None`, not `False`. Seeded here with a non-empty string rather
    than `True`, both because that is what the column actually holds in
    production and to avoid relying on implicit bool-to-varchar coercion.
    """
    community, post, replier = _seed_scenario()
    post.archived = 'https://s3.example/archived/1.json.gz'
    db.session.commit()

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert 'archived' in log.exception_message


def test_a_reply_from_a_blocked_user_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """`post.author.has_blocked_user(user.id)`.

    Read `has_blocked_user` (app/models.py): `self` is the blocker,
    `user_id` is the argument checked as blocked --
    `UserBlock.filter_by(blocker_id=self.id, blocked_id=user_id)`. Read
    `make_user_block(blocker, blocked)` (tests/factories.py): it sets
    `UserBlock(blocker_id=blocker.id, blocked_id=blocked.id)` -- the same
    direction the method reads, not reversed. So the post's author blocking
    the replier is `make_user_block(post.author, replier)`.

    Asserts the message EXACTLY, not by substring. `PostReply.new`
    (app/models.py:3030-3031) independently re-checks
    `notification_target.author.has_blocked_user(reply.user_id)` -- the SAME
    author, SAME user -- and raises `PostReplyValidationError('Replier
    blocked')`, which `create_post_reply`'s `except Exception` swallows into
    a log row with THAT message. Confirmed by mutation: with this file's own
    guard (`post.author.has_blocked_user(...) or ...`, line ~2628) forced
    `False`, `result is None` and `PostReply.query.count() == 0` both still
    held -- this duplicate downstream check produced the same externally
    observable outcome for a different reason, and a bare `'blocked' in
    ...` substring assertion (both messages contain it) did not tell the two
    apart. The exact-match assertion below does.
    """
    community, post, replier = _seed_scenario()
    make_user_block(post.author, replier)

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Post author blocked replier'


def test_bare_content_is_wrapped_and_allowlisted(app, db_session, redis_lock_only_double):
    """Content that starts with neither `<p>` nor `<blockquote>` is wrapped
    before allowlisting, and `body` is derived from the html because no
    `source` was supplied.
    """
    community, post, replier = _seed_scenario()

    reply = _create(community, post, replier, document=_reply_doc(content='hello there'))

    assert reply.body_html == '<p>hello there</p>'
    assert reply.body == 'hello there'


def test_already_wrapped_content_is_not_double_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<p>')` disjunct of the wrap guard."""
    community, post, replier = _seed_scenario()

    reply = _create(community, post, replier, document=_reply_doc(content='<p>hello</p>'))

    assert reply.body_html == '<p>hello</p>'


def test_blockquote_content_is_not_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<blockquote>')` disjunct, which needs its own test or
    it can be deleted with the `<p>` case still passing.
    """
    community, post, replier = _seed_scenario()

    reply = _create(community, post, replier,
                    document=_reply_doc(content='<blockquote>q</blockquote>'))

    assert reply.body_html.startswith('<blockquote>')


def test_a_markdown_source_overwrites_the_html_derived_body(app, db_session, redis_lock_only_double):
    """`source` with `mediaType: text/markdown` wins, overwriting the body the
    html arm computed. The two strings differ deliberately, or the assertion
    could not tell which arm won.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>',
                          source={'mediaType': 'text/markdown', 'content': 'from markdown'})

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from markdown'
    assert 'from markdown' in reply.body_html
    assert 'from html' not in reply.body_html


def test_a_source_with_no_media_type_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `'mediaType' in ...` conjunct, which THIS function has and its
    update-path twin lacked until sub-project 14 added it.

    A dict `source` carrying only `content` must fall to the `else`.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>', source={'content': 'from markdown'})

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from html'


def test_a_source_that_is_not_a_dict_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` conjunct. Choose the fixture value with
    care: `in` against a string is a SUBSTRING test, so a string `source` lets
    the next conjunct return False rather than raise, and the guard
    short-circuits identically with or without `isinstance`. Use a value on
    which `'mediaType' in ...` raises or succeeds-then-fails.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>', source=None)

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from html'


def test_a_source_with_a_non_markdown_media_type_leaves_the_html_body(app, db_session, redis_lock_only_double):
    """The `== 'text/markdown'` equality conjunct. `source` is a dict and DOES
    carry a `mediaType` key here, unlike `test_a_source_with_no_media_type_
    leaves_the_html_body` above -- that test's `source` has no `mediaType` key
    at all, so it cannot reach this conjunct (the guard short-circuits one
    conjunct earlier); only a `mediaType` that is present but wrong reaches
    the `==` comparison itself.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>from html</p>',
                          source={'mediaType': 'text/html', 'content': 'from markdown'})

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'from html'


def _make_language(code, name):
    """Seed and commit a `Language` row directly, bypassing
    `find_language_or_create`.

    `find_language_or_create` (app/activitypub/util.py) reads:

        new_language = Language(code=code, name=name)
        if session:
            session.add(new_language)
        else:
            db.session.add(new_language)
        return new_language

    -- no `flush()` on either path, and `app/__init__.py` constructs
    `db = SQLAlchemy(session_options={"autoflush": False}, ...)`, so a
    `Language` created through the "not found" branch has `.id is None` at
    the moment `create_post_reply` reads `language.id` for
    `PostReply.language_id`. Every language test below therefore pre-seeds
    and commits the `Language` row itself, so `find_language_or_create`'s
    "already exists" branch (`existing_language = Language.query.filter(
    Language.code == code).first()`, returned directly) runs instead --
    that row has a real, committed id. This is a workaround for a defect
    Task 9 fixes, not ordinary setup: left to itself, the create branch
    would hand back an unflushed, id-less `Language`, and
    `PostReply.language_id` would be set to `None` regardless of which
    `code`/`name` the document carried.
    """
    language = Language(code=code, name=name)
    db.session.add(language)
    db.session.commit()
    return language


def test_a_language_dict_is_applied(app, db_session, redis_lock_only_double):
    """The `if 'language' in request_json['object'] and isinstance(
    request_json['object']['language'], dict):` arm, taking `find_language_or
    _create`'s "already exists" branch (see `_make_language`'s docstring).
    """
    community, post, replier = _seed_scenario()
    spanish = _make_language('es', 'Spanish')
    document = _reply_doc(content='hello',
                          language={'identifier': 'es', 'name': 'Spanish'})

    reply = _create(community, post, replier, document=document)

    assert reply.language_id == spanish.id


def test_a_non_dict_language_is_ignored_in_favour_of_content_map(app, db_session, redis_lock_only_double):
    """The `isinstance(request_json['object']['language'], dict)` conjunct.

    `language` is a plain string here, so the `if` arm's `isinstance` conjunct
    is False and control falls to the `elif 'contentMap' in ... and isinstance
    (..., dict):` arm. Two different `Language` rows are seeded so the result
    is only explained by the `elif` firing on `contentMap`, not by the `if`
    arm having fired on `language` after all (which would raise, since a
    string does not support `['identifier']` the way a dict does -- indexing
    a string by a non-integer key raises `TypeError`).
    """
    community, post, replier = _seed_scenario()
    _make_language('es', 'Spanish')
    german = _make_language('de', 'German')
    document = _reply_doc(content='hello', language='not-a-dict',
                          contentMap={'de': 'hallo'})

    reply = _create(community, post, replier, document=document)

    assert reply.language_id == german.id


def test_a_non_dict_content_map_is_ignored_in_favour_of_site_language_id(app, db_session, redis_lock_only_double):
    """The `isinstance(request_json['object']['contentMap'], dict)` conjunct
    -- the `elif` arm's mirror of the `if` arm's isinstance check above.

    `contentMap` is a plain string here, so `isinstance(..., dict)` is False
    and control falls to the `else`. Seeding a decoy Spanish `Language`
    alongside the English one seeds a contrary baseline for the same reason
    `test_neither_language_nor_content_map_falls_to_site_language_id` does.
    """
    community, post, replier = _seed_scenario()
    _make_language('es', 'Spanish')
    english = _make_language('en', 'English')
    document = _reply_doc(content='hello', contentMap='not-a-dict')

    reply = _create(community, post, replier, document=document)

    assert reply.language_id == english.id


def test_content_map_supplies_the_language_when_language_is_absent(app, db_session, redis_lock_only_double):
    """The `elif 'contentMap' in request_json['object'] and isinstance(
    request_json['object']['contentMap'], dict):` arm, with no `language` key
    at all.

    `find_language` (app/activitypub/util.py) only looks up --
    `Language.query.filter(Language.code == code).first()`, returning `None`
    on a miss -- so the `Language` row named by `contentMap`'s first key must
    already exist in the test database, unlike `find_language_or_create`.
    """
    community, post, replier = _seed_scenario()
    italian = _make_language('it', 'Italian')
    document = _reply_doc(content='hello', contentMap={'it': 'ciao'})

    reply = _create(community, post, replier, document=document)

    assert reply.language_id == italian.id


def test_a_language_dict_wins_over_a_present_content_map(app, db_session, redis_lock_only_double):
    """The `if`/`elif` ordering: when both `language` and `contentMap` are
    present, the `if` arm's `find_language_or_create` runs and the `elif`
    arm's `find_language` does not. The two seeded languages are deliberately
    different, or the winner would not be observable.
    """
    community, post, replier = _seed_scenario()
    french = _make_language('fr', 'French')
    _make_language('de', 'German')
    document = _reply_doc(content='hello',
                          language={'identifier': 'fr', 'name': 'French'},
                          contentMap={'de': 'hallo'})

    reply = _create(community, post, replier, document=document)

    assert reply.language_id == french.id


def test_neither_language_nor_content_map_falls_to_site_language_id(app, db_session, redis_lock_only_double):
    """The `else: ... language_id = site_language_id()` arm, reached when
    neither `language` nor `contentMap` is present at all.

    `site_language_id()` (app/utils.py) checks an explicit `site` argument
    (not passed here), then `g.site.language_id` (there is no request cycle
    here, so `g` carries no `site` -- `tests/conftest.py`'s `db_session`
    fixture clears `g.__dict__` before every test and nothing in this module
    populates `g.site`), and only then falls to its own `else`:
    `Language.query.filter(Language.code == 'en').first()`, returning that
    row's id. Seeding a Spanish decoy alongside the English row seeds a
    contrary baseline: if the `else` arm were skipped and `language_id` were
    left at whatever a broken guard produced, the assertion below would not
    coincidentally pass by both sides being the same non-English row or both
    being `None`.
    """
    community, post, replier = _seed_scenario()
    _make_language('es', 'Spanish')
    english = _make_language('en', 'English')
    document = _reply_doc(content='hello')

    reply = _create(community, post, replier, document=document)

    assert reply.language_id == english.id


def test_a_reply_from_a_blocked_instance_is_refused(app, db_session, redis_lock_only_double, ap_log):
    """The `or has_blocked_instance(...)` half of the same guard.

    `post.author.has_blocked_instance(user.instance_id)` --
    `InstanceBlock.filter_by(user_id=self.id, instance_id=instance_id)`.
    `make_instance_block(user, instance)` (tests/factories.py) sets
    `InstanceBlock(user_id=user.id, instance_id=instance.id)`, so the post's
    author blocking the replier's instance is
    `make_instance_block(post.author, replier.instance)`. Needed because the
    guard's `or` has two operands and a test that only ever blocks the USER
    cannot prove the INSTANCE half still matters -- and, unlike the user half,
    `PostReply.new` has no downstream instance-block check to independently
    reproduce the same outcome, so this one does not need the same
    exact-match defence to be a real kill (kept exact anyway, for symmetry
    with the test above and because both fire the same log message).
    """
    community, post, replier = _seed_scenario()
    make_instance_block(post.author, replier.instance)

    result = _create(community, post, replier)

    assert result is None
    assert PostReply.query.count() == 0
    log = ActivityPubLog.query.one()
    assert log.exception_message == 'Post author blocked replier'
