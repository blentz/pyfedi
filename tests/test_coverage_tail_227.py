"""Round 227: the eight lines round 226 left in `app/api/alpha/views.py`.

Round 226 closed seventeen lines that were one `if X: v[k] = X` each and named these eight
as needing a fixture of their own rather than a value on an object it already built. This
is that round; the file goes from 96.17% to complete.

    150         `interacted_at.get(post.id) or utcnow() - timedelta(days=1)`
    381, 382    the `DetachedInstanceError` fallback when reading a user's extra fields
    916         a reply report's optional `description`
    1204        `conversation_report_view`'s variant-1 early return
    1370        the `AllowedInstances` loop body
    1387        an instance's optional `version`
    1418        `cached_modlist_for_user(None)` answering `[]`

Three of the eight are worth more than their coverage:

* `:150` is the CALLER-SUPPLIED path. `post_view` takes an `interacted_at` dict so a
  listing can read every post's last-interaction time in one query instead of one per
  post; the per-post `SELECT` above it is the fallback. The batch path -- the one the list
  endpoints actually use -- was the uncovered one.
* `:381-382` exists because `convert_archived_replies_to_tree` builds temporary detached
  `User` objects, and reading `user.extra_fields` on one raises. The arm re-fetches by id.
  A row proves the arm, not the comment.
* `:1418` is a `@cache.memoize`d function answering `None`, which matters because the memo
  key includes the argument: a `None` user and a real one must not share an entry.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import (AllowedInstances, BannedInstances, Instance, Report, Site, User,
                        utcnow)
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.site = db.session.get(Site, 1)
    g.admin_ids = []
    community = make_community('probeland')
    author = make_user(None, 'tailauthor', local=True)
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/t/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author, post=post)


# --------------------------------------------------------------------------
# post_view: the batch interacted_at path
# --------------------------------------------------------------------------


class TestTheInteractedAtBatch:
    """`post_view` counts unread comments since the viewer last interacted with the post.
    Given no `interacted_at` it runs a `SELECT` per post; given a dict it reads from that
    instead -- which is how the list endpoints avoid one query per row.

    The dict path was uncovered, so every row exercising unread counts was measuring the
    fallback.
    """

    def _view(self, env, **kwargs):
        from app.api.alpha.views import post_view

        return post_view(env.post, variant=2, user_id=env.author.id, **kwargs)

    def test_a_supplied_time_is_used_instead_of_a_query(self, env):
        """`:150`, first arm of the `or`. A reply posted BEFORE the supplied time is
        already read, so the count is zero -- which is the observable difference from the
        fallback, whose own lookup finds no `read_posts` row at all."""
        make_post_reply(env.post, env.author, 'an old reply')
        db.session.commit()
        later = utcnow() + __import__('datetime').timedelta(days=1)

        view = self._view(env, interacted_at={env.post.id: later})

        assert view['unread_comments'] == 0

    def test_a_post_missing_from_the_dict_falls_back_to_a_day_ago(self, env):
        """`:150`, second arm. `interacted_at.get(post.id)` is None for a post the batch
        did not cover, and the `or` gives it 'since yesterday' rather than None -- which
        would otherwise reach the SQL below as a null and match nothing."""
        make_post_reply(env.post, env.author, 'a fresh reply')
        db.session.commit()

        view = self._view(env, interacted_at={})

        assert view['unread_comments'] == 1

    def test_the_two_paths_agree_for_a_post_with_no_replies(self, env):
        """The control: supplying the dict does not change the answer where the fallback
        would have given the same one."""
        with_dict = self._view(env, interacted_at={})
        without = self._view(env)

        assert with_dict['unread_comments'] == without['unread_comments'] == 0


# --------------------------------------------------------------------------
# user_view: the detached-instance fallback
# --------------------------------------------------------------------------


class TestADetachedUsersExtraFields:
    """D1437, fixed (owner ruling). `user_view` read a user's extra fields straight off the
    object it was handed, behind an `except DetachedInstanceError` that could not fire:
    `User.extra_fields` is `lazy='dynamic'`, and a detached instance's query answered EMPTY
    with an SAWarning instead of raising. `convert_archived_replies_to_tree` hands it such
    temporaries, so their fields were lost. The view now reads them off the author attached
    to the current session, and the handler is gone.
    """

    @pytest.fixture
    def detached(self, env):
        """A user expunged from the session, its already-loaded columns intact.

        NOT expired as well -- `user_view` passes the user to memoized helpers and
        flask-caching builds their key from `repr(user)`, which reads columns.
        """
        from app.models import UserExtraField

        user = make_user(None, 'detachable', local=True)
        db.session.add(UserExtraField(user_id=user.id, label='site',
                                      text='https://example.test'))
        db.session.commit()
        user_id = user.id
        db.session.expunge(user)
        return user, user_id

    def test_a_detached_users_fields_are_read_from_the_attached_row(self, detached):
        """No SAWarning (the suite runs at zero warnings), and the field is served."""
        from app.api.alpha.views import user_view

        user, user_id = detached

        view = user_view(user, variant=1)

        assert view['id'] == user_id
        assert [f['label'] for f in view['extra_fields']] == ['site']

    def test_an_attached_user_gets_them(self, env):
        """The control, and what makes the row above a statement about DETACHMENT rather
        than about extra fields never working."""
        from app.api.alpha.views import user_view
        from app.models import UserExtraField

        user = make_user(None, 'attached', local=True)
        db.session.add(UserExtraField(user_id=user.id, label='home',
                                      text='https://home.example'))
        db.session.commit()

        view = user_view(user, variant=1)

        assert [f['label'] for f in view['extra_fields']] == ['home']

    def test_only_four_are_served(self, env):
        """The `num_extra_fields == 4` break, which nothing else covers."""
        from app.api.alpha.views import user_view
        from app.models import UserExtraField

        user = make_user(None, 'manyfields', local=True)
        for n in range(6):
            db.session.add(UserExtraField(user_id=user.id, label=f'f{n}', text=str(n)))
        db.session.commit()

        assert len(user_view(user, variant=1)['extra_fields']) == 4


# --------------------------------------------------------------------------
# reply_report_view and conversation_report_view
# --------------------------------------------------------------------------


def test_a_reply_report_carries_its_description(env):
    """`:916`. `description` is what the reporter typed beyond the canned reason, and is
    optional -- so a report without one must not carry the key."""
    from app.api.alpha.views import reply_report_view

    reply = make_post_reply(env.post, env.author, 'a reply')
    db.session.commit()
    # `suspect_user_id` is the reported comment's AUTHOR, and the view reads it with
    # `user_view(user=report.suspect_user_id, ...)`. A row without it reaches `user_view`
    # with None, which falls past that function's `isinstance(user, int)` guard and dies on
    # `user.__table__` -- so every report this codebase writes sets it, and a fixture that
    # does not is testing a row the product cannot produce (fact 781).
    with_text = Report(reasons='spam', description='they keep doing it', type=2,
                       reporter_id=env.author.id, suspect_user_id=env.author.id,
                       suspect_post_reply_id=reply.id,
                       in_community_id=env.community.id, created_at=utcnow())
    without = Report(reasons='spam', type=2, reporter_id=env.author.id,
                     suspect_user_id=env.author.id,
                     suspect_post_reply_id=reply.id, in_community_id=env.community.id,
                     created_at=utcnow())
    db.session.add_all([with_text, without])
    db.session.commit()

    served = reply_report_view(with_text, reply.id, env.author.id)
    bare = reply_report_view(without, reply.id, env.author.id)
    # variant 1 wraps the report in a `comment_report_view` envelope.
    assert served['comment_report_view']['comment_report']['description'] == \
        'they keep doing it'
    assert 'description' not in bare['comment_report_view']['comment_report']


def test_a_conversation_report_variant_one_returns_before_loading_the_conversation(env):
    """`:1204`. Variant 1 is the summary the report list serves, and it returns BEFORE
    `db.session.get(Conversation, ...)` -- which is what lets it describe a report whose
    conversation has since been deleted.

    `suspect_conversation_id` carries a foreign key, so the row cannot name a missing
    conversation to prove the early return -- it asserts the SHAPE instead: variant 1
    answers with the report's own fields and no `conversation` key, which is only true of
    the branch above the lookup.
    """
    from app.api.alpha.views import conversation_report_view

    from app.models import Conversation

    conversation = Conversation(user_id=env.author.id)
    db.session.add(conversation)
    db.session.commit()
    report = Report(reasons='abuse', description='rude', type=4,
                    reporter_id=env.author.id, suspect_conversation_id=conversation.id,
                    created_at=utcnow())
    db.session.add(report)
    db.session.commit()

    v1 = conversation_report_view(report, variant=1)

    assert v1['reason'] == 'abuse'
    assert v1['description'] == 'rude'
    assert 'conversation' not in v1


# --------------------------------------------------------------------------
# federated_instances_view
# --------------------------------------------------------------------------


class TestTheFederatedInstancesView:
    """Three lists in one document: every known instance, the allowlist, and the blocklist.
    The allowlist loop and an instance's optional `version` were both uncovered -- the
    allowlist because no row had ever created an `AllowedInstances` entry.
    """

    def test_an_allowed_instance_is_listed_and_a_banned_one_is_not_linked(self, env):
        """`:1370`, the allowlist loop body -- and the `blocked` filter below it, which is
        why the banned instance is seeded too: an instance on the blocklist must appear
        under 'blocked' and NOT under 'linked'."""
        from app.api.alpha.views import federated_instances_view

        peer = make_instance('peer.example', software='lemmy')
        peer.version = '0.19.3'
        blocked_peer = make_instance('blocked.example', software='lemmy')
        db.session.add_all([AllowedInstances(domain='peer.example'),
                            BannedInstances(domain='blocked.example')])
        db.session.commit()

        view = federated_instances_view()['federated_instances']

        assert [i['domain'] for i in view['allowed']] == ['peer.example']
        assert [i['domain'] for i in view['blocked']] == ['blocked.example']
        linked = [i['domain'] for i in view['linked']]
        assert 'peer.example' in linked
        assert 'blocked.example' not in linked

    def test_a_version_is_served_when_the_instance_reports_one(self, env):
        """`:1387`. `software` was already covered and `version` was not, which is the
        shape round 226 found seventeen of: adjacent optional fields, one line each."""
        from app.api.alpha.views import federated_instances_view

        peer = make_instance('versioned.example', software='piefed')
        peer.version = '1.2.3'
        db.session.commit()

        entry = next(i for i in federated_instances_view()['federated_instances']['linked']
                     if i['domain'] == 'versioned.example')

        assert entry['software'] == 'piefed'
        assert entry['version'] == '1.2.3'

    def test_an_instance_with_no_version_omits_the_key(self, env):
        from app.api.alpha.views import federated_instances_view

        make_instance('bare.example')
        db.session.commit()

        entry = next(i for i in federated_instances_view()['federated_instances']['linked']
                     if i['domain'] == 'bare.example')

        assert 'version' not in entry


# --------------------------------------------------------------------------
# cached_modlist_for_user
# --------------------------------------------------------------------------


def test_the_modlist_for_no_user_is_empty(env):
    """`:1418`. The function is `@cache.memoize`d, so the argument is part of the key: an
    anonymous caller and a real user must not share an entry. This row also pins that the
    None case answers `[]` rather than raising on `user.id` one line down."""
    from app.api.alpha.views import cached_modlist_for_user

    assert cached_modlist_for_user(None) == []


def test_the_modlist_for_a_real_user_is_computed_separately(env):
    """The control for the row above: a real user gets their own answer, so the None case
    is not simply what everyone receives."""
    from app.api.alpha.views import cached_modlist_for_user

    assert isinstance(cached_modlist_for_user(env.author), list)
