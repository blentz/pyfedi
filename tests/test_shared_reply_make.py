"""Group B of app/shared/reply.py -- creating and editing a reply.

    make_reply   :156   42 statements / 22 arcs
    edit_reply   :216   29 / 12

Both were at ZERO coverage when this file was created. Every line number here
was re-derived with numbered output at the commit each task's report names.

WHERE THE HARNESS COMES FROM. `tests/test_shared_reply_interactions.py`
(Groups A and C) and `tests/test_shared_reply_moderation.py` (Groups E and F)
cover the rest of this module; `tests/test_shared_post_make.py` covers the
twins `make_post` (app/shared/post.py:175) and `edit_post` (`:262`).

MAKE_REPLY'S WEIGHT IS `PostReply.new`, NOT `make_reply`. The function itself
is plain control flow over rows -- no upload pipeline, no URL classification.
`PostReply.new` runs the blocked-phrase, gif-reaction and low-effort filters,
builds the `path` array, moves three counters, recomputes
`reply_count_cross_posted`, and raises `PostReplyValidationError` from SEVEN
places, not the five the plan named -- re-enumerated against app/models.py at
the commit this task's report names:

    :2983  'Comments are disabled on this post'  (not post.comments_enabled)
    :2986  'Banned from commenting'               (user.ban_comments)
    :3025  'Blocked phrase in comment'
    :3036  'Replier blocked'
    :3039  'Duplicate reply'
    :3046  'Gif comment ignored'   (gated on site.enable_gif_reply_rep_decrease)
    :3049  'Low quality reply'     (gated on site.enable_this_comment_filter)

PROBE A's finding, for Task 4: both `enable_gif_reply_rep_decrease` and
`enable_this_comment_filter` are `db.Column(db.Boolean, default=False)`
(app/models.py:3967,3969) -- NEITHER defaults True. And when no Site row
exists, `:3041-3043` builds `site = Site()` WITHOUT adding or flushing it, so
its boolean columns hold Python `None` (the ORM applies a column `default=`
at flush time, not at bare construction) -- also falsy. So the Gif and
Low-quality raises are unreachable from a factory seed unless a test builds
a Site row and explicitly sets the flag True; every other one of the seven
is reachable with plain factory rows.

PROBE B's finding: `make_reply` calls `user_ip_banned()` at `:174` and
`ip_address()` directly at `:201`. Both resolve to the SAME function --
app/shared/reply.py imports `ip_address` from `app.utils`, which is
`app.utils.ip_address = get_ip_address` (app/__init__.py), the identical
function `user_ip_banned` calls internally. Group A already established that
`get_ip_address` catches `RuntimeError` (no app/request context) and returns
`''`. So SRC_API needs no request context at `:174` OR at `:201` -- same
function, same guard, confirmed rather than assumed.

PROBE C's finding: `reply_already_exists` (app/utils.py:2599) counts
non-deleted `post_reply` rows matching `user_id`, `post_id`, `parent_id` AND
`body`. A fixture seeding two replies with different bodies (or different
authors, or different posts) does not trip it by accident; only an identical
resubmission does.

NO `Language` ROW IS NEEDED ANYWHERE IN THIS FILE. `make_user` leaves
`language_id` and `interface_language` as `None`, so `get_recipient_language`
takes its `'en'` branch and `Language.query.get(...)` is structurally
unreachable on that path -- established in sub-project 41 and re-verified by
its reviewer.

PLAN ASSUMPTION FALSIFIED, FIXED HERE: the plan's `edit_reply` payload/form
used `language_id=2`. `edit_reply:238` writes that value straight onto
`reply.language_id`, an FK to `language.id` (app/models.py:2926), and no row
2 exists -- the run failed both tests with
`ForeignKeyViolation: Key (language_id)=(2) is not present in table
"language"`. `language_id` is nullable (no `nullable=False` at :2926), so
both tests below pass `language_id=None` instead; neither asserts on
`language_id`, only on `body` and `edited_at`, so nothing is lost.

EVERY SEEDED ID IS OFFSET, AND THAT IS LOAD-BEARING RATHER THAN TIDY.
Register entry D533 records that sub-project 41's fixture left
`community.id == post.id == author.id == 1`, which hid at least four mutants:
several call sites resolve objects to ids, so a wrong-object-right-id mutation
is invisible. `_seed_for_reply` below burns rows to force the three sequences
apart and asserts they differ.
"""

import pytest
from types import SimpleNamespace

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Notification, PostReply, PostReplyValidationError
from app.shared.reply import edit_reply, make_reply
from tests.factories import (
    bearer, make_community, make_community_member, make_instance, make_post,
    make_post_reply, make_site, make_user, web_ctx,
)


def _seed_for_reply(*, private=True, community_name='replies'):
    """One instance, two users, one community, one post, one existing reply.

    `author` owns the seeded reply; `actor` is a second user for the tests
    that need a non-author. They are DISTINCT, which `edit_reply:218`'s
    `id_match=reply.user_id` makes load-bearing.

    THE IDS ARE FORCED APART -- see the module docstring and D533. A spare
    community and a spare post are created and left unused purely to advance
    those sequences past the user sequence, and the assertion below fails
    loudly if a future factory change makes them collide again.

    `private=True` sets `community.private`, register entry D393(d)'s
    federation lever: it stops the eager Celery task bodies at their first
    guard so no test issues an outbound request.
    """
    instance = make_instance('local.example', software='piefed')
    author = make_user(instance, 'author', local=True)
    actor = make_user(instance, 'actor', local=True)
    make_community('sequence-burner-one')
    make_community('sequence-burner-two')
    community = make_community(community_name)
    community.private = private
    db.session.commit()
    make_post(community, author, 'https://local.example/burner')
    post = make_post(community, author, 'https://local.example/p/1')
    reply = make_post_reply(post, author)
    db.session.commit()
    assert len({community.id, post.id, author.id}) == 3, (
        'D533: seeded ids collided again -- a wrong-object-right-id mutation '
        'would be invisible'
    )
    return SimpleNamespace(instance=instance, author=author, actor=actor,
                           community=community, post=post, reply=reply)


def make_moderator(s, user=None):
    """Make `user` (default `s.actor`) a moderator of `s.community`.

    `Community.moderators()` (app/models.py:716-722) filters
    `is_banned == False`, so a banned CommunityMember is NOT a moderator.
    """
    return make_community_member(user or s.actor, s.community, is_moderator=True)


class TestEditReply:
    """`edit_reply` (app/shared/reply.py:216-251)."""

    def test_the_api_arm_edits_the_body_and_returns_the_pair(self, db_session):
        """`:217` true, `:218`-`:222`, the writes at `:233`-`:238`, `:249`.

        Asserts `body` AND `edited_at`, because `:233`-`:238` write six
        attributes unconditionally and any single one of them would pass with
        the other five deleted.
        """
        s = _seed_for_reply()
        payload = {'body': 'edited through the api', 'notify_author': True,
                   'language_id': None, 'distinguished': False}

        user_id, reply = edit_reply(payload, s.reply, s.post, SRC_API,
                                    auth=bearer(s.author))

        assert user_id == s.author.id
        db.session.refresh(s.reply)
        assert 'edited through the api' in s.reply.body
        assert s.reply.edited_at is not None
        assert s.reply.notify_author is True

    def test_the_web_arm_reads_the_form_and_returns_none(self, db_session, app):
        """`:217` false -> `:227`-`:231`, `:243`-`:244`'s flash, `:251`.

        `web_ctx` takes the app fixture FIRST -- `web_ctx(app, user)`,
        tests/factories.py:1217. Sub-project 41's plan wrote `web_ctx(user)`
        five times and every occurrence was wrong.
        """
        from flask import get_flashed_messages
        s = _seed_for_reply()
        form = SimpleNamespace(
            body=SimpleNamespace(data='edited through the web'),
            notify_author=SimpleNamespace(data=False),
            language_id=SimpleNamespace(data=None),
            distinguished=SimpleNamespace(data=False),
        )

        with web_ctx(app, s.author):
            result = edit_reply(form, s.reply, s.post, SRC_WEB, auth=None)
            messages = get_flashed_messages()

        assert result is None
        assert 'Your changes have been saved.' in messages
        db.session.refresh(s.reply)
        assert 'edited through the web' in s.reply.body
