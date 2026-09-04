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
from app.constants import MICROBLOG_APPS, NOTIF_MENTION, NOTIF_POST, NOTIF_REPLY
from app.models import ActivityPubLog, Language, Notification, PostReply, User, UserFlair
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_instance_block,
                             make_notification_subscription, make_post, make_post_reply,
                             make_site, make_user, make_user_block)

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


# --- attachment loop ---------------------------------------------------
#
# Structurally identical to the loop sub-project 14 covered in
# `update_post_reply_from_activity`, its update-path twin -- confirmed by
# reading both: same `isinstance(dict)` / `isinstance(list)` normalisation,
# the same `href`-then-`url` precedence, the same `name`-as-alt-text and
# `if url:` gates, and the same `if attachment_list:` regeneration gate.
#
# One difference matters here and changes nothing about what these tests
# assert, only how they get there: in the twin, `body` is a column read back
# off the row before the loop runs. Here it is a local the content arm just
# built (`body = body_html = ''`, then the `'content' in ...` arm sets it),
# and the loop mutates that local before `PostReply.new(..., body=body, ...)`
# ever persists it. Every assertion below still reads the persisted
# `PostReply`, not the local -- there is no other way to observe it.


def test_a_single_attachment_dict_is_appended(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` arm: a lone attachment object is wrapped
    into a one-element list rather than iterated as a dict's keys (which
    would loop over the strings `'url'`, not the attachment itself).

    Exact equality: `content='hello'` makes the content arm's `body`
    predictable ('hello'), so the appended markdown can be pinned precisely.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment={'url': 'https://cdn.example/a.png'})

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'hello\n\n![](https://cdn.example/a.png)'


def test_an_attachment_list_is_appended_in_order(app, db_session, redis_lock_only_double):
    """The `isinstance(..., list)` arm, with two entries so the loop runs
    more than once and order is observable.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment=[{'url': 'https://cdn.example/1.png'},
                                      {'url': 'https://cdn.example/2.png'}])

    reply = _create(community, post, replier, document=document)

    assert reply.body == ('hello\n\n![](https://cdn.example/1.png)'
                          '\n\n![](https://cdn.example/2.png)')


def test_attachment_url_wins_over_href(app, db_session, redis_lock_only_double):
    """Both keys are read and `url` is read second -- `if 'href' in
    attachment: url = attachment['href']` then `if 'url' in attachment: url =
    attachment['url']`, confirmed against the current source -- so the two
    values differ here, which is what makes the precedence observable; equal
    values would pass whichever won.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment=[{'href': 'https://cdn.example/href.png',
                                      'url': 'https://cdn.example/url.png'}])

    reply = _create(community, post, replier, document=document)

    assert 'url.png' in reply.body
    assert 'href.png' not in reply.body


def test_attachment_href_is_used_when_there_is_no_url(app, db_session, redis_lock_only_double):
    """The `href` half on its own. Without this test the `'href' in
    attachment` conjunct can be deleted with the suite green, because the
    precedence test above supplies both keys and would still pass (`url`
    would simply stay unset by a route that never reads `href` at all).
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment=[{'href': 'https://cdn.example/href.png'}])

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'hello\n\n![](https://cdn.example/href.png)'


def test_an_attachment_with_neither_href_nor_url_contributes_nothing(app, db_session, redis_lock_only_double):
    """The `if url:` gate's normal (false) side. An attachment carrying
    neither `href` nor `url` leaves `url` at its initial `''`, so nothing is
    appended.

    Asserted by exact equality against what the content arm alone produced,
    so a stray `![]()` -- `url` falsy but the append happening anyway --
    would fail this test rather than pass unnoticed.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment=[{'mediaType': 'image/png'}])

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'hello'


def test_attachment_name_supplies_alt_text(app, db_session, redis_lock_only_double):
    """The `'name' in attachment` gate: `name` becomes the markdown alt text,
    not folded into the url or dropped.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment=[{'url': 'https://cdn.example/a.png',
                                      'name': 'alt words'}])

    reply = _create(community, post, replier, document=document)

    assert reply.body == 'hello\n\n![alt words](https://cdn.example/a.png)'


def test_an_empty_attachment_list_does_not_regenerate_the_html(app, db_session, redis_lock_only_double):
    """`if attachment_list:` guards the `body_html` regeneration. With an
    empty list, `body_html` must remain exactly what the content arm
    allowlisted -- `markdown_to_html(body)` must not run.

    Seeded through the already-wrapped html arm (`<p>hello</p>`) rather than
    the bare-content arm, so the pre- and post-regeneration spellings would
    differ if regeneration ran, and the assertion can tell them apart.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='<p>hello</p>', attachment=[])

    reply = _create(community, post, replier, document=document)

    assert reply.body_html == '<p>hello</p>'


def test_a_non_empty_attachment_list_regenerates_body_html(app, db_session, redis_lock_only_double):
    """The `if attachment_list:` gate's true side. A non-empty list must
    cause `body_html` to be re-derived (via `markdown_to_html`) from the
    attachment-appended `body`, not merely leave `body` updated while
    `body_html` still reflects only the content arm's `allowlist_html` pass.

    Every other test in this block asserts only on `body`, which
    `html_to_text` would render identically whether or not
    `body_html = markdown_to_html(body)` ever ran -- this is the test
    sub-project 14's brief found missing from its own six, in the twin.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          attachment=[{'url': 'https://cdn.example/a.png',
                                      'name': 'alt words'}])

    reply = _create(community, post, replier, document=document)

    assert '<img' in reply.body_html
    assert 'https://cdn.example/a.png' in reply.body_html


# --- Mention collection --------------------------------------------------
#
# Two-phase here, unlike `update_post_reply_from_activity` (sub-project 14's
# twin): the tag scan (app/activitypub/util.py, right after the attachment
# loop) only APPENDS matching profile ids to a local, `local_users_to_notify`;
# the actual `Notification` rows are created later, in the `for lutn in
# local_users_to_notify:` loop nested inside `PostReply.new`'s try block.
# `local_users_to_notify` is a local, so every assertion below reads the
# resulting `Notification` rows, never the list.


def _seed_local_recipient(name='localuser'):
    """A local user the Mention block can resolve.

    The lookup is `filter_by(ap_profile_id=..., ap_id=None)`, so both columns
    matter. `make_user(None, name, local=True)` leaves `ap_id` None and
    `ap_profile_id` None; this helper sets the profile id to the lowercased
    form the document will send.
    """
    recipient = make_user(None, name, local=True)
    recipient.ap_profile_id = f'https://test.piefed.local/u/{name}'
    db.session.commit()
    return recipient


def _mention(name='localuser'):
    return {'type': 'Mention', 'href': f'https://test.piefed.local/u/{name}'}


def _use_a_non_microblog_instance(replier):
    """Steers `post_reply.instance.software` off `_seed_scenario`'s default.

    `make_instance` (tests/factories.py) defaults `software='mastodon'`,
    which IS in `MICROBLOG_APPS` (app/constants.py) -- so by default every
    reply built by `_seed_scenario` trips `create_post_reply`'s mbin/
    microblog mirroring branch (app/activitypub/util.py, inside the `for
    lutn in local_users_to_notify:` loop) the moment any Mention is actually
    collected. That branch's last query -- `db.session.execute(text('SELECT
    user_id FROM "post_reply" WHERE id IN :ids'), {'ids': ids})` -- binds
    `ids` as a Python tuple; every reply built by a test that calls this
    helper is a top-level reply (`post_reply.path == [0, post_reply.id]`), so
    for those the tuple is always empty -- see the de-duplication section at
    the end of this module for how `path` is built, and for the tests that
    seed a deeper chain instead so that the tuple is not empty. On an empty
    tuple psycopg2 raises a syntax error on `IN ()` -- confirmed
    directly against this suite's own Postgres container:
    `cur.execute('SELECT 1 WHERE 1 IN %s', ((),))` raises `syntax error at
    or near ")"`. That raise happens inside `create_post_reply`'s own
    `try`/`except Exception`, so it is swallowed into a `None` return --
    silently discarding the very `Notification` a test needs to observe,
    for a reason that has nothing to do with whatever guard the test is
    actually aimed at.

    Fixing that defect is out of scope for a test-only task; every test
    below that needs a REAL Notification to be created routes around it by
    calling this first, the same way `_seed_scenario`'s own docstring routes
    around `make_community`'s hardcoded ids.
    """
    replier.instance.software = 'lemmy'
    db.session.commit()


def test_a_mention_of_a_local_user_produces_a_notification(app, db_session, redis_lock_only_double):
    """The Mention block's normal job, start to finish: collected into
    `local_users_to_notify` during the tag scan, then turned into a
    `Notification` row in the loop after `PostReply.new`.

    A second, typeless tag (`{'name': 'decoy'}`) rides along in the list --
    needed only to get the list past `len(request_json['object']['tag']) >
    1` (see `test_the_tag_length_gate_skips_a_lone_mention` below), and, as a
    side effect, it also kills a mutant that drops the `'type' in json_tag`
    conjunct: with that conjunct forced true, `json_tag['type']` would raise
    `KeyError` on this decoy (it carries no `'type'` key at all), and that
    raise sits outside `create_post_reply`'s `try`/`except`, so it would
    surface as an uncaught exception here instead of a clean `reply`.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    recipient = _seed_local_recipient('localuser')
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    notification = Notification.query.filter_by(user_id=recipient.id).one()
    assert notification.notif_type == NOTIF_MENTION
    assert notification.subtype == 'comment_mention'
    assert notification.author_id == replier.id
    refreshed = User.query.get(recipient.id)
    assert refreshed.unread_notifications == 1


def test_the_tag_length_gate_skips_a_lone_mention(app, db_session, redis_lock_only_double):
    """`len(request_json['object']['tag']) > 1`. Pinning CURRENT behaviour,
    not endorsing it: read directly off `create_post_reply`
    (app/activitypub/util.py), a genuine single `Mention` -- the ordinary
    "@user, thanks" case -- is silently dropped and notifies no one, purely
    because the tag list has exactly one entry. Nothing here argues that is
    the right behaviour; it only pins what the code does today.

    Needs `_use_a_non_microblog_instance`: with this conjunct forced true, a
    lone, otherwise-valid Mention WOULD reach the notify loop and create a
    real Notification, and only steering the instance off 'mastodon' lets
    that difference surface instead of being masked by the unrelated
    microblog-mirror defect (see that helper's docstring) -- which also ends
    in zero Notifications, for a reason unrelated to this guard.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    _seed_local_recipient('localuser')
    document = _reply_doc(content='hello', tag=[_mention('localuser')])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_mention_of_a_remote_user_produces_no_notification(app, db_session, redis_lock_only_double):
    """The `profile_id.startswith('https://' + SERVER_NAME)` guard's
    ordinary job: a foreign-host href is never collected.

    Kept for what it proves, NOT for killing the `startswith` guard itself --
    a remote host cannot do that (see
    `test_a_same_host_case_mismatched_href_produces_no_notification`'s
    docstring): no local user's `ap_profile_id` can ever equal a
    'peer.example' href, so whether this guard runs or is deleted looks
    identical from here -- both leave `local_users_to_notify` empty (correct
    code, because the guard rejects it) or non-empty but pointing at a
    profile id no local `User` row matches (a deleted guard), and either way
    the notify loop's `if not recipient: continue` produces zero
    Notifications.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          tag=[{'type': 'Mention', 'href': f'https://{PEER}/u/someone'},
                              {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_same_host_case_mismatched_href_produces_no_notification(app, db_session, redis_lock_only_double):
    """Kills the `.startswith('https://' + current_app.config['SERVER_NAME'])`
    conjunct -- the trap sub-project 14 hit on its own twin guard. This href
    names the right host with the wrong CASE; `str.startswith` is
    case-sensitive, so correctly-guarded code refuses it here, but a
    recipient is seeded that WOULD match it once `.lower()`'d -- so a guard
    forced to always pass would find that recipient and create a real
    Notification, which the assertion below would catch.

    Needs `_use_a_non_microblog_instance` for the same reason as the length
    gate test above: without it, a guard forced to always pass would still
    end in zero Notifications via the unrelated microblog-mirror defect,
    masking the kill.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    _seed_local_recipient('localuser')
    document = _reply_doc(content='hello',
                          tag=[{'type': 'Mention',
                               'href': 'https://TEST.PIEFED.LOCAL/u/localuser'},
                              {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_mention_with_no_href_produces_no_notification(app, db_session, redis_lock_only_double):
    """`profile_id = json_tag['href'] if 'href' in json_tag else None`, then
    the `if profile_id and isinstance(profile_id, str) and profile_id.
    startswith(...)` guard's truthiness half on that `None`.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          tag=[{'type': 'Mention'}, {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_non_string_href_produces_no_notification(app, db_session, redis_lock_only_double):
    """The `isinstance(profile_id, str)` conjunct. A non-string, truthy
    `href` -- `.startswith` would raise `AttributeError` on a bare `int`, so
    a passing test here also confirms `isinstance` runs before `.startswith`,
    not merely that the two conjuncts agree.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          tag=[{'type': 'Mention', 'href': 12345}, {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_non_mention_tag_type_is_not_treated_as_a_mention(app, db_session, redis_lock_only_double):
    """`json_tag['type'] == 'Mention'`. This tag carries a `type` key (so
    `'type' in json_tag` alone cannot explain a skip) whose value is not
    `'Mention'`, and an `href` that WOULD resolve to a seeded local recipient
    if the equality comparison were dropped -- proving this specific
    conjunct, not merely that some unrelated non-Mention tag is ignored.

    Needs `_use_a_non_microblog_instance` for the same reason as the other
    Mention-guard tests above: a dropped equality still reaches the notify
    loop, and the unrelated microblog-mirror defect would otherwise swallow
    the difference into the same zero-Notification outcome.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    _seed_local_recipient('localuser')
    document = _reply_doc(content='hello',
                          tag=[{'type': 'Hashtag',
                               'href': 'https://test.piefed.local/u/localuser'},
                              {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_non_list_tag_is_not_scanned_for_mentions(app, db_session, redis_lock_only_double):
    """`isinstance(request_json['object']['tag'], list)`. A bare dict here
    has two keys, so a dropped `isinstance` conjunct would still clear the
    following `len(...) > 1` (a 2-key dict's `len` is 2), then `for json_tag
    in request_json['object']['tag']:` would iterate the dict's KEYS as bare
    strings ('type', then 'href'). `'type' in 'type'` is a true substring
    test, so the loop would go on to evaluate `'type'['type']` -- indexing a
    string with a string -- which raises `TypeError`. That raise sits
    OUTSIDE `create_post_reply`'s `try`/`except`, so it would propagate out
    of the call uncaught rather than resolve to the plain successful reply
    asserted below.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          tag={'type': 'Mention',
                              'href': 'https://test.piefed.local/u/localuser'})

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_the_self_mention_exclusion_produces_no_notification(app, db_session, redis_lock_only_double):
    """`profile_id != reply_parent.author.ap_profile_id`. `reply_parent` is
    `parent_comment if parent_comment else post` (app/activitypub/util.py);
    every test in this module replies directly to the post, with no
    `parent_comment`, so `reply_parent` is always `post` here and the
    excluded author is the POST's author.

    The comparison is only reachable for a Mention that already cleared the
    local-host guard, so the excluded author must itself be local --
    `_seed_scenario`'s ordinary `post.author` is a remote user on `PEER` and
    can never match a 'test.piefed.local' href. `post.author` is reassigned
    to a freshly-seeded local user for this test only.

    Needs `_use_a_non_microblog_instance` for the same reason as the other
    Mention-guard tests: a dropped `!=` comparison would still append the
    profile id and reach the notify loop, and the unrelated microblog-mirror
    defect would otherwise swallow the difference into the same
    zero-Notification outcome.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    parent_author = _seed_local_recipient('postauthor')
    post.author = parent_author
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('postauthor'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


# --- Notification resolution and delivery -----------------------------------
#
# The tests above prove `local_users_to_notify` gets populated (or not).
# Everything below is the loop that CONSUMES it (app/activitypub/util.py,
# `for lutn in local_users_to_notify:`, nested inside the `try` that wraps
# `PostReply.new`): resolving each profile id to a local `User`, the
# blocked-sender check, the `Notification` row itself, the unread counter,
# and the `force_locale(get_recipient_language(...))` wrapper.
#
# Immediately after the recipient lookup sits `if post_reply.instance.
# software == 'mbin' or post_reply.instance.software in MICROBLOG_APPS:`,
# gating four Mention-suppression rules (MICROBLOG_APPS, app/constants.py:
# mastodon, misskey, akkoma, iceshrimp, pleroma, fedibird -- so every value
# in that list, plus the literal 'mbin', reaches the gated rules; anything
# else, e.g. 'lemmy' or 'piefed', does not). Those four rules, and that gate
# condition itself, are out of scope here -- Task 6's job. Every test below
# that reaches past the recipient lookup calls `_use_a_non_microblog_instance`
# so it lands on the 'lemmy' side of that gate, both to stay off Task 6's
# rules and to route around D243 (see that helper's docstring): the gated
# block's last query renders `IN ()` for a top-level reply's always-empty
# `ids`, which Postgres rejects, and this function's own tail `except
# Exception as ex: log_incoming_ap(...); return None` swallows that crash --
# producing zero Notifications for a reason that has nothing to do with
# whatever guard a test is actually aimed at.


def test_a_delivered_mention_notification_carries_the_expected_fields(app, db_session, redis_lock_only_double):
    """The `Notification(...)` construction itself (app/activitypub/util.py,
    inside `for lutn in local_users_to_notify:`), checked field by field:
    `user_id` is the RESOLVED recipient (not the replier, not the mentioned
    post's author), `notif_type` and `subtype` are the Mention-specific
    constants, and `url` is built from the new reply's own id.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    recipient = _seed_local_recipient('localuser')
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    notification = Notification.query.one()
    assert notification.user_id == recipient.id
    assert notification.notif_type == NOTIF_MENTION
    assert notification.subtype == 'comment_mention'
    assert notification.url == f"{app.config['SERVER_URL']}/comment/{reply.id}"


def test_the_unread_counter_increments_rather_than_resets(app, db_session, redis_lock_only_double):
    """`recipient.unread_notifications += 1` (app/activitypub/util.py).
    Seeded to a non-default **3** so a mutant that instead SETS the counter
    to 1 -- indistinguishable from a correct increment at the usual zero
    baseline -- is caught: `assert == 4` fails against a set-to-1 mutant's 1,
    where `assert == 1` would not have.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    recipient = _seed_local_recipient('localuser')
    recipient.unread_notifications = 3
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    refreshed = User.query.get(recipient.id)
    assert refreshed.unread_notifications == 4


def test_a_recipient_that_does_not_resolve_produces_no_notification(app, db_session, redis_lock_only_double):
    """`recipient = db.session.query(User).filter_by(ap_profile_id=lutn,
    ap_id=None).first(); if not recipient: continue`. This Mention's href
    clears every collection guard above (right host, right case, `Mention`
    type, not the reply's own author) so `local_users_to_notify` is
    genuinely non-empty here -- but no `User` row carries that profile id at
    all (no call to `_seed_local_recipient`), so it is THIS lookup that
    turns up empty, not an upstream collection guard.

    `if post_reply.instance.software == 'mbin' or ...` sits AFTER `if not
    recipient: continue` in the source, so this test never reaches it and,
    unlike the tests below, needs no `_use_a_non_microblog_instance`: D243
    is structurally unreachable when the loop body never gets past the
    recipient lookup.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello',
                          tag=[_mention('nobody'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0


def test_a_recipient_blocked_by_the_sender_gets_no_notification(app, db_session, redis_lock_only_double):
    """`blocked_senders = blocked_users(recipient.id); if post_reply.user_id
    not in blocked_senders:`. `blocked_users(user_id)` (app/utils.py) reads
    `UserBlock.filter_by(blocker_id=user_id)` -- the ids that `user_id` has
    BLOCKED. So `blocked_senders` here is who the RECIPIENT has blocked, and
    the guard suppresses only when the REPLIER (`post_reply.user_id`) is in
    THAT list -- the recipient blocking the sender, not the reverse. Built
    with `make_user_block(recipient, replier)` (tests/factories.py:
    `UserBlock(blocker_id=blocker.id, blocked_id=blocked.id)`, the same
    direction `blocked_users` reads), the direction read off the source
    rather than assumed.

    Needs `_use_a_non_microblog_instance` for the same reason the collection
    guards earlier in this module do: without it, D243 crashes before this
    guard is ever reached, and the resulting zero Notifications would look
    identical to a correct suppression -- masking whatever this guard
    actually did.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    recipient = _seed_local_recipient('localuser')
    make_user_block(recipient, replier)
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert Notification.query.count() == 0
    refreshed = User.query.get(recipient.id)
    assert refreshed.unread_notifications == 0


def test_the_recipient_language_wrapper_is_reached(app, db_session, redis_lock_only_double, monkeypatch):
    """`with force_locale(get_recipient_language(recipient.id)):` (app/
    activitypub/util.py). Both names are imported directly into
    `app.activitypub.util`'s own namespace (confirmed by reading its import
    block: `from app.utils import ..., get_recipient_language, ...` and
    `from flask_babel import _, force_locale, gettext`), so both are
    replaced with spies here rather than exercised for a translated string:
    this checkout ships no compiled `.mo` catalogs at all (`app/translations/
    */LC_MESSAGES` is empty for every language directory, confirmed with
    `find`), so `gettext` has no catalog to translate INTO and a locale-based
    string-content assertion would not be testing anything real.

    The spies instead pin the call site's own behaviour: `get_recipient_
    language` is invoked with the RECIPIENT's id (not the replier's, not the
    post author's), and `force_locale` is entered with exactly the value
    that call returned, before the `Notification` row is built.
    """
    community, post, replier = _seed_scenario()
    _use_a_non_microblog_instance(replier)
    recipient = _seed_local_recipient('localuser')
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    language_calls = []

    def fake_get_recipient_language(user_id):
        language_calls.append(user_id)
        return 'ca'

    entered_locales = []

    @contextlib.contextmanager
    def fake_force_locale(locale):
        entered_locales.append(locale)
        yield

    monkeypatch.setattr('app.activitypub.util.get_recipient_language', fake_get_recipient_language)
    monkeypatch.setattr('app.activitypub.util.force_locale', fake_force_locale)

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    assert language_calls == [recipient.id]
    assert entered_locales == ['ca']
    notification = Notification.query.one()
    assert notification.subtype == 'comment_mention'


# --- User flair ------------------------------------------------------------
#
# `request_json['object']['flair']` (Lemmy-style user flair on a comment),
# read after the Mention scan and before `PostReply.new`. `UserFlair`
# (app/models.py) has no factory in tests/factories.py -- it is a bare
# `id`/`user_id`/`community_id`/`flair` row with no timestamps or ap_id, so
# every test below seeds it by hand.


def test_a_flair_on_a_user_with_none_creates_a_user_flair_row(app, db_session, redis_lock_only_double):
    """Both conjuncts of `'flair' in request_json['object'] and request_json[
    'object']['flair']` true, and the `UserFlair.query.filter(...).first()`
    lookup finds nothing, so the `else:` branch creates. Only the CREATE
    branch calls `.strip()` on the value (read directly off
    app/activitypub/util.py: `flair=request_json['object']['flair'].strip()`)
    -- padding the fixture value proves that, and distinguishes this branch
    from the update branch below, which assigns the raw string.
    """
    community, post, replier = _seed_scenario()
    document = _reply_doc(content='hello', flair='  gold  ')

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    flair = UserFlair.query.filter_by(user_id=replier.id, community_id=community.id).one()
    assert flair.flair == 'gold'


def test_a_flair_on_a_user_with_an_existing_flair_updates_it(app, db_session, redis_lock_only_double):
    """The `if existing_flair:` branch. Unlike the create branch above, this
    one assigns the raw value with no `.strip()` call (read directly off
    app/activitypub/util.py: `existing_flair.flair = request_json['object'][
    'flair']`) -- the fixture value below carries no padding, so this test
    cannot be confused with the create branch's stripped assertion.

    Asserted by a row count of exactly one, not merely that some row carries
    the new value -- a create-instead-of-update bug would leave two rows,
    one of them still matching this filter and satisfying a weaker
    assertion.
    """
    community, post, replier = _seed_scenario()
    existing = UserFlair(user_id=replier.id, community_id=community.id, flair='bronze')
    db.session.add(existing)
    db.session.commit()
    document = _reply_doc(content='hello', flair='gold')

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    rows = UserFlair.query.filter_by(user_id=replier.id, community_id=community.id).all()
    assert len(rows) == 1
    assert rows[0].flair == 'gold'


def test_an_absent_flair_key_leaves_an_existing_flair_unchanged(app, db_session, redis_lock_only_double):
    """The `'flair' in request_json['object']` conjunct: no `flair` key at
    all in the document. A non-zero baseline is seeded first, so a guard
    that fired anyway (deleting a UserFlair row, say, or blanking its value)
    would be caught, not just a guard that failed to CREATE one.
    """
    community, post, replier = _seed_scenario()
    existing = UserFlair(user_id=replier.id, community_id=community.id, flair='bronze')
    db.session.add(existing)
    db.session.commit()
    document = _reply_doc(content='hello')

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    rows = UserFlair.query.filter_by(user_id=replier.id, community_id=community.id).all()
    assert len(rows) == 1
    assert rows[0].flair == 'bronze'


def test_a_falsy_flair_value_leaves_an_existing_flair_unchanged(app, db_session, redis_lock_only_double):
    """The truthiness half of `'flair' in request_json['object'] and
    request_json['object']['flair']` -- the key is present but empty, unlike
    the test above where the key is absent entirely. A non-zero baseline is
    seeded first for the same reason.
    """
    community, post, replier = _seed_scenario()
    existing = UserFlair(user_id=replier.id, community_id=community.id, flair='bronze')
    db.session.add(existing)
    db.session.commit()
    document = _reply_doc(content='hello', flair='')

    reply = _create(community, post, replier, document=document)

    assert reply is not None
    rows = UserFlair.query.filter_by(user_id=replier.id, community_id=community.id).all()
    assert len(rows) == 1
    assert rows[0].flair == 'bronze'


# --- The four Mention de-duplication rules -------------------------------
#
# All four sit inside `create_post_reply`'s `for lutn in
# local_users_to_notify:` loop, behind one gate quoted from
# app/activitypub/util.py:
#
#     if post_reply.instance.software == 'mbin' or post_reply.instance.software in MICROBLOG_APPS:
#
# `MICROBLOG_APPS` is `["mastodon", "misskey", "akkoma", "iceshrimp",
# "pleroma", "fedibird"]` (app/constants.py) and `make_instance`
# (tests/factories.py) defaults `software='mastodon'`, so `_seed_scenario`
# lands ON the gated path by default. The Task 4/5 tests above call
# `_use_a_non_microblog_instance` to get OFF it; every test below
# deliberately does not, and asserts the software value it is relying on.
#
# The rules, in the order they run, quoted from the source:
#
#   1. `if recipient.id == post.user_id: continue`
#   2. a Notification for the recipient with `notif_type == NOTIF_MENTION`,
#      `subtype == "post_mention"` and
#      `Notification.targets.op("->>")("post_id").cast(Integer) == post_reply.post_id`
#   3. a Notification for the recipient with `notif_type == NOTIF_MENTION`,
#      `subtype == "comment_mention"` and
#      `Notification.targets.op("->>")("comment_id").cast(Integer).in_(ids)`
#   4. `SELECT user_id FROM "post_reply" WHERE id IN :ids` containing
#      `recipient.id`
#
# Rules 2 and 3 cast a JSON field to Integer, so the seeded `targets` below
# store `post.id` / `parent.id` as ints -- which is what production's own
# writers store (`'post_id': post.id` in app/models.py's post_mention block,
# `'comment_id': post_reply.id` in this function's own notification block).
#
# `ids` is built once, between rules 2 and 3, and reused by rule 4:
#
#     ids = []
#     for element in post_reply.path:
#         if element == 0 or element == post_reply.id:
#             continue
#         ids.append(element)
#
# In THIS copy, rule 3's query and its `continue` sit OUTSIDE that
# `for element` loop -- so rule 3 works. That is the same spelling
# `update_post_reply_from_activity` carries after sub-project 14's Fix F;
# the two blocks were read side by side before these tests were written and
# are identical except for D243, which the update copy has fixed
# (`ids = tuple(ids)` followed by `if ids:`) and this copy has not.
#
# The shape of `post_reply.path`. `PostReply.new` (app/models.py) ends with
#
#     if in_reply_to and in_reply_to.path:
#         reply.path = in_reply_to.path[:]
#         reply.path.append(reply.id)
#         ...
#     else:
#         reply.path = [0, reply.id]
#
# so a top-level reply gets `[0, reply.id]`, whose `ids` is EMPTY. An empty
# `ids` is D243: `ids = tuple(ids)` then `WHERE id IN :ids` renders `IN ()`,
# which Postgres rejects, and this function's tail `except Exception as ex`
# swallows the crash into a `None` return and zero Notifications -- so a
# suppression test on an empty `ids` would pass because of the crash, not
# because of its rule. `make_post_reply` (tests/factories.py) sets neither
# `path` nor `parent_id`, and a parent whose `path` is None takes the `else`
# branch above too, giving the CHILD `[0, child.id]` and an empty `ids` all
# over again. `_seed_chain_comment` below therefore sets `path` explicitly,
# and every test that needs a rule other than the crash to decide the outcome
# replies to one of those comments rather than to the post.


def _seed_chain_comment(post, author, slug, parent=None):
    """A PostReply in `post`'s comment chain with the columns
    `create_post_reply` and `PostReply.new` actually read set explicitly.

    `ap_id` carries 'comment' because `find_reply_parent` tests
    `if 'comment' in in_reply_to:` before it tests for 'post'.

    `path` is the load-bearing one, for the reason set out in the block
    comment above: `make_post_reply` leaves it None, and `PostReply.new`
    copies a parent's path only `if in_reply_to and in_reply_to.path`.
    A root comment here gets `[0, self.id]`; a child gets its parent's path
    with its own id appended, which is exactly what `PostReply.new` builds.
    """
    reply = make_post_reply(post, author)
    reply.ap_id = f'https://{PEER}/comment/{slug}'
    reply.parent_id = parent.id if parent is not None else None
    reply.path = [0, reply.id] if parent is None else list(parent.path) + [reply.id]
    reply.depth = len(reply.path) - 2
    reply.root_id = reply.path[1]
    db.session.commit()
    return reply


def test_a_microblog_mention_no_rule_suppresses_is_delivered(app, db_session, redis_lock_only_double):
    """The control for the four suppression tests below.

    Same fixture shape they use -- the gated (microblog) path, a Mention of a
    local user, a reply to an existing comment so `ids` is non-empty -- with
    none of the four rules' state seeded. It produces a Notification, which
    is what makes the four "zero Notifications" assertions below mean
    something: without it they would be negative against a path that never
    delivers anything under this fixture at all.

    Also records the two facts the rest of the section rests on: the
    instance's software really is inside `MICROBLOG_APPS`, and a reply to a
    comment whose `path` is `[0, parent.id]` gets `[0, parent.id, reply.id]`,
    whose `ids` is `[parent.id]`.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    recipient = _seed_local_recipient('localuser')
    assert replier.instance.software == 'mastodon'
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    assert reply.path == [0, parent.id, reply.id]
    notification = Notification.query.filter_by(user_id=recipient.id).one()
    assert notification.notif_type == NOTIF_MENTION
    assert notification.subtype == 'comment_mention'
    assert notification.targets['comment_id'] == reply.id


def test_a_microblog_mention_of_the_post_author_is_suppressed(app, db_session, redis_lock_only_double):
    """Rule 1: `if recipient.id == post.user_id: continue`.

    The recipient is made the POST's author while the parent comment keeps
    its remote author, so the Mention still survives the collection phase's
    `if profile_id != reply_parent.author.ap_profile_id` (whose `reply_parent`
    is the parent comment here, not the post) and reaches the loop.

    Replies to a comment rather than to the post so `ids` is `[parent.id]`.
    That is not what rule 1 keys on -- it routes around D243, so that a
    mutant dropping rule 1 falls through rules 2, 3 and 4 to the delivery
    block instead of into the swallowed `IN ()` crash, which would produce
    the same zero Notifications and leave the mutant alive.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    recipient = _seed_local_recipient('localuser')
    post.user_id = recipient.id
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    assert reply.path == [0, parent.id, reply.id]
    assert Notification.query.filter_by(user_id=recipient.id).count() == 0


def test_a_microblog_mention_mirroring_a_post_mention_is_suppressed(app, db_session, redis_lock_only_double):
    """Rule 2: an existing `post_mention` Notification for this recipient
    whose `targets->>'post_id'`, cast to Integer, equals `post_reply.post_id`.

    `post_id` is stored as an int, matching what app/models.py's post_mention
    block stores (`'post_id': post.id`). That is a fidelity choice, not a
    correctness one: `->>` yields text for a JSON number and for a JSON
    string alike, so `{'post_id': '1'}` would cast to the same Integer and
    match just as well. The int is used because it is the shape every
    production post_mention writer stores, so this fixture cannot pass
    against a row production never produces. (Had the lookup missed, the
    failure direction would be loud, not silent: rule 2 would not fire, the
    Mention would be delivered, and `len(rows) == 1` below would see two
    rows and fail.)

    Replies to a comment so `ids` is non-empty, routing around D243 for the
    same reason rule 1's test does: with rule 2 dropped, the fall-through has
    to reach the delivery block, not the crash.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    recipient = _seed_local_recipient('localuser')
    existing = Notification(user_id=recipient.id, author_id=replier.id,
                            title='You have been mentioned in a post',
                            url=f'/post/{post.id}',
                            notif_type=NOTIF_MENTION, subtype='post_mention',
                            targets={'gen': '0', 'post_id': post.id})
    db.session.add(existing)
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    rows = Notification.query.filter_by(user_id=recipient.id).all()
    assert len(rows) == 1
    assert rows[0].subtype == 'post_mention'
    assert rows[0].targets['post_id'] == post.id


def test_a_microblog_mention_mirroring_a_comment_mention_is_suppressed(app, db_session, redis_lock_only_double):
    """Rule 3: an existing `comment_mention` Notification for this recipient
    whose `targets->>'comment_id'`, cast to Integer, is `.in_(ids)`.

    `ids` here is `[parent.id]` -- the new reply's path is
    `[0, parent.id, reply.id]` and the loop building `ids` drops `0` and
    `post_reply.id`. The seeded Notification names `parent.id`, so it is
    inside `ids`; `comment_id` is stored as an int to match the cast, the
    same way rule 2's `post_id` is.

    This is the one rule whose own fixture already makes `ids` non-empty --
    an empty `ids` would render rule 3's filter as `.in_([])`, which matches
    nothing, so there would be no rule to test. No separate routing around
    D243 is needed or added here: with rule 3 dropped, `ids` is still
    `[parent.id]` and rule 4's query is still well-formed.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    recipient = _seed_local_recipient('localuser')
    existing = Notification(user_id=recipient.id, author_id=replier.id,
                            title=f'You have been mentioned in comment {parent.id}',
                            url=f'/comment/{parent.id}',
                            notif_type=NOTIF_MENTION, subtype='comment_mention',
                            targets={'gen': '0', 'post_id': post.id,
                                     'comment_id': parent.id})
    db.session.add(existing)
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    assert reply.path == [0, parent.id, reply.id]
    rows = Notification.query.filter_by(user_id=recipient.id).all()
    assert len(rows) == 1
    assert rows[0].targets['comment_id'] == parent.id


def test_a_microblog_mention_of_an_earlier_commenter_in_the_chain_is_suppressed(app, db_session, redis_lock_only_double):
    """Rule 4: `SELECT user_id FROM "post_reply" WHERE id IN :ids` returning
    the recipient.

    Needs a chain two comments deep. The recipient authors the ROOT comment;
    a remote user authors the comment actually being replied to. Both are
    reasons: if the recipient authored the immediate parent instead, the
    collection phase's `if profile_id != reply_parent.author.ap_profile_id`
    would drop the Mention before the loop ever ran, and the test would pass
    without rule 4 existing.

    `ids` is `[root.id, parent.id]` -- the new reply's path is
    `[0, root.id, parent.id, reply.id]` -- so `root.id` is in the tuple and
    its `user_id` is the recipient's. No routing around D243 is needed: this
    rule's own fixture is what makes `ids` non-empty, and an empty `ids` is
    exactly the state in which the rule cannot run at all.
    """
    community, post, replier = _seed_scenario()
    recipient = _seed_local_recipient('localuser')
    root = _seed_chain_comment(post, recipient, 'root')
    parent = _seed_chain_comment(post, post.author, 'parent', parent=root)
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    assert reply.path == [0, root.id, parent.id, reply.id]
    assert root.user_id == recipient.id
    assert Notification.query.filter_by(user_id=recipient.id).count() == 0


def test_the_mbin_arm_of_the_gate_reaches_the_rules(app, db_session, redis_lock_only_double):
    """The gate's other arm: `post_reply.instance.software == 'mbin'`.

    'mbin' is NOT one of `MICROBLOG_APPS`' six entries (asserted below), so
    that literal comparison is the only way an mbin instance reaches these
    rules -- every other test in this section arrives through the
    `in MICROBLOG_APPS` arm with `make_instance`'s default 'mastodon'.

    Uses rule 1's state to show the arm reaching a rule rather than merely
    being evaluated, and replies to a comment for the same D243 reason rule
    1's own test does.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    recipient = _seed_local_recipient('localuser')
    post.user_id = recipient.id
    replier.instance.software = 'mbin'
    db.session.commit()
    assert 'mbin' not in MICROBLOG_APPS
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    assert reply.path == [0, parent.id, reply.id]
    assert Notification.query.filter_by(user_id=recipient.id).count() == 0


def test_a_post_mention_naming_another_post_does_not_suppress(app, db_session, redis_lock_only_double):
    """Rule 2's near miss: the recipient has a `post_mention`, but for a
    different post, so
    `Notification.targets.op("->>")("post_id").cast(Integer) == post_reply.post_id`
    is False and the Mention is delivered.

    Without this, rule 2's test alone cannot distinguish the real filter from
    one that suppresses on the mere existence of any `post_mention` for the
    recipient.

    A second decoy rides along: a `post_mention` naming THIS post but
    belonging to a different local user, which the filter's
    `Notification.user_id == recipient.id` conjunct must exclude.
    """
    community, post, replier = _seed_scenario()
    other_post = make_post(community, post.author, ap_id=f'https://{PEER}/post/2')
    parent = _seed_chain_comment(post, post.author, 'parent')
    recipient = _seed_local_recipient('localuser')
    bystander = _seed_local_recipient('bystander')
    unrelated = Notification(user_id=recipient.id, author_id=replier.id,
                             title='You have been mentioned in a post',
                             url=f'/post/{other_post.id}',
                             notif_type=NOTIF_MENTION, subtype='post_mention',
                             targets={'gen': '0', 'post_id': other_post.id})
    someone_elses = Notification(user_id=bystander.id, author_id=replier.id,
                                 title='You have been mentioned in a post',
                                 url=f'/post/{post.id}',
                                 notif_type=NOTIF_MENTION, subtype='post_mention',
                                 targets={'gen': '0', 'post_id': post.id})
    db.session.add(unrelated)
    db.session.add(someone_elses)
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    delivered = Notification.query.filter_by(user_id=recipient.id,
                                             subtype='comment_mention').one()
    assert delivered.targets['comment_id'] == reply.id


def test_a_comment_mention_outside_the_chain_does_not_suppress(app, db_session, redis_lock_only_double):
    """Rule 3's near miss: the recipient has a `comment_mention`, but it names
    a sibling comment that is not an ancestor of this reply, so its
    `comment_id` is not `.in_(ids)` and the Mention is delivered.

    The sibling is a root comment of its own (`path == [0, sibling.id]`)
    while the reply's `ids` is `[parent.id]`, so the two never overlap. The
    seeded row deliberately carries this post's `post_id`, which also makes
    it a near miss for rule 2's `subtype == "post_mention"` filter.

    A second decoy rides along: a `comment_mention` that DOES name
    `parent.id` -- inside `ids` -- but belongs to a different local user,
    which the filter's `Notification.user_id == recipient.id` conjunct must
    exclude.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    sibling = _seed_chain_comment(post, post.author, 'sibling')
    recipient = _seed_local_recipient('localuser')
    bystander = _seed_local_recipient('bystander')
    unrelated = Notification(user_id=recipient.id, author_id=replier.id,
                             title=f'You have been mentioned in comment {sibling.id}',
                             url=f'/comment/{sibling.id}',
                             notif_type=NOTIF_MENTION, subtype='comment_mention',
                             targets={'gen': '0', 'post_id': post.id,
                                      'comment_id': sibling.id})
    someone_elses = Notification(user_id=bystander.id, author_id=replier.id,
                                 title=f'You have been mentioned in comment {parent.id}',
                                 url=f'/comment/{parent.id}',
                                 notif_type=NOTIF_MENTION, subtype='comment_mention',
                                 targets={'gen': '0', 'post_id': post.id,
                                          'comment_id': parent.id})
    db.session.add(unrelated)
    db.session.add(someone_elses)
    db.session.commit()
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document,
                    in_reply_to=parent.ap_id)

    assert reply is not None
    assert reply.path == [0, parent.id, reply.id]
    assert sibling.path == [0, sibling.id]
    delivered = Notification.query.filter_by(user_id=recipient.id,
                                             subtype='comment_mention').all()
    assert sorted(n.targets['comment_id'] for n in delivered) == sorted([sibling.id, reply.id])


def test_a_top_level_microblog_mention_is_lost_to_the_empty_id_tuple(app, db_session, redis_lock_only_double):
    """D243, pinned as CURRENT behaviour, not endorsed.

    A top-level reply's path is `[0, reply.id]`, so `ids` is empty; rule 4
    then runs `ids = tuple(ids)` and
    `db.session.execute(text('SELECT user_id FROM "post_reply" WHERE id IN :ids'), {'ids': ids})`,
    which psycopg2 renders as `IN ()`. Postgres rejects that, and
    `create_post_reply`'s tail `except Exception as ex: log_incoming_ap(...);
    return None` swallows it -- so a caller sees None and the mentioned user
    gets nothing, even though the PostReply row was already committed inside
    `PostReply.new`.

    The assertions below name that split state: the reply row IS persisted,
    the function returned None, and no Notification exists. Task 9 fixes the
    defect (the update path's `if ids:` guard is the shape of the fix) and
    this pin inverts: the reply will be returned and the Notification will be
    created, exactly as `test_a_microblog_mention_no_rule_suppresses_is_delivered`
    already sees for a nested reply.

    The `db.session.rollback()` is not decoration: the psycopg2 error leaves
    the transaction aborted, so every later statement in it fails with
    InFailedSqlTransaction until it is rolled back. `PostReply.new` committed
    the reply before the crash, so the row survives the rollback.

    `assert reply is None` deliberately does NOT say which exception was
    swallowed, and that gap is accepted rather than overlooked: the tail
    catches everything, so in principle an unrelated error would satisfy it.
    The natural strengthening -- asserting the `ActivityPubLog` message the
    way this module's head-guard tests do via the `ap_log` fixture -- is not
    available here, because `log_incoming_ap` writes that row with
    `db.session.add(...)` / `commit()` INSIDE the already-aborted
    transaction, so turning the config flag on replaces the pinned state with
    a second, different failure. The three assertions below pin the shape of
    THIS crash instead: an empty `ids` (`path == [0, id]`), a committed
    PostReply, a None return, and no Notification.
    """
    community, post, replier = _seed_scenario()
    recipient = _seed_local_recipient('localuser')
    assert replier.instance.software == 'mastodon'
    document = _reply_doc(content='hello',
                          tag=[_mention('localuser'), {'name': 'decoy'}])

    reply = _create(community, post, replier, document=document)

    assert reply is None
    db.session.rollback()
    persisted = PostReply.query.filter_by(ap_id=f'https://{PEER}/comment/1').one()
    assert persisted.path == [0, persisted.id]
    assert Notification.query.filter_by(user_id=recipient.id).count() == 0


# --- notify_about_post_reply: the parent_reply-is-None branch -------------
#
# Called directly here, not through `create_post_reply`: its two arguments
# are rows (a PostReply-or-None and a PostReply), and driving it end-to-end
# would make every assertion depend on everything upstream, including D243's
# swallowed crash (see the block comment above `_seed_chain_comment`), which
# produces zero Notifications for a reason unrelated to any guard in this
# function.
#
# `notification_subscribers(entity_id, entity_type)` (app/utils.py) is a
# plain read: `SELECT user_id FROM "notification_subscription" WHERE
# entity_id = :entity_id AND type = :type`. For this branch it is called as
# `notification_subscribers(new_reply.post.id, NOTIF_POST)`, so a row seeded
# via `make_notification_subscription(user, post.id, NOTIF_POST)`
# (tests/factories.py) is exactly what it selects.
#
# The only guard in this branch is `if new_reply.user_id != notify_id:` --
# one conjunct. Reaching its True side (a subscriber who is not the replier)
# cannot kill a mutant that forces the guard False, and reaching its False
# side (the replier, self-subscribed) cannot kill a mutant that forces it
# True -- so the "subscriber notified" and "replier excluded" tests below
# are both required and neither is redundant with the other for that
# purpose, even though both incidentally also kill a `!=`-to-`==` flip.
#
# The unread counter here is `user.unread_notifications += 1` -- an
# INCREMENT of whatever is already in the column, not a recount. (Contrast
# `notify_about_post_reply`'s `else` branch -- parent_reply is not None,
# Task 8's target -- which instead does
# `user.unread_notifications = Notification.query.filter_by(...).count()`,
# a recount.) An increment and an assignment agree if the seeded baseline is
# the count of what gets created (here, 0 -> 1), so the baseline seeded below
# is a non-zero, non-1 value that only an increment reproduces.


def _seed_post_subscriber(post, name='subscriber'):
    """A local user subscribed to `post` for NOTIF_POST -- what
    `notification_subscribers(new_reply.post.id, NOTIF_POST)` selects.
    """
    subscriber = make_user(None, name, local=True)
    make_notification_subscription(subscriber, post.id, NOTIF_POST)
    return subscriber


def test_a_post_subscriber_is_notified_of_a_top_level_reply(app, db_session, redis_lock_only_double):
    """The branch's normal job: one subscriber, not the replier, gets a
    Notification whose fields are read straight from the code -- `notif_type`
    is the NOTIF_POST passed to `notification_subscribers`, `subtype` is the
    literal `'top_level_comment_on_followed_post'`, `url` is built from the
    post and reply ids, and `targets` carries the community's `name` (its
    `ap_id` is None here -- `make_community` never sets it, so the `community.ap_id
    if community.ap_id else community.name` fallback lands on `name`) and the
    replier's `ap_id` (set, because `_seed_scenario`'s replier is remote).
    """
    community, post, replier = _seed_scenario()
    subscriber = _seed_post_subscriber(post)
    new_reply = make_post_reply(post, replier, body='a top level reply')

    notify_about_post_reply(None, new_reply)

    notification = Notification.query.filter_by(user_id=subscriber.id).one()
    assert notification.notif_type == NOTIF_POST
    assert notification.subtype == 'top_level_comment_on_followed_post'
    assert notification.url == f'/post/{post.id}/comment/{new_reply.id}#comment_{new_reply.id}'
    assert notification.targets == {
        'gen': '0',
        'post_id': post.id,
        'post_title': post.title,
        'community_name': community.name,
        'author_user_name': replier.ap_id,
        'comment_id': new_reply.id,
        'comment_body': new_reply.body,
    }


def test_the_replier_is_not_notified_of_their_own_top_level_reply(app, db_session, redis_lock_only_double):
    """The `new_reply.user_id != notify_id` guard's False side: the replier
    subscribed to their own post (a plausible case -- someone subscribes,
    then later comments on the same post themselves) gets no Notification
    for their own comment even though `notification_subscribers` returns
    their id.
    """
    community, post, replier = _seed_scenario()
    make_notification_subscription(replier, post.id, NOTIF_POST)
    new_reply = make_post_reply(post, replier, body='a top level reply')

    notify_about_post_reply(None, new_reply)

    assert Notification.query.filter_by(user_id=replier.id).count() == 0


def test_the_subscriber_unread_count_is_incremented_not_reassigned(app, db_session, redis_lock_only_double):
    """`user.unread_notifications += 1`, read directly from the source (see
    this section's header comment). The baseline is seeded to 5, a value
    that disagrees with both 0 (the column default) and 1 (the post-branch
    Notification count) -- so only a true increment lands on 6; a recount or
    a bare assignment of the created-count would leave 1.
    """
    community, post, replier = _seed_scenario()
    subscriber = _seed_post_subscriber(post)
    subscriber.unread_notifications = 5
    db.session.commit()
    new_reply = make_post_reply(post, replier, body='a top level reply')

    notify_about_post_reply(None, new_reply)

    assert subscriber.unread_notifications == 6


def test_no_subscribers_creates_no_notification_for_the_reply(app, db_session, redis_lock_only_double):
    """No row in `notification_subscription` names this post, so
    `notification_subscribers` returns an empty list and the `for notify_id
    in send_notifs_to:` loop body never runs.

    A bystander's unrelated Notification is seeded first so the total count
    is non-zero going in -- otherwise an unchanged count of 0 would prove
    nothing distinguishable from "the table is simply empty".
    """
    community, post, replier = _seed_scenario()
    bystander = make_user(None, 'bystander', local=True)
    baseline = Notification(user_id=bystander.id, author_id=replier.id,
                            title='an unrelated notification', url='/unrelated',
                            notif_type=NOTIF_POST, subtype='top_level_comment_on_followed_post',
                            targets={'gen': '0'})
    db.session.add(baseline)
    db.session.commit()
    before = Notification.query.count()
    assert before == 1
    new_reply = make_post_reply(post, replier, body='a top level reply')

    notify_about_post_reply(None, new_reply)

    assert Notification.query.count() == before


# --- notify_about_post_reply: the parent_reply-is-not-None branch ---------
#
# A reply to a comment rather than to a post. `_seed_chain_comment` builds the
# genuine parent-child `path` this branch needs (see its own docstring) --
# unlike a bare `make_post_reply`, whose `path` is None.
#
# This branch does three things the top-level branch does not:
#
# 1. It marks existing Notifications about `parent_reply` read for the new
#    reply's author, via a two-conjunct UPDATE: `Notification.user_id ==
#    new_reply.user_id` AND `Notification.targets['comment_id'].as_string()
#    == str(parent_reply.id)`. Each conjunct gets its own negative test below,
#    because reaching the True side of an AND cannot kill a mutant that drops
#    either conjunct on its own -- only a row that satisfies one conjunct and
#    not the other, and stays unread, can.
#
# 2. It RECOUNTS `new_reply.user_id`'s `unread_notifications` from the
#    database (`Notification.query.filter_by(user_id=user.id,
#    read=False).count()`) rather than incrementing it, the way the
#    top-level branch does. A seed that agrees with the true count cannot
#    distinguish a recount from an increment, so the test below seeds a
#    deliberately wrong baseline.
#
# 3. It notifies `parent_reply`'s subscribers -- `notification_subscribers(
#    parent_reply.id, NOTIF_REPLY)` -- under the same `new_reply.user_id !=
#    notify_id` shape of guard the top-level branch uses for its own
#    subscribers, so both sides get their own test for the same reason set
#    out above that section's header comment.


def test_the_parents_notification_is_marked_read_for_the_new_replys_author(app, db_session, redis_lock_only_double):
    """The branch's opening UPDATE, read from source: `Notification.user_id
    == new_reply.user_id` (the new reply's author -- not `parent_reply`'s own
    author) AND `Notification.targets['comment_id'].as_string() ==
    str(parent_reply.id)`.

    Seeds an unread Notification for `author` whose `targets['comment_id']`
    names `parent`'s id -- the shape a notification that once told `author`
    about `parent` would carry -- and asserts it becomes read.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    author = make_user(None, 'author', local=True)
    new_reply = _seed_chain_comment(post, author, 'child', parent=parent)
    notif = Notification(user_id=author.id, author_id=replier.id,
                         title='a notification about the parent comment',
                         url=f'/comment/{parent.id}',
                         notif_type=NOTIF_REPLY, subtype='new_reply_on_followed_comment',
                         targets={'gen': '0', 'comment_id': parent.id})
    db.session.add(notif)
    db.session.commit()

    notify_about_post_reply(parent, new_reply)

    assert Notification.query.get(notif.id).read is True


def test_a_notification_about_a_different_comment_is_left_unread(app, db_session, redis_lock_only_double):
    """The `targets['comment_id']` conjunct, isolated: a Notification for
    `author` (the matching user) that names some OTHER comment stays unread.
    Proves the comparison itself gates the update, not merely the user_id
    filter -- a mutant that drops this conjunct would mark this row read too.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    other = _seed_chain_comment(post, post.author, 'other')
    author = make_user(None, 'author', local=True)
    new_reply = _seed_chain_comment(post, author, 'child', parent=parent)
    notif = Notification(user_id=author.id, author_id=replier.id,
                         title='a notification about a different comment',
                         url=f'/comment/{other.id}',
                         notif_type=NOTIF_REPLY, subtype='new_reply_on_followed_comment',
                         targets={'gen': '0', 'comment_id': other.id})
    db.session.add(notif)
    db.session.commit()

    notify_about_post_reply(parent, new_reply)

    assert Notification.query.get(notif.id).read is False


def test_a_matching_notification_for_a_different_user_is_left_unread(app, db_session, redis_lock_only_double):
    """The `user_id` conjunct, isolated: a Notification naming `parent`'s id
    (the matching comment) but belonging to a DIFFERENT user stays unread.
    Proves the update is scoped to `new_reply.user_id`, not to every
    notification about `parent` regardless of owner -- a mutant that drops
    this conjunct would mark this row read too.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    author = make_user(None, 'author', local=True)
    bystander = make_user(None, 'bystander', local=True)
    new_reply = _seed_chain_comment(post, author, 'child', parent=parent)
    notif = Notification(user_id=bystander.id, author_id=replier.id,
                         title='a notification about the parent for someone else',
                         url=f'/comment/{parent.id}',
                         notif_type=NOTIF_REPLY, subtype='new_reply_on_followed_comment',
                         targets={'gen': '0', 'comment_id': parent.id})
    db.session.add(notif)
    db.session.commit()

    notify_about_post_reply(parent, new_reply)

    assert Notification.query.get(notif.id).read is False


def test_the_authors_unread_total_is_recounted_not_incremented(app, db_session, redis_lock_only_double):
    """The reply branch RECOUNTS `unread_notifications` from the database
    where the top-level branch increments it.

    The seeded counter is deliberately wrong -- 9 against a real unread count
    of 1 -- because an increment would give 10 and a recount gives the true
    figure. A seed that agreed with the truth could not tell them apart.

    The one unread Notification names a comment OTHER than `parent` (`other`),
    so the branch's own mark-read step -- proven separately above -- does not
    touch it, and it survives to be the sole row the recount finds. `author`
    is excluded from the subscriber loop by its own `new_reply.user_id !=
    notify_id` guard (no subscription is seeded for `parent` here anyway), so
    no new Notification for `author` is created after the mark-read step.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    other = _seed_chain_comment(post, post.author, 'other')
    author = make_user(None, 'author', local=True)
    new_reply = _seed_chain_comment(post, author, 'child', parent=parent)
    unrelated = Notification(user_id=author.id, author_id=replier.id,
                             title='an unrelated unread notification',
                             url=f'/comment/{other.id}',
                             notif_type=NOTIF_REPLY, subtype='new_reply_on_followed_comment',
                             targets={'gen': '0', 'comment_id': other.id})
    db.session.add(unrelated)
    db.session.commit()
    author.unread_notifications = 9
    db.session.commit()

    notify_about_post_reply(parent, new_reply)

    assert author.unread_notifications == Notification.query.filter_by(
        user_id=author.id, read=False).count()
    assert author.unread_notifications != 10
    assert author.unread_notifications == 1


def test_a_parent_subscriber_is_notified_of_a_reply(app, db_session, redis_lock_only_double):
    """The branch's subscriber fan-out: `notification_subscribers(parent_reply.id,
    NOTIF_REPLY)`, with fields read straight from source -- `notif_type` is
    the NOTIF_REPLY passed in, `subtype` is the literal
    'new_reply_on_followed_comment', `url` is built from the post id and
    `new_reply.parent_id` (which is `parent`'s id, since `_seed_chain_comment`
    sets it), and `targets` carries `parent`'s post id and body plus the new
    reply's id, body, author id and `author_user_name` (the replier's
    `ap_id`, set because `_seed_scenario`'s replier is remote).
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    subscriber = make_user(None, 'subscriber', local=True)
    make_notification_subscription(subscriber, parent.id, NOTIF_REPLY)
    new_reply = _seed_chain_comment(post, replier, 'child', parent=parent)

    notify_about_post_reply(parent, new_reply)

    notification = Notification.query.filter_by(user_id=subscriber.id).one()
    assert notification.notif_type == NOTIF_REPLY
    assert notification.subtype == 'new_reply_on_followed_comment'
    assert notification.url == f'/post/{post.id}/comment/{new_reply.parent_id}#comment_{new_reply.id}'
    assert notification.targets == {
        'gen': '0',
        'post_id': parent.post.id,
        'parent_comment_id': new_reply.parent_id,
        'parent_reply_body': parent.body,
        'comment_id': new_reply.id,
        'comment_body': new_reply.body,
        'author_id': new_reply.user_id,
        'author_user_name': replier.ap_id,
    }


def test_the_replier_is_not_notified_of_their_own_reply_to_a_comment(app, db_session, redis_lock_only_double):
    """The `new_reply.user_id != notify_id` guard's False side: the replier
    subscribed to `parent` (a plausible case -- someone subscribes to a
    comment, then later replies to it themselves) gets no Notification for
    their own reply even though `notification_subscribers` returns their id.
    """
    community, post, replier = _seed_scenario()
    parent = _seed_chain_comment(post, post.author, 'parent')
    make_notification_subscription(replier, parent.id, NOTIF_REPLY)
    new_reply = _seed_chain_comment(post, replier, 'child', parent=parent)

    notify_about_post_reply(parent, new_reply)

    assert Notification.query.filter_by(user_id=replier.id).count() == 0
