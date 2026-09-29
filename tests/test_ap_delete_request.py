"""`process_delete_request`: a remote user who deleted their account.

Twenty statements, none executed by any test before this file, on a **destructive**
federated path -- it marks a user deleted, runs `delete_dependencies()`, and for
some activities calls `purge_content()`, which deletes files from disk and the CDN.

WHAT THE INBOX HAS ALREADY DECIDED before this runs, which is why the `if user:`
here is defensive rather than a gate:
`shared_inbox` (app/activitypub/routes.py:697-703) matches a self-delete --
`type == 'Delete'`, a string `object`, and `actor == object` -- looks the actor up
itself, and on a miss logs `APLOG_DELETE, APLOG_IGNORED, 'Does not exist here'` and
returns 200. So the unknown-actor case never reaches this function through the
inbox, which is also why it holds the round's only `log_incoming_ap` call.

THE PURGE CONDITION is the part worth pinning:

    if ('removeData' in request_json and request_json['removeData'] is True) \\
            or user.created_very_recently():
        user.purge_content()

`is True`, not truthy: a peer sending `"removeData": "true"` or `1` does NOT get a
purge. That is the conservative direction for a destructive operation and is
asserted as such. `created_very_recently()` is `created > utcnow() - 1 day`, so a
brand-new account's content is purged whether or not the peer asked -- the
spam-account case, where the deleting party is usually the spammer.

FOUR SWEEPS CAME BACK CLEAN while this round was looking for a defect, recorded so
they are not repeated:

* every `{% extends 'themes/' + theme() + '/base.html' %}` is a data-driven
  template path like D1393's, but `User.theme` and `Site.default_theme` are
  `SelectField`s whose choices come from `theme_list()`, and WTForms' `pre_validate`
  refuses anything else. The peer-controlled `Community.theme`
  (`app/activitypub/util.py:961`, `:1542`) is guarded by `file_exists(...)` at both
  use sites;
* `instance_banned(instance.inbox)` at `app/activitypub/routes.py:1992` is the only
  one of ~40 call sites not passed a domain, which looks exactly like a defect. It
  is correct: `instance_banned` normalises through `inbox_domain`, whose docstring
  says it "accepts both forms because callers hold values from either source";
* the account-deletion twins agree where it matters. The local path
  (`app/admin/util.py:26`) sets `banned = true` as well as `deleted`, which this one
  does not -- a remote user cannot log in here, so banning them is meaningless --
  and it sets `deleted_by` at its route (`app/admin/routes.py:2166`) rather than in
  the task.
"""
import pytest
from flask import g

from app import db
from app.activitypub.routes import process_delete_request
from app.models import ActivityPubLog, Post, Site, User, utcnow
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')
PEER = 'peer.delete.test'


@pytest.fixture
def env(app, db_session):
    """A remote user with a post, plus a local bystander whose content must be
    untouched."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance('test.piefed.local')
    make_user(local, 'founder', local=True)          # burns id 1
    peer = make_instance(PEER)
    remote = make_user(peer, 'departing')
    remote.ap_profile_id = f'https://{PEER}/u/departing'
    remote.ap_public_url = f'https://{PEER}/u/departing'
    bystander = make_user(peer, 'staying')
    bystander.ap_profile_id = f'https://{PEER}/u/staying'
    community = make_community('general')
    db.session.commit()
    make_community_member(remote, community)
    their_post = make_post(community, remote, f'https://{PEER}/p/1',
                           title='THEIRPOST')
    other_post = make_post(community, bystander, f'https://{PEER}/p/2',
                           title='OTHERPOST')
    db.session.commit()
    g.admin_ids = []
    return remote, bystander, their_post, other_post


def a_delete(actor=f'https://{PEER}/u/departing', **extra):
    body = {'id': f'https://{PEER}/activities/delete/1', 'type': 'Delete',
            'actor': actor, 'object': actor}
    body.update(extra)
    return body


# --------------------------------------------------------------------------
# The ordinary self-delete
# --------------------------------------------------------------------------


@pytest.fixture
def logging_on(app, monkeypatch):
    """`log_incoming_ap` writes nothing unless `LOG_ACTIVITYPUB_TO_DB` is on
    (app/activitypub/util.py:4918), and the test config leaves it off -- which
    `admin_activities` warns about on its own page. A log assertion without this
    passes for the wrong reason.
    """
    monkeypatch.setitem(app.config, 'LOG_ACTIVITYPUB_TO_DB', True)


class TestASelfDelete:
    def test_the_user_is_marked_deleted(self, app, env):
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(), store_ap_json=False)

        db.session.expire_all()
        assert db.session.get(User, remote.id).deleted is True

    def test_it_is_recorded_as_their_own_doing(self, app, env):
        """`deleted_by = user.id`. `app/api/alpha/views.py:57` distinguishes
        content deleted by its author from content deleted by somebody else with
        exactly this comparison, so a self-delete has to say so."""
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(), store_ap_json=False)

        db.session.expire_all()
        row = db.session.get(User, remote.id)
        assert row.deleted_by == row.id

    def test_the_bystander_is_untouched(self, app, env):
        """The same instance, the same community. A delete that took the whole
        peer's users with it would pass every row above."""
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(), store_ap_json=False)

        db.session.expire_all()
        assert db.session.get(User, bystander.id).deleted is not True

    def test_it_is_logged_as_a_success(self, app, env, logging_on):
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(), store_ap_json=False)

        logged = ActivityPubLog.query.all()
        assert len(logged) == 1
        assert logged[0].activity_id == f'https://{PEER}/activities/delete/1'

    def test_the_activity_is_stored_when_asked(self, app, env, logging_on):
        """`store_ap_json` decides whether the body is kept for the admin
        activity viewer; both arms are the caller's choice, not this function's."""
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(), store_ap_json=True)

        assert ActivityPubLog.query.one().activity_json is not None

    def test_the_activity_is_not_stored_otherwise(self, app, env, logging_on):
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(), store_ap_json=False)

        assert ActivityPubLog.query.one().activity_json is None


# --------------------------------------------------------------------------
# The purge condition
# --------------------------------------------------------------------------


class TestWhenTheContentIsPurged:
    def _aged(self, user, days):
        user.created = utcnow() - __import__('datetime').timedelta(days=days)
        db.session.commit()

    def test_remove_data_true_purges(self, app, env):
        remote, bystander, their_post, other_post = env
        self._aged(remote, 30)
        post_id = their_post.id

        process_delete_request(a_delete(removeData=True), store_ap_json=False)

        db.session.expire_all()
        post = db.session.get(Post, post_id)
        assert post is None or post.deleted is True

    def test_an_established_account_keeps_its_content_without_remove_data(
            self, app, env):
        """The other side: a Delete with no `removeData` from an account older
        than a day marks the user deleted and leaves their posts alone."""
        remote, bystander, their_post, other_post = env
        self._aged(remote, 30)
        post_id = their_post.id

        process_delete_request(a_delete(), store_ap_json=False)

        db.session.expire_all()
        post = db.session.get(Post, post_id)
        assert post is not None and post.deleted is not True

    def test_a_brand_new_account_is_purged_anyway(self, app, env):
        """`or user.created_very_recently()` -- under a day old, so the content
        goes whether the peer asked or not. The spam case: the account that is
        deleting itself is usually the one that made the mess."""
        remote, bystander, their_post, other_post = env
        self._aged(remote, 0)
        post_id = their_post.id

        process_delete_request(a_delete(), store_ap_json=False)

        db.session.expire_all()
        post = db.session.get(Post, post_id)
        assert post is None or post.deleted is True

    @pytest.mark.parametrize('value', ['true', 'True', 1, ['yes'], {}, 'false',
                                       0, None])
    def test_only_a_real_true_purges(self, app, env, value):
        """`request_json['removeData'] is True`, not truthy. A peer sending the
        string `"true"` or `1` does not get a purge -- the conservative direction
        for an operation that deletes files from disk and the CDN, and worth
        pinning because `is True` reads like a mistake until you ask which way it
        should fail.
        """
        remote, bystander, their_post, other_post = env
        self._aged(remote, 30)
        post_id = their_post.id

        process_delete_request(a_delete(removeData=value), store_ap_json=False)

        db.session.expire_all()
        post = db.session.get(Post, post_id)
        assert post is not None and post.deleted is not True

    def test_the_user_is_deleted_either_way(self, app, env):
        """Whatever happens to the content, the account goes."""
        remote, bystander, their_post, other_post = env
        self._aged(remote, 30)

        process_delete_request(a_delete(removeData='not a bool'),
                               store_ap_json=False)

        db.session.expire_all()
        assert db.session.get(User, remote.id).deleted is True


# --------------------------------------------------------------------------
# The defensive arm
# --------------------------------------------------------------------------


class TestAnActorThisInstanceDoesNotKnow:
    def test_nothing_happens_and_nothing_raises(self, app, env):
        """`if user:`. Unreachable through `shared_inbox`, which refuses an
        unknown self-delete at :700-703 and logs `'Does not exist here'` -- but
        `process_delete_request` is also called directly (`:755`, `:757`) and by
        `replay_inbox_request`, so the guard earns its place.
        """
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(actor=f'https://{PEER}/u/nobody'),
                               store_ap_json=False)

        db.session.expire_all()
        assert db.session.get(User, remote.id).deleted is not True

    def test_it_is_not_logged_as_a_success(self, app, env, logging_on):
        """The one `log_incoming_ap` call sits inside `if user:`, so an actor that
        resolves to nothing produces no log line from here. That is the inbox's
        job, and it does it before dispatching."""
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(actor=f'https://{PEER}/u/nobody'),
                               store_ap_json=False)

        assert ActivityPubLog.query.count() == 0

    def test_the_actor_is_matched_case_insensitively(self, app, env):
        """`filter_by(ap_profile_id=user_ap_id.lower())` -- a peer may publish its
        actor id with different casing than the one stored."""
        remote, bystander, their_post, other_post = env

        process_delete_request(a_delete(actor=f'https://{PEER.upper()}/u/DEPARTING'),
                               store_ap_json=False)

        db.session.expire_all()
        assert db.session.get(User, remote.id).deleted is True
