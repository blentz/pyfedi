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

Unlike the reply function, `update_post_from_activity` reads
`request_json['object']['type']` unconditionally, later in the function, to
route Video/Question/Event objects -- so every post test in this file passes
`type='Note'` through `_update(...)` to reach the plain Article/Note path,
even tests that exist only to cover the content arm. This is an object-level
key inside `fields`, not one of the envelope-level `type`/`actor`/`id` keys
`_update` deliberately omits below.
"""
import contextlib

import pytest
from sqlalchemy.exc import ProgrammingError

from app import db
from app.activitypub.util import (update_post_from_activity,
                                  update_post_reply_from_activity)
from app.constants import NOTIF_MENTION
from app.models import Language, Notification, PostReply, User
from app.utils import utcnow
from tests.factories import (make_community, make_instance, make_post,
                             make_post_reply, make_site, make_user,
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


def test_a_reply_single_attachment_dict_is_appended(app, db_session, redis_lock_only_double):
    """The `isinstance(..., dict)` arm: a lone attachment object is wrapped
    into a one-element list rather than iterated as a dict's keys (which
    would loop over the strings `'url'` and `'name'`, not the attachment
    itself).

    Also exercises the `'name' in attachment` gate and its use as alt text --
    see the mutation table for why this test turns out to be that gate's
    killer too.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment={'url': 'https://cdn.example/a.png', 'name': 'alt words'},
    ))

    assert '![alt words](https://cdn.example/a.png)' in reply.body


def test_a_reply_attachment_list_is_appended_in_order(app, db_session, redis_lock_only_double):
    """The `isinstance(..., list)` arm, with two entries so the loop runs more
    than once and order is observable.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'url': 'https://cdn.example/1.png'},
                    {'url': 'https://cdn.example/2.png'}],
    ))

    assert reply.body.index('1.png') < reply.body.index('2.png')


def test_a_reply_attachment_url_wins_over_href(app, db_session, redis_lock_only_double):
    """Both keys are read and `url` is read second (`href` first, then `url`
    overwrites it -- confirmed against the current source), so the two
    values differ here, which is what makes the precedence observable --
    equal values would pass whichever won.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'href': 'https://cdn.example/href.png',
                     'url': 'https://cdn.example/url.png'}],
    ))

    assert 'url.png' in reply.body
    assert 'href.png' not in reply.body


def test_a_reply_attachment_href_is_used_when_there_is_no_url(app, db_session, redis_lock_only_double):
    """The `href` half on its own. Without this test the `'href' in
    attachment` conjunct can be deleted with the suite green, because the
    precedence test above supplies both keys and would still pass (`url`
    would simply stay unset by a route that never reads it).
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'href': 'https://cdn.example/href.png'}],
    ))

    assert 'href.png' in reply.body


def test_a_reply_attachment_with_no_url_appends_nothing(app, db_session, redis_lock_only_double):
    """The `if url:` gate's normal (false) side. An attachment carrying only
    a `name` contributes no markdown at all.

    The body is asserted to equal exactly what the content arm produced, so
    an empty `![alt]()` would fail rather than pass unnoticed.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'name': 'alt only'}],
    ))

    assert reply.body == 'body text'


def test_a_reply_attachment_actually_regenerates_body_html(app, db_session, redis_lock_only_double):
    """The `if attachment_list:` gate's true side. A non-empty list must
    cause `body_html` to be re-derived (via `markdown_to_html`) from the
    attachment-appended `body`, not merely leave `body` updated while
    `body_html` still reflects only the content arm's `allowlist_html` pass.

    Without a test that inspects `body_html` after a non-empty attachment
    list, the regeneration call itself is unproven: every other test in this
    block asserts only on `body`, which would look identical whether or not
    `reply.body_html = markdown_to_html(reply.body)` ever ran.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='body text',
        attachment=[{'url': 'https://cdn.example/a.png', 'name': 'alt words'}],
    ))

    assert '<img' in reply.body_html
    assert 'https://cdn.example/a.png' in reply.body_html


def test_an_empty_attachment_list_does_not_regenerate_the_html(app, db_session, redis_lock_only_double):
    """`if attachment_list:` guards the `body_html` regeneration. With an
    empty list the html must remain what the content arm allowlisted.

    Seeded through the html arm rather than the markdown arm so the two
    spellings differ and the assertion can tell them apart.
    """
    reply = _seed_reply()

    update_post_reply_from_activity(reply, _update(
        content='<p>body text</p>', attachment=[],
    ))

    assert reply.body_html == '<p>body text</p>'


def _seed_local_recipient(name='localuser'):
    """A local user the Mention block can resolve.

    `User.query.filter_by(ap_profile_id=..., ap_id=None)` is the lookup, so
    both columns matter. `make_user(None, name, local=True)` already leaves
    `ap_id` None -- and leaves `ap_profile_id` None too, which is why this
    helper sets it: the lookup needs it to equal the lowercased href the
    document sends. Passing `instance=None` is supported; `make_user` falls
    back to `instance_id=1`.
    """
    recipient = make_user(None, name, local=True)
    recipient.ap_profile_id = f'https://test.piefed.local/u/{name}'
    db.session.commit()
    return recipient


def _mention(name='localuser'):
    return {'type': 'Mention', 'href': f'https://test.piefed.local/u/{name}'}


def test_a_reply_mention_of_a_local_user_notifies_them(app, db_session, redis_lock_only_double):
    """The simple path: two tags so the `len(...) > 1` gate is satisfied, a
    Mention naming a local user, a non-microblog instance so the
    de-duplication block is skipped.

    Asserts the Notification row exists with the right recipient, type and
    url -- not merely that some notification was created.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    notification = db.session.query(Notification).filter_by(
        user_id=recipient.id, notif_type=NOTIF_MENTION).one()
    assert notification.url.endswith(f'/comment/{reply.id}')
    assert notification.subtype == 'comment_mention'


def test_a_reply_mention_increments_the_recipients_unread_count(app, db_session, redis_lock_only_double):
    """`recipient.unread_notifications += 1` sits beside the `db.session.add`
    and is a separate statement. Seeded to 3 rather than left at the column
    default, so "incremented" is distinguishable from "set to 1".
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    recipient.unread_notifications = 3
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert recipient.unread_notifications == 4


def test_a_lone_reply_mention_is_ignored(app, db_session, redis_lock_only_double):
    """THE ASYMMETRY. The reply function's tag gate requires
    `len(request_json['object']['tag']) > 1`, so a document carrying exactly
    one tag -- a single Mention -- is skipped entirely. The post function's
    gate has no length condition.

    This test PINS the current behaviour rather than asserting it is correct.
    The spec registers it rather than fixing it, because changing the gate
    changes which notifications this instance generates.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(content='hello', tag=[_mention()]))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_of_a_remote_user_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `startswith('https://' + SERVER_NAME)` guard. A Mention naming a
    user on another host is not ours to notify.
    """
    reply = _seed_reply()
    _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'},
             {'type': 'Mention', 'href': f'https://{PEER}/u/someone'}],
    ))

    assert db.session.query(Notification).count() == 0


def test_a_reply_mention_with_no_href_notifies_nobody(app, db_session, redis_lock_only_double):
    """`profile_id = json_tag['href'] if 'href' in json_tag else None`, then
    `if profile_id and ...`. A Mention with no href yields None and stops.
    """
    reply = _seed_reply()
    _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, {'type': 'Mention'}],
    ))

    assert db.session.query(Notification).count() == 0


def test_a_reply_mention_of_a_blocked_sender_is_suppressed(app, db_session, redis_lock_only_double):
    """`blocked_users(recipient.id)` -- a recipient who has blocked the
    reply's author gets no notification.

    `make_user_block(blocker, blocked)` inserts `UserBlock(blocker_id=blocker.id,
    blocked_id=blocked.id)` (tests/factories.py), and `blocked_users(user_id)`
    (app/utils.py) filters `UserBlock` on `blocker_id == user_id` and returns
    the `blocked_id`s. Production checks `if reply.user_id not in
    blocked_senders` where `blocked_senders = blocked_users(recipient.id)`, so
    the recipient must be the BLOCKER and the reply's author the BLOCKED --
    confirmed by reading both functions before writing this call.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    author = db.session.query(User).get(reply.user_id)
    make_user_block(recipient, author)

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_second_reply_mention_does_not_duplicate_the_notification(app, db_session, redis_lock_only_double):
    """`existing_notification` -- the same comment mentioning the same user
    twice produces one row, not two.

    Two Updates rather than two tags in one document, and the original reason
    given here -- "the block breaks out per tag" -- was wrong: the mention loop
    contains no `break`, so both tags are processed. The real reason is that
    `existing_notification` cannot see a notification added earlier in the SAME
    call. The app factory constructs SQLAlchemy with
    `session_options={"autoflush": False}` (app/__init__.py), so the first
    tag's `db.session.add(notification)` is still pending and unflushed when
    the second tag runs that query, and a document carrying the same Mention
    twice produces TWO rows -- confirmed by running it. Only a second Update,
    after the first has committed, exercises the check this test is named for.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    document = _update(content='hello',
                       tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()])

    update_post_reply_from_activity(reply, document)
    update_post_reply_from_activity(reply, document)

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 1


def test_a_reply_mention_tag_that_is_not_a_list_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `isinstance(request_json['object']['tag'], list)` conjunct. `tag`
    present with a length greater than 1 by `len()`'s reckoning, but not a
    list, must not reach the `for json_tag in ...` loop.

    An int rather than a string: a two-character string would be silently
    absorbed by the loop (each character fails `'type' in json_tag`'s
    substring check and produces no notification either way, so it would not
    distinguish the guard from its absence). `len()` on an int raises
    `TypeError`, so if the `isinstance` conjunct were dropped this call would
    crash instead of quietly reproducing the guarded no-notification outcome.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(content='hello', tag=42))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_tag_entry_with_no_type_key_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `'type' in json_tag` conjunct. A tag entry with no `type` key at
    all must not reach the `json_tag['type'] == 'Mention'` comparison it
    guards -- the entry that WOULD be a Mention has no `type`, only `href`,
    so the comparison would KeyError if this conjunct were dropped.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'},
             {'href': f'https://test.piefed.local/u/{recipient.user_name}'}],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_tag_of_another_type_is_not_treated_as_a_mention(app, db_session, redis_lock_only_double):
    """The `json_tag['type'] == 'Mention'` comparison. A tag entry that has a
    `type` key and an `href` that would otherwise resolve to the recipient,
    but whose type is not `'Mention'`, must not notify.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'},
             {'type': 'Emoji', 'href': f'https://test.piefed.local/u/{recipient.user_name}'}],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_with_a_case_mismatched_host_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `profile_id.startswith('https://' + SERVER_NAME)` conjunct itself,
    as distinct from the recipient lookup that follows it. A href whose host
    differs from SERVER_NAME only by case fails this case-sensitive
    `startswith` before any lowering happens (`profile_id.lower()` only runs
    INSIDE the guard, once it has already passed).

    A same-host-but-wrong-case href, rather than
    test_a_reply_mention_of_a_remote_user_notifies_nobody's foreign PEER href,
    is what makes this conjunct's effect observable: PEER's href would find
    no matching local recipient regardless of whether this conjunct ran, so
    that test alone does not kill a mutant that drops this conjunct. Here,
    lower-casing the mixed-case href (what the guard's own next line would do
    if it were reached) reproduces the recipient's `ap_profile_id` exactly --
    so if `startswith` were bypassed, this Mention WOULD resolve and notify.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'},
             {'type': 'Mention', 'href': 'https://Test.Piefed.Local/u/localuser'}],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_with_a_non_string_href_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `isinstance(profile_id, str)` conjunct of `if profile_id and
    isinstance(profile_id, str) and profile_id.startswith(...)`. `profile_id`
    truthy alone is not enough -- a non-string href (here, an int, which is
    truthy) must not reach `.startswith(...)`, which would raise
    `AttributeError` on an int if this conjunct were dropped.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, {'type': 'Mention', 'href': 1}],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0

def _seed_microblog_chain(length=2):
    """A comment chain on a microblog instance, with `path` and `parent_id`
    populated by hand.

    Two facts about `path` this helper exists to supply. First, its shape:
    `PostReply.new` (app/models.py) sets `path` to `[0, reply.id]` for a
    top-level comment, and to `parent.path[:] + [reply.id]` for a child -- so a
    chain's path is a leading 0 followed by every ancestor's id in order,
    ending with the row's own id. Second, `tests.factories.make_post_reply`
    sets neither `path` nor `parent_id`; the column has no default, so a
    factory-made reply's `path` is NULL. Production's de-duplication block does
    `for element in reply.path`, which would raise TypeError on None, so this
    helper sets both columns to what `PostReply.new` would have written.

    `length` is the number of comments in the chain. Returns them oldest-first;
    the LAST element is the reply to hand to `update_post_reply_from_activity`,
    and the earlier ones are the ancestors its `path` names.

    Every comment is authored by the post's remote author, not by the local
    recipient and not by the post author-of-record where a test has moved that
    -- each suppression test moves exactly the one row its own rule keys on.
    """
    top = _seed_reply(software='mastodon')
    top.path = [0, top.id]
    db.session.commit()
    chain = [top]
    author = db.session.query(User).get(top.user_id)
    for _ in range(length - 1):
        parent = chain[-1]
        child = make_post_reply(parent.post, author, body='child')
        child.parent_id = parent.id
        child.path = parent.path + [child.id]
        db.session.commit()
        chain.append(child)
    return chain


def _seed_notification(recipient, subtype, targets, url):
    """A pre-existing Notification row for the de-duplication queries to find.

    `url` is passed explicitly because production's own later
    `existing_notification` check filters on `user_id` and `url` only -- not on
    `notif_type`, `subtype` or `targets`. Every row this helper seeds is for
    the recipient under test, so the url is the sole column that keeps that
    check from matching: a seeded row carrying the url of the reply under test
    would suppress the notification through THAT check instead of through the
    de-duplication rule the test is aiming at. Each caller therefore gives its
    seeded row a url belonging to a different object.
    """
    notification = Notification(user_id=recipient.id, title='seeded', url=url,
                                notif_type=NOTIF_MENTION, subtype=subtype,
                                targets=targets)
    db.session.add(notification)
    db.session.commit()
    return notification


def test_a_reply_whose_parent_row_is_missing_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `reply_parent` conjunct of `if reply_parent and profile_id !=
    reply_parent.author.ap_profile_id`, on its own.

    `reply_parent` is `PostReply.query.get(reply.parent_id)` when `parent_id`
    is set, and `reply.post` otherwise -- so the only way it comes back falsy
    is a `parent_id` pointing at a row that is not there. That is what this
    seeds: a `parent_id` no PostReply has.

    Distinguishable from the sibling conjunct's test because dropping
    `reply_parent and` here does not merely let the Mention through -- it
    evaluates `None.author`, so the mutant raises AttributeError rather than
    quietly notifying.
    """
    reply = _seed_reply()
    reply.parent_id = 999999
    db.session.commit()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_of_the_parent_authors_own_profile_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `profile_id != reply_parent.author.ap_profile_id` conjunct -- the
    self-mention exclusion -- on its own.

    A reply with no `parent_id` takes `reply_parent = reply.post`, so the
    profile compared against is the POST author's. Seeded by making the local
    recipient the post's author, which is the shape a microblog reply to a
    local user's post arrives in: the Mention names the person being replied
    to, who does not need telling.

    The instance is left at `_seed_reply`'s 'lemmy' so the de-duplication block
    is skipped: its first rule (`recipient.id == reply.post.user_id`) keys on
    exactly the same row this test moves, and running both would make the kill
    unattributable.
    """
    reply = _seed_reply()
    recipient = _seed_local_recipient()
    reply.post.user_id = recipient.id
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_a_reply_mention_of_an_unregistered_local_name_notifies_nobody(app, db_session, redis_lock_only_double):
    """The `if recipient:` existence check that follows
    `User.query.filter_by(ap_profile_id=profile_id, ap_id=None).first()`.

    The href is on THIS server -- so `startswith('https://' + SERVER_NAME)`
    passes and the case-mismatch test's guard is not what stops it -- but names
    a local user who does not exist. A different local user IS seeded, so the
    lookup is against a populated table rather than an empty one.

    Dropping `if recipient:` makes the next line evaluate `None.id`, so the
    mutant raises AttributeError rather than reproducing this no-notification
    outcome.
    """
    reply = _seed_reply()
    existing = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention(name='nobody')],
    ))

    assert db.session.query(Notification).count() == 0
    assert existing.unread_notifications == 0


def test_a_microblog_reply_mention_still_notifies_when_no_rule_applies(app, db_session, redis_lock_only_double):
    """The de-duplication block runs -- the instance is 'mastodon', which is in
    MICROBLOG_APPS -- and none of its four rules matches, so the notification is
    still created.

    This test exists so the suppression tests below cannot pass for the wrong
    reason: it proves the path reaches the notification at all under the same
    fixture they use. Each of those tests is a "zero notifications" assertion,
    which on its own is satisfied by any fixture that never got near the
    notification.

    A two-comment chain rather than a lone reply, because a lone reply's path
    is `[0, reply.id]` -- both entries skipped by the loop's own `continue` --
    which leaves the fourth rule's `id IN :ids` with an empty tuple. See
    test_a_top_level_microblog_reply_mention_raises_on_its_empty_id_list.
    """
    _, reply = _seed_microblog_chain()
    recipient = _seed_local_recipient()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, notif_type=NOTIF_MENTION).count() == 1


def test_a_microblog_reply_mention_of_the_post_author_is_suppressed(app, db_session, redis_lock_only_double):
    """De-duplication rule 1: `if recipient.id == reply.post.user_id: continue`
    -- "ignore Mention of post author".

    The post's author is moved to the local recipient while every comment in
    the chain keeps the remote author, so the reply's PARENT author is still
    somebody else. That separation is load-bearing: with a top-level reply,
    `reply_parent` is `reply.post` and the earlier self-mention exclusion would
    suppress the Mention before this rule ever ran, and the kill would belong
    to that guard instead.
    """
    _, reply = _seed_microblog_chain()
    recipient = _seed_local_recipient()
    reply.post.user_id = recipient.id
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, subtype='comment_mention').count() == 0


def test_a_microblog_reply_mention_already_sent_as_a_post_mention_is_suppressed(app, db_session, redis_lock_only_double):
    """De-duplication rule 2 -- "ignore Mentions mirroring a Mention made in a
    post body". The query is

        Notification.user_id == recipient.id,
        Notification.notif_type == NOTIF_MENTION,
        Notification.subtype == "post_mention",
        Notification.targets.op("->>")("post_id").cast(Integer) == reply.post_id

    so all four columns must match. `targets` is a JSON column and `->>`
    extracts the value as text before the cast to Integer, so the seeded
    `post_id` is stored as the int it is in production's own `targets_data`.

    The seeded row's url is the post's, not the reply's, so production's later
    `existing_notification` url check cannot be what suppresses this instead.
    The assertion filters on `subtype='comment_mention'` because the seeded row
    is itself a NOTIF_MENTION for this recipient -- an unfiltered count would
    be 1 either way.
    """
    _, reply = _seed_microblog_chain()
    recipient = _seed_local_recipient()
    _seed_notification(recipient, 'post_mention', {'post_id': reply.post_id},
                       f'https://test.piefed.local/post/{reply.post_id}')

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, subtype='comment_mention').count() == 0


def test_a_microblog_reply_mention_is_not_suppressed_by_a_comment_mention_in_the_chain(app, db_session, redis_lock_only_double):
    """De-duplication rule 3 -- "ignore Mentions mirroring a Mention someone
    else made in the comment chain" -- DOES NOT SUPPRESS. This test pins the
    behaviour, it does not endorse it.

    Its `continue` is the last statement of the `for element in reply.path`
    loop body, not of the enclosing `for json_tag in ...` loop:

        for element in reply.path:
            if element == 0 or element == reply.id:
                continue
            ids.append(element)
            notifs = db.session.query(Notification).filter(...).first()
            if notifs:
                continue

    Continuing the innermost loop from its final statement skips nothing, and
    `notifs` is never read after the loop. So the rule finds its row and has no
    effect: execution falls through to rule 4 and then to the notification.

    Seeded so rule 3's query matches exactly -- NOTIF_MENTION,
    subtype 'comment_mention', `targets->>'comment_id'` equal to the ancestor
    whose id is the only entry in `ids` -- and so nothing else suppresses: the
    ancestor is authored by the remote author, so rule 4 does not fire, and the
    seeded row carries the ancestor's url, so the `existing_notification` check
    does not either.
    """
    top, reply = _seed_microblog_chain()
    recipient = _seed_local_recipient()
    _seed_notification(recipient, 'comment_mention', {'comment_id': top.id},
                       f'https://test.piefed.local/comment/{top.id}')

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, url=f'https://test.piefed.local/comment/{reply.id}').count() == 1


def test_a_microblog_reply_mention_of_an_ancestor_comments_author_is_suppressed(app, db_session, redis_lock_only_double):
    """De-duplication rule 4 -- "ignore Mentions generated because a local user
    authored a comment further up in the comment chain":

        ids = tuple(ids)
        user_ids = db.session.execute(text('SELECT user_id FROM "post_reply" WHERE id IN :ids'), {'ids': ids}).scalars()
        if recipient.id in user_ids:
            continue

    `ids` is what the rule-3 loop accumulated: every entry of `reply.path`
    except the leading 0 and the reply's own id -- that is, its ancestors.

    A three-comment chain, with the OLDEST comment reassigned to the local
    recipient. Depth three is required: in a two-comment chain the only
    ancestor is the reply's own parent, and making the recipient its author
    would trip the earlier `profile_id != reply_parent.author.ap_profile_id`
    exclusion first, so the kill would belong to that guard. Here the parent
    (the middle comment) keeps the remote author, and the post's author is
    untouched, so rules 1 and 2 do not fire either.
    """
    top, _, reply = _seed_microblog_chain(3)
    recipient = _seed_local_recipient()
    top.user_id = recipient.id
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, subtype='comment_mention').count() == 0


def test_a_top_level_microblog_reply_mention_raises_on_its_empty_id_list(app, db_session, redis_lock_only_double):
    """A BUG, pinned rather than endorsed, and the reason every other test here
    uses a chain.

    A top-level comment's `path` is `[0, reply.id]` (`PostReply.new`,
    app/models.py). Rule 3's loop skips both entries -- `if element == 0 or
    element == reply.id: continue` -- so `ids` is still empty when rule 4 does
    `ids = tuple(ids)` and interpolates it into `WHERE id IN :ids`. psycopg2
    renders the empty tuple as a literal `()`, which Postgres rejects, so the
    whole Update handler dies with a ProgrammingError before the notification
    is reached.

    Reaching that line needs all of: an UPDATE (this function is the edit path
    only -- creates go through the other copy of this block, at
    app/activitypub/util.py:2705-2736, whose loop is written correctly); a
    `tag` list of length greater than one, since a lone Mention never enters
    the block at all (test_a_lone_reply_mention_is_ignored pins that); a
    mentioned local user who is NOT the post's author, because for a top-level
    reply `reply_parent` is `reply.post` and the self-mention exclusion catches
    that case first; and no pre-existing `post_mention` notification for that
    recipient and post, which rule 2 would have caught. So the accurate scope
    is every EDIT of a top-level microblog comment carrying two or more tags
    that Mentions a local user other than the post's author. Narrower than
    "every top-level microblog comment", but not exotic.

    The session is left unusable by the failed statement, so the assertion that
    nothing was written needs a rollback first.
    """
    reply = _seed_reply(software='mastodon')
    reply.path = [0, reply.id]
    db.session.commit()
    recipient = _seed_local_recipient()

    with pytest.raises(ProgrammingError):
        update_post_reply_from_activity(reply, _update(
            content='hello',
            tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
        ))

    db.session.rollback()
    assert db.session.query(Notification).filter_by(user_id=recipient.id).count() == 0


def test_an_mbin_reply_mention_of_the_post_author_is_suppressed(app, db_session, redis_lock_only_double):
    """The `reply.instance.software == 'mbin'` disjunct of the gate on the
    whole de-duplication block, which is
    `software == 'mbin' or software in MICROBLOG_APPS`.

    'mbin' is NOT in MICROBLOG_APPS (app/constants.py lists mastodon, misskey,
    akkoma, iceshrimp, pleroma, fedibird), so this disjunct is the only way an
    mbin instance reaches the block -- and every other test in this group opts
    in through the other disjunct, with 'mastodon'. Without this test the
    'mbin' comparison can be deleted with the suite green.

    Rule 1 is the suppression used to make the gate observable; it is covered
    on its own in
    test_a_microblog_reply_mention_of_the_post_author_is_suppressed.
    """
    _, reply = _seed_microblog_chain()
    reply.instance.software = 'mbin'
    recipient = _seed_local_recipient()
    reply.post.user_id = recipient.id
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, subtype='comment_mention').count() == 0


def test_a_microblog_reply_mention_of_its_own_author_is_still_notified(app, db_session, redis_lock_only_double):
    """The `element == reply.id` disjunct of the path loop's skip:

        for element in reply.path:
            if element == 0 or element == reply.id:
                continue
            ids.append(element)

    The reply's own id is the last entry of its own `path`, and this disjunct
    keeps it out of `ids` -- so rule 4, which suppresses when the recipient
    authored any comment whose id is in `ids`, is asking about ancestors only
    and not about the reply being updated.

    Seeded with the recipient as the author of the reply itself and of nothing
    else in the chain: production notifies, and a mutant that drops this
    disjunct puts `reply.id` into `ids`, whereupon rule 4 finds the recipient
    and suppresses. Pins the current behaviour rather than asserting it is
    desirable.
    """
    _, reply = _seed_microblog_chain()
    recipient = _seed_local_recipient()
    reply.user_id = recipient.id
    db.session.commit()

    update_post_reply_from_activity(reply, _update(
        content='hello',
        tag=[{'type': 'Hashtag', 'name': '#x'}, _mention()],
    ))

    assert db.session.query(Notification).filter_by(
        user_id=recipient.id, subtype='comment_mention').count() == 1


def test_a_post_markdown_source_sets_body_and_html(app, db_session, redis_lock_only_double):
    """`update_post_from_activity`'s `source` arm -- the first branch of the
    content elif chain. Unlike the reply function, which always computes an
    html-derived body first and lets `source` overwrite it, this function's
    `source` check runs BEFORE any html handling: `content` is read only for
    the outer `is not None` guard, never allowlisted, when a markdown
    `source` is present.

    `content` and `source['content']` are deliberately different strings, so
    the assertion tells which one produced `body`.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/markdown', 'content': 'from markdown'},
        type='Note',
    ))

    assert post.body == 'from markdown'
    assert 'from markdown' in post.body_html
    assert 'from html' not in post.body_html


def test_a_post_source_that_is_not_markdown_falls_through_to_the_next_arm(app, db_session, redis_lock_only_double):
    """The source guard's `source['mediaType'] == 'text/markdown'` conjunct's
    False side. A `source` that IS a dict but carries the wrong `mediaType`
    must fall past the `source` arm; with no object-level `mediaType` either,
    it lands in the `else` wrap-and-allowlist arm and keeps the html-derived
    body.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='<p>from html</p>',
        source={'mediaType': 'text/plain', 'content': 'from markdown'},
        type='Note',
    ))

    assert post.body == 'from html'


def test_a_post_source_that_is_not_a_dict_falls_through_to_the_wrap(app, db_session, redis_lock_only_double):
    """The source guard's `isinstance(..., dict)` conjunct.

    The reply function's analogous guard has a fourth conjunct,
    `'mediaType' in request_json['object']['source']`, which is why a string
    `source` there proves nothing about `isinstance` (`'mediaType' in
    'a string'` is a harmless substring test). This function's guard has NO
    such conjunct -- past `isinstance` it goes straight to the subscript
    `request_json['object']['source']['mediaType']` -- so a string `source`
    already distinguishes the two: with `isinstance` in place (production)
    the guard short-circuits to `else` before that subscript ever runs; with
    it removed, `'not a dict'['mediaType']` raises `TypeError` (string
    indices must be integers), not a graceful `False`. So this one fixture
    proves the conjunct on its own -- no `source=None` companion test is
    needed the way the reply suite needed one.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='<p>from html</p>', source='not a dict', type='Note',
    ))

    assert post.body == 'from html'


def test_a_post_html_media_type_allowlists_the_content(app, db_session, redis_lock_only_double):
    """The object-level `mediaType: text/html` arm, which the reply function
    has no counterpart for. `body` is derived from the allowlisted html.

    Content is pre-wrapped in `<p>` here on purpose: this test alone does
    NOT prove the arm still exists (the `else` arm would wrap-then-allowlist
    the same already-wrapped string to the identical result), it only proves
    what the arm computes. See the next test for the one that tells this arm
    apart from `else`.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='<p>plain html</p>', mediaType='text/html', type='Note',
    ))

    assert post.body_html == '<p>plain html</p>'
    assert post.body == 'plain html'


def test_a_post_html_media_type_skips_the_wrap_that_else_would_apply(app, db_session, redis_lock_only_double):
    """The `mediaType: text/html` arm's actual distinguishing behaviour: it
    calls `allowlist_html` directly on `content`, with none of the `else`
    arm's `<p>`-wrap step first.

    Content here does NOT start with `<p>` or `<blockquote>`, so the two
    arms diverge: this arm leaves it unwrapped (`allowlist_html('plain
    html')` returns `'plain html'` unchanged), while `else` would wrap it to
    `'<p>plain html</p>'` before allowlisting. That divergence is what a
    mutant deleting this elif arm (falling through to `else`) breaks, and
    what the previous test's already-wrapped fixture could not detect.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='plain html', mediaType='text/html', type='Note',
    ))

    assert post.body_html == 'plain html'


def test_a_post_markdown_media_type_renders_the_content(app, db_session, redis_lock_only_double):
    """The object-level `mediaType: text/markdown` arm. `content` IS the
    markdown here, so `body` keeps it verbatim and `body_html` is rendered.

    `else` would wrap-then-allowlist `'**bold**'` to the literal
    `'<p>**bold**</p>'` -- allowlist_html does not render markdown syntax --
    so `'<strong>'` only appears here if this arm actually ran.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='**bold**', mediaType='text/markdown', type='Note',
    ))

    assert post.body == '**bold**'
    assert '<strong>' in post.body_html or '<b>' in post.body_html


def test_a_post_unrecognized_media_type_falls_through_to_the_wrap(app, db_session, redis_lock_only_double):
    """An object-level `mediaType` that matches neither `text/html` nor
    `text/markdown` falls all the way to `else`, exactly as if no
    `mediaType` had been supplied at all.

    This is the one fixture that tells the markdown arm's
    `mediaType == 'text/markdown'` conjunct apart from a mutant that keeps
    only the `'mediaType' in ...` membership half: with the equality
    dropped, ANY present `mediaType` (here `'text/plain'`) would satisfy the
    weakened guard and route through `markdown_to_html` instead of the wrap.
    `markdown_to_html('bare words')` renders to `'<p>bare words</p>\\n'`
    (trailing newline, confirmed by calling it directly) where `else`'s
    wrap-then-allowlist produces `'<p>bare words</p>'` (no newline) -- an
    exact `==` is needed to catch that difference; `in` would not.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='bare words', mediaType='text/plain', type='Note',
    ))

    assert post.body_html == '<p>bare words</p>'
    assert post.body == 'bare words'


def test_a_post_already_wrapped_content_is_not_double_wrapped(app, db_session, redis_lock_only_double):
    """The `startswith('<p>')` half of the `else` arm's wrap guard.

    The two `source`-fallthrough tests above also reach `else` with content
    already starting `<p>`, but both assert only `post.body`: html_to_text
    strips tags regardless of how many `<p>` wrappers surround the text, so
    `html_to_text('<p><p>from html</p></p>')` reads back as `'from html'`
    same as the correctly-single-wrapped case -- a double-wrap mutant on this
    disjunct passes both of those tests. `body_html` is the one place a
    double wrap is visible, so it is what this test asserts.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(content='<p>hello</p>', type='Note'))

    assert post.body_html == '<p>hello</p>'


def test_a_post_bare_content_is_wrapped_and_allowlisted(app, db_session, redis_lock_only_double):
    """The `else` arm's wrap. No `source`, no `mediaType`, and content that
    starts with neither `<p>` nor `<blockquote>`.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(content='bare words', type='Note'))

    assert post.body_html == '<p>bare words</p>'


def test_a_post_blockquote_content_is_not_wrapped(app, db_session, redis_lock_only_double):
    """The `<blockquote>` disjunct of the wrap guard, which needs its own
    test or it can be deleted with the `<p>` case still passing.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(content='<blockquote>q</blockquote>', type='Note'))

    assert post.body_html.startswith('<blockquote>')


def test_a_post_with_null_content_keeps_its_body(app, db_session, redis_lock_only_double):
    """THE ASYMMETRY, on the side that gets it right. This function guards
    `request_json['object']['content'] is not None`; the reply function does
    not, and Task 10 fixes that.

    The seeded body is asserted unchanged, which is what distinguishes "the
    guard skipped the arm" from "the arm ran and wrote None". A mutant that
    drops this conjunct reaches the `else` arm's `content.startswith(...)`
    with `content` still `None`, which raises `AttributeError` rather than
    producing a wrong value -- this test kills that mutant by crash, not by
    assertion.
    """
    post = _seed_post()
    post.body = 'seeded body'
    db.session.commit()

    update_post_from_activity(post, _update(content=None, name='a title', type='Note'))

    assert post.body == 'seeded body'


def test_a_post_missing_content_key_leaves_the_body_untouched(app, db_session, redis_lock_only_double):
    """The outer guard's `'content' in request_json['object']` conjunct,
    which `test_a_post_with_null_content_keeps_its_body` does not cover: that
    test supplies `content` as an explicit `None`, this one omits the key
    altogether. A mutant that drops this conjunct, leaving only `... is not
    None`, evaluates `request_json['object']['content']` unconditionally and
    raises `KeyError` on this fixture -- another crash-kill, distinct from
    the explicit-`None` one.

    Both `body` and `body_html` are asserted, since the whole content block
    -- not just the wrap step -- must be skipped.
    """
    post = _seed_post()
    post.body = 'seeded body'
    post.body_html = '<p>seeded body html</p>'
    db.session.commit()

    update_post_from_activity(post, _update(name='a title', type='Note'))

    assert post.body == 'seeded body'
    assert post.body_html == '<p>seeded body html</p>'


def test_a_post_name_sets_the_title_and_clears_microblog(app, db_session, redis_lock_only_double):
    """The `'name' in` arm. `microblog` is seeded True so the `= False` is
    observable rather than matching the column's own default.
    """
    post = _seed_post()
    post.microblog = True
    db.session.commit()

    update_post_from_activity(post, _update(name='A New Title', content='x', type='Note'))

    assert post.title == 'A New Title'
    assert post.microblog is False


def test_a_post_with_no_name_autogenerates_a_microblog_title(app, db_session, redis_lock_only_double):
    """The `else` arm. With no `name`, the title comes from
    `microblog_content_to_title(post.body_html)` and `microblog` is set True.

    The content is NOT the brief's original 'short': `microblog_content_to_title`
    only reprocesses its extracted `<p>` text when it finds `.`/`?`/`!`
    punctuation, or when the no-punctuation text is at least 10 characters
    long; below that it falls back to the fixed string
    '(content in post body)', which is 22 characters -- already past the
    `len(...) < 20` gate this test means to put on its True side. Content
    ending in a period keeps the punctuation-clipped path instead: 'Hi
    there.' clips to 'Hi there' (8 characters, confirmed by direct
    measurement), which does clear `< 20`.
    """
    post = _seed_post()
    post.microblog = False
    db.session.commit()

    update_post_from_activity(post, _update(content='Hi there.', type='Note'))

    assert post.title == '[Microblog] Hi there'
    assert post.microblog is True


def test_a_long_autogenerated_title_is_not_prefixed(app, db_session, redis_lock_only_double):
    """The `len(autogenerated_title) < 20` gate's False side. This content has
    no `.`/`?`/`!`, so `microblog_content_to_title` keeps it whole (49
    characters, confirmed by direct measurement) rather than falling back to
    the fixed short string -- comfortably past 20, so the prefix is dropped.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(
        content='a considerably longer sentence than the other one', type='Note',
    ))

    assert not post.title.startswith('[Microblog] ')
    assert post.title == 'a considerably longer sentence than the other one'


def test_an_autogenerated_title_of_exactly_twenty_chars_is_not_prefixed(app, db_session, redis_lock_only_double):
    """The `< 20` gate's exact boundary. 'this is twenty chars' clips to
    itself unchanged at exactly 20 characters (confirmed by direct
    measurement, no `.`/`?`/`!` present). `test_a_long_autogenerated_title_is_not_prefixed`
    and `test_a_post_with_no_name_autogenerates_a_microblog_title` sit at 49
    and 8 characters respectively -- neither distinguishes `< 20` from `<= 20`.
    This one does: under the real `<` operator 20 is not less than 20, so no
    prefix is added; a boundary mutant widening the comparison to `<=` would
    add one here where the unmutated code does not.
    """
    post = _seed_post()

    update_post_from_activity(post, _update(content='this is twenty chars', type='Note'))

    assert not post.title.startswith('[Microblog] ')
    assert post.title == 'this is twenty chars'


def test_a_post_with_no_anchor_in_its_content_leaves_the_url_untouched(app, db_session, redis_lock_only_double):
    """The `else` arm's `if link != '':` gate. `microblog_content_to_title`
    returns `link = ''` when the extracted text carries no anchor, and that
    must skip the `post.url = ...` write entirely -- `url_is_parseable('')`
    is never even called, since it sits behind this same gate.

    `type='Video'` here, not this file's usual `'Note'`, and that substitution
    is deliberate, not decorative: a plain Note reaches the function's later
    Links section, which reads `old_url = post.url` and then unconditionally
    recomputes `new_url` from `request_json['object']['attachment']` (absent
    here, so `new_url` starts `None`) and, since `old_url != new_url`,
    overwrites `post.url` regardless of what the title block just did --
    confirmed by running this test body with `type='Note'` first, which failed
    with `post.url` reset to `None` even though the `link != ''` gate itself
    correctly skipped its write. Video returns early (this file's own
    `... commit(); return` two blocks down) before that section is ever
    reached, the same escape `tests/test_unparseable_url_ingress.py`'s
    `TestFederationDoesNotStoreAnUnparseableMicroblogLink` uses for the same
    write. That file always supplies an anchor (`link != ''`) and compares
    what gets stored when it does; nothing there exercises this guard's skip
    side, which is the ground this test covers. `post.url` is seeded to a
    value distinct from both `''` and `None` so a write of either becomes
    visible against it.
    """
    post = _seed_post()
    post.url = 'https://original.example/keep'
    db.session.commit()

    update_post_from_activity(post, _update(content='Hi there.', type='Video'))

    assert post.url == 'https://original.example/keep'


def test_an_nsfl_keyword_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The keyword scan behind `if old_title != new_title:`. Seeded False --
    the column default -- but the title is also seeded DIFFERENT from the
    document's, which is the part that makes the gate open.

    Covers the `[NSFL]` disjunct only; `(NSFL)` and `[COMBAT]` are separate,
    non-overlapping literal substrings that a disjunct-deletion mutant can
    drop independently of this one, so each gets its own test below.
    """
    post = _seed_post()
    post.nsfl = False
    db.session.commit()

    update_post_from_activity(post, _update(name='[NSFL] a title', content='x', type='Note'))

    assert post.nsfl is True


def test_an_nsfl_keyword_parenthesized_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The `(NSFL)` disjunct. Distinct literal from `[NSFL]` and `[COMBAT]` --
    none is a substring of another -- so a mutant deleting only this disjunct
    leaves the other two tests passing and needs this test to be caught.
    """
    post = _seed_post()
    post.nsfl = False
    db.session.commit()

    update_post_from_activity(post, _update(name='(NSFL) a title', content='x', type='Note'))

    assert post.nsfl is True


def test_an_nsfl_combat_keyword_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The `[COMBAT]` disjunct, the third and last of the NSFL scan."""
    post = _seed_post()
    post.nsfl = False
    db.session.commit()

    update_post_from_activity(post, _update(name='[COMBAT] a title', content='x', type='Note'))

    assert post.nsfl is True


def test_an_nsfw_keyword_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The NSFW half of the same scan. Separate from NSFL so each keyword
    branch has its own kill. Covers the `(NSFW)` disjunct; `[NSFW]` is the
    other, non-overlapping literal and gets its own test below.
    """
    post = _seed_post()
    post.nsfw = False
    db.session.commit()

    update_post_from_activity(post, _update(name='(NSFW) a title', content='x', type='Note'))

    assert post.nsfw is True


def test_an_nsfw_bracketed_keyword_in_a_new_title_sets_the_flag(app, db_session, redis_lock_only_double):
    """The `[NSFW]` disjunct, the other half of the NSFW scan."""
    post = _seed_post()
    post.nsfw = False
    db.session.commit()

    update_post_from_activity(post, _update(name='[NSFW] a title', content='x', type='Note'))

    assert post.nsfw is True


def test_an_unchanged_title_does_not_rescan_for_keywords(app, db_session, redis_lock_only_double):
    """The `old_title != new_title` gate itself. A document repeating the
    title the post already has must not reach the scan -- proved by seeding a
    title that CONTAINS a keyword and a flag that is False, and asserting the
    flag stayed False.

    This is the only test that distinguishes the gate from an unconditional
    scan.
    """
    post = _seed_post()
    post.title = '[NSFL] already'
    post.nsfl = False
    db.session.commit()

    update_post_from_activity(post, _update(name='[NSFL] already', content='x', type='Note'))

    assert post.nsfl is False


def test_a_sensitive_flag_overrides_the_title_scan(app, db_session, redis_lock_only_double):
    """`sensitive` is applied AFTER the keyword scan, so a document with an
    NSFW title and `sensitive: false` ends up False.

    That ordering is the whole point of the test -- asserting False against a
    title that would have set True is what proves which ran last.
    """
    post = _seed_post()
    post.nsfw = False
    db.session.commit()

    update_post_from_activity(post, _update(
        name='[NSFW] a title', content='x', sensitive=False, type='Note',
    ))

    assert post.nsfw is False


def test_an_nsfl_key_is_applied(app, db_session, redis_lock_only_double):
    """The `'nsfl' in` application, seeded to the opposite value."""
    post = _seed_post()
    post.nsfl = True
    db.session.commit()

    update_post_from_activity(post, _update(name='a title', content='x', nsfl=False, type='Note'))

    assert post.nsfl is False
