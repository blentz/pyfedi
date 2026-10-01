"""`announce_activity_to_followers`: who a local community's Announce reaches.

The main fan-out. Every activity a local community relays -- a post, a vote, a lock, a
delete, a report -- goes through this function, and what it decides is the recipient
list. Fifteen of its statements had no test, including the whole of the branch that
makes a **report** go to fewer instances than everything else.

THE CLAIM THAT MATTERS. A `Flag` names a suspect and a reporter. It is fanned out to
the instances hosting the community's MODERATORS, plus the instance hosting the
reported author so that its own admins can act -- and NOT to every instance with a
subscriber, which is where an ordinary post goes:

    if is_flag:
        instances = community.following_instances(include_dormant=True, mod_hosts_only=True)
        if admin_instance_id != 1 and not any(i.id == admin_instance_id for i in instances):
            admin_instance = db.session.get(Instance, admin_instance_id)
            if admin_instance:
                instances.append(admin_instance)
    else:
        instances = community.following_instances(include_dormant=True)

`mod_hosts_only=True` adds `CommunityMember.is_moderator == True` to the join
(app/models.py:1426). Dropping it -- one keyword -- broadcasts every report to every
subscribing instance, and nothing in the suite noticed. The caller side is already
covered: tests/test_inbox_dispatch_misc.py asserts the dispatcher passes
`is_flag=True` and `admin_instance_id=reported.author.instance_id`. This file is the
other half, the function that acts on them.

`admin_instance_id` is the REPORTED AUTHOR's instance, not the reporter's -- the
dispatcher passes `reported.author.instance_id`. That reads oddly against the
parameter's name and is deliberate: the suspect's home admins are the people who can
act on a report about their own user, and it is what Lemmy does. Pinned below rather
than repaired, since the name is the only thing wrong with it.

WHAT IS PATCHED, AND WHY. `send_to_remote_instance_fast` and
`HttpSignature.signed_request` are the two sends, and both would leave the process;
they are recorded instead. `instance_banned` does a Redis-backed lookup through
`get_task_session`, so it is patched to a predicate this file controls. Nothing else
is doubled: the instance rows, the memberships and the `ActivityBatch` writes are real.
"""
import json

import pytest
from unittest.mock import patch

from flask import g

from app import db
from app.models import ActivityBatch, Instance, Site
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session):
    """A local community with four remote member instances:

    * `mod_host`   -- hosts a moderator
    * `plain_host` -- hosts an ordinary member, which is what separates a Flag's
                      recipients from everything else's
    * `suspect_host` -- hosts the reported author, an ordinary member
    * `creator_host` -- hosts the account that sent the activity being relayed
    """
    from types import SimpleNamespace
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    g.admin_ids = []
    db.session.commit()
    local = make_instance(HOST, software='piefed')
    # An inbox on the LOCAL instance, so that appending it to the recipient list
    # would produce an observable send. `make_instance` leaves `inbox` None, and the
    # loop skips a host without one -- so a mutant dropping the `admin_instance_id != 1`
    # guard was invisible until this row existed.
    local.inbox = f'https://{HOST}/inbox'
    make_user(local, 'founder', local=True)
    community = make_community('general')
    db.session.commit()

    hosts = {}
    for name in ('mod_host', 'plain_host', 'suspect_host', 'creator_host'):
        instance = make_instance(f'{name.replace("_", "-")}.test', software='lemmy')
        instance.inbox = f'https://{name.replace("_", "-")}.test/inbox'
        hosts[name] = instance
    db.session.commit()

    moderator = make_user(hosts['mod_host'], 'amod')
    plain = make_user(hosts['plain_host'], 'aplain')
    suspect = make_user(hosts['suspect_host'], 'asuspect')
    creator = make_user(hosts['creator_host'], 'acreator')
    db.session.commit()
    make_community_member(moderator, community, is_moderator=True)
    make_community_member(plain, community)
    make_community_member(suspect, community)
    make_community_member(creator, community)
    db.session.commit()

    return SimpleNamespace(community=community, creator=creator, suspect=suspect,
                           moderator=moderator, plain=plain, local=local, **hosts)


def _announce(env, activity=None, banned_inboxes=(), **kwargs):
    """Call the function and report the inboxes it sent to.

    Returns (inboxes, async_calls, published) so a test can say WHERE an activity
    went as well as whether it went at all.
    """
    from app.activitypub.routes import announce_activity_to_followers

    sent, async_calls, published, woken = [], [], [], []

    def record_fast(inbox, private_key, key_id, payload):
        sent.append(inbox)

    def record_signed(inbox, payload, private_key, key_id, send_via_async=False):
        async_calls.append(inbox)
        return (inbox, {'Signature': 'x'}, json.dumps(payload).encode())

    class _Redis:
        def publish(self, channel, body):
            published.append((channel, json.loads(body)))

    document = activity if activity is not None else {
        '@context': ['https://www.w3.org/ns/activitystreams'],
        'id': 'https://creator-host.test/activities/1', 'type': 'Create',
        'actor': 'https://creator-host.test/u/acreator',
        'object': {'id': 'https://creator-host.test/p/1', 'type': 'Page'}}

    real_awaken = routes_awaken()

    def record_awaken(instance):
        woken.append(instance)
        return real_awaken(instance)

    with patch('app.activitypub.routes.send_to_remote_instance_fast',
               side_effect=record_fast), \
            patch('app.activitypub.routes.instance_banned',
                  side_effect=lambda inbox: inbox in banned_inboxes), \
            patch('app.activitypub.routes.awaken_dormant_instance',
                  side_effect=record_awaken), \
            patch('app.activitypub.signature.HttpSignature.signed_request',
                  side_effect=record_signed), \
            patch('app.redis_client', _Redis()):
        announce_activity_to_followers(env.community, env.creator, document, **kwargs)

    # Every recipient list is checked for Nones, in every call: `if admin_instance:`
    # keeps one out of the list, and the two guards below it -- this function's own
    # `if instance and ...` and the loop's -- would hide one silently. A list holding
    # None is the defect that guard prevents, so the list is what is asserted.
    assert all(entry is not None for entry in woken), woken

    return sent, async_calls, published


def routes_awaken():
    from app.utils import awaken_dormant_instance
    return awaken_dormant_instance


# --------------------------------------------------------------------------
# The two head guards
# --------------------------------------------------------------------------


class TestWhenNothingIsAnnounced:
    def test_a_remote_community_relays_nothing(self, env):
        """`if not community.is_local(): return`. A community another server
        publishes is not ours to fan out for -- its own instance does that."""
        env.community.ap_id = 'general@peer.test'
        env.community.ap_profile_id = 'https://peer.test/c/general'
        db.session.commit()
        assert env.community.is_local() is False

        sent, _, _ = _announce(env)

        assert sent == []

    def test_a_banned_creator_relays_nothing(self, env):
        """`if creator.banned: return`, checked before the recipient list is built,
        so a banned account's activity reaches nobody rather than reaching everyone
        minus its own host."""
        env.creator.banned = True
        db.session.commit()

        sent, _, _ = _announce(env)

        assert sent == []

    def test_an_unbanned_creator_on_a_local_community_relays(self, env):
        """The control for both rows above."""
        sent, _, _ = _announce(env)

        assert sorted(sent) == ['https://mod-host.test/inbox',
                                'https://plain-host.test/inbox',
                                'https://suspect-host.test/inbox']


# --------------------------------------------------------------------------
# The Flag restriction
# --------------------------------------------------------------------------


class TestAReport:
    def test_a_report_skips_an_instance_that_only_hosts_a_subscriber(self, env):
        """THE CLAIM. `plain_host` has a member of this community and no moderator,
        so it gets every ordinary activity and must not get the report.

        Dropping `mod_hosts_only=True` broadcasts every report to every subscribing
        instance, which is what this row exists to catch.
        """
        sent, _, _ = _announce(env, is_flag=True,
                              admin_instance_id=env.suspect.instance_id)

        assert 'https://plain-host.test/inbox' not in sent

    def test_a_report_reaches_the_moderators_host(self, env):
        sent, _, _ = _announce(env, is_flag=True,
                              admin_instance_id=env.suspect.instance_id)

        assert 'https://mod-host.test/inbox' in sent

    def test_a_report_reaches_the_reported_authors_host(self, env):
        """`admin_instance_id` is the REPORTED AUTHOR's instance -- the dispatcher
        passes `reported.author.instance_id` -- so the suspect's own admins are told
        about a report against their user. That is why `suspect_host` is here at all,
        having no moderator."""
        sent, _, _ = _announce(env, is_flag=True,
                              admin_instance_id=env.suspect.instance_id)

        assert 'https://suspect-host.test/inbox' in sent

    def test_an_ordinary_activity_reaches_all_three(self, env):
        """The control that makes the three rows above mean something: without it, a
        fix that sent reports nowhere would pass every one of them."""
        sent, _, _ = _announce(env)

        assert 'https://plain-host.test/inbox' in sent
        assert 'https://mod-host.test/inbox' in sent
        assert 'https://suspect-host.test/inbox' in sent

    def test_a_local_admin_instance_is_not_appended(self, env):
        """`if admin_instance_id != 1`. Instance 1 is this server; appending it would
        send the community's own Announce to its own inbox.

        `following_instances` filters `Instance.id != 1`, so the `not any(...)` half is
        True for the local instance and the `!= 1` half is the only thing stopping it.
        The fixture gives the local instance an inbox for exactly this row -- without
        one the loop skips it anyway and the operand is unobservable.
        """
        sent, _, _ = _announce(env, is_flag=True, admin_instance_id=1)

        assert f'https://{HOST}/inbox' not in sent
        assert sorted(sent) == ['https://mod-host.test/inbox']

    def test_an_admin_instance_that_already_hosts_a_moderator_is_not_doubled(
            self, env):
        """`not any(i.id == admin_instance_id for i in instances)`. The suspect's host
        is often a mod host too, and the same inbox must not be sent to twice."""
        sent, _, _ = _announce(env, is_flag=True,
                              admin_instance_id=env.moderator.instance_id)

        assert sent.count('https://mod-host.test/inbox') == 1
        assert sorted(sent) == ['https://mod-host.test/inbox']

    def test_an_admin_instance_id_naming_no_instance_is_not_appended(self, env):
        """`if admin_instance:`. The id comes from a row the dispatcher read, and the
        guard is there for the case it names nothing.

        Dropping it does not crash -- `awaken_dormant_instance` opens
        `if instance and not instance.gone_forever:` and the send loop opens
        `if instance and ...`, so a None in the list is silently skipped twice. What
        it does is put a None in the recipient list, which `_announce` asserts against
        on every call. Without that assertion this row passed under the mutation.
        """
        sent, _, _ = _announce(env, is_flag=True, admin_instance_id=999999)

        assert sorted(sent) == ['https://mod-host.test/inbox']


# --------------------------------------------------------------------------
# Which instances in the list are actually sent to
# --------------------------------------------------------------------------


class TestTheRecipientFilter:
    def test_the_creators_own_instance_is_skipped(self, env):
        """`if creator.instance_id != instance.id` -- the host that sent us the
        activity already has it. `creator_host` is a member instance and appears in
        no assertion above for this reason; this row is where it is named."""
        sent, _, _ = _announce(env)

        assert 'https://creator-host.test/inbox' not in sent

    def test_an_instance_with_no_inbox_is_skipped(self, env):
        env.plain_host.inbox = None
        db.session.commit()

        sent, _, _ = _announce(env)

        assert sorted(sent) == ['https://mod-host.test/inbox',
                                'https://suspect-host.test/inbox']

    def test_a_defederated_instance_is_skipped(self, env):
        """`not instance_banned(instance.inbox)`. The one call site of ~40 that is
        handed an inbox url rather than a domain, which round 198 recorded as correct
        because `instance_banned` normalises through `inbox_domain`."""
        sent, _, _ = _announce(env,
                               banned_inboxes={'https://plain-host.test/inbox'})

        assert 'https://plain-host.test/inbox' not in sent
        assert 'https://mod-host.test/inbox' in sent

    def test_an_instance_that_is_gone_forever_is_skipped(self, env):
        """`instance.online()` is `not (self.dormant or self.gone_forever)`, and
        `following_instances` already filters `gone_forever` out of the query -- so
        this row proves the pair agree rather than either alone."""
        env.plain_host.gone_forever = True
        db.session.commit()

        sent, _, _ = _announce(env)

        assert 'https://plain-host.test/inbox' not in sent

    def test_a_dormant_instance_is_in_the_list_and_skipped_by_online(self, env):
        """`include_dormant=True` puts it in the list, `online()` keeps it out of the
        send, and `awaken_dormant_instance` is what may change that in between. The
        two are deliberately not the same test: the list is built to include dormant
        hosts precisely so one can be woken.
        """
        env.plain_host.dormant = True
        env.plain_host.start_trying_again = None
        db.session.commit()

        sent, _, _ = _announce(env)

        assert 'https://plain-host.test/inbox' not in sent

    def test_a_dormant_instance_whose_wait_has_passed_is_woken_and_sent_to(self, env):
        """The reason `include_dormant=True` is there at all.
        `awaken_dormant_instance` clears `dormant` when `start_trying_again` has
        passed, and it runs BEFORE `online()` is asked -- so a host that has slept
        long enough receives this same activity rather than the next one."""
        from app.models import utcnow
        from datetime import timedelta
        env.plain_host.dormant = True
        env.plain_host.gone_forever = False
        env.plain_host.start_trying_again = utcnow() - timedelta(days=1)
        db.session.commit()

        sent, _, _ = _announce(env)

        assert 'https://plain-host.test/inbox' in sent
        assert db.session.get(Instance, env.plain_host.id).dormant is False


# --------------------------------------------------------------------------
# The three ways an activity leaves
# --------------------------------------------------------------------------


class TestHowItIsSent:
    def _vote(self):
        return {'@context': ['https://www.w3.org/ns/activitystreams'],
                'id': 'https://creator-host.test/activities/2', 'type': 'Like',
                'actor': 'https://creator-host.test/u/acreator',
                'object': 'https://test.piefed.local/p/1'}

    def test_a_batchable_activity_to_a_piefed_host_is_queued_not_sent(self, env):
        """`can_batch and (software == 'piefed' or software == 'pylova')` writes an
        `ActivityBatch` row instead of sending, which is how votes are collapsed."""
        env.plain_host.software = 'piefed'
        db.session.commit()

        sent, _, _ = _announce(env, can_batch=True)

        assert 'https://plain-host.test/inbox' not in sent
        queued = ActivityBatch.query.filter_by(instance_id=env.plain_host.id).all()
        assert len(queued) == 1
        assert queued[0].community_id == env.community.id

    def test_pylova_batches_as_well(self, env):
        env.plain_host.software = 'pylova'
        db.session.commit()

        sent, _, _ = _announce(env, can_batch=True)

        assert 'https://plain-host.test/inbox' not in sent
        assert ActivityBatch.query.filter_by(
            instance_id=env.plain_host.id).count() == 1

    def test_another_software_is_sent_to_even_when_batching_is_allowed(self, env):
        """The control for the software half of that condition: `lemmy` cannot read a
        batch, so it gets the activity directly."""
        assert env.plain_host.software == 'lemmy'

        sent, _, _ = _announce(env, can_batch=True)

        assert 'https://plain-host.test/inbox' in sent
        assert ActivityBatch.query.count() == 0

    def test_a_piefed_host_is_sent_to_when_batching_is_not_allowed(self, env):
        """And the control for the other half. `can_batch` is the caller's decision --
        only votes and likes pass it -- so a post to a piefed host goes directly."""
        env.plain_host.software = 'piefed'
        db.session.commit()

        sent, _, _ = _announce(env, can_batch=False)

        assert 'https://plain-host.test/inbox' in sent
        assert ActivityBatch.query.count() == 0

    def test_a_vote_goes_through_the_notif_server_when_one_is_configured(
            self, env, monkeypatch):
        """`NOTIF_SERVER and is_vote(...)`: the signed requests are collected and
        published to Redis in one message rather than sent one at a time. `is_vote`
        reads the ANNOUNCE wrapper, so the inner object's type is what decides it."""
        from flask import current_app
        monkeypatch.setitem(current_app.config, 'NOTIF_SERVER', 'notifs.test')

        sent, async_calls, published = _announce(env, activity=self._vote())

        assert sent == []
        assert sorted(async_calls) == ['https://mod-host.test/inbox',
                                       'https://plain-host.test/inbox',
                                       'https://suspect-host.test/inbox']
        assert len(published) == 1
        channel, body = published[0]
        assert channel == 'http_posts:activity'
        assert sorted(body['urls']) == sorted(async_calls)

    def test_a_non_vote_is_sent_directly_even_with_a_notif_server(self, env,
                                                                monkeypatch):
        """The control for the `is_vote` half."""
        from flask import current_app
        monkeypatch.setitem(current_app.config, 'NOTIF_SERVER', 'notifs.test')

        sent, async_calls, published = _announce(env)

        assert async_calls == []
        assert published == []
        assert 'https://mod-host.test/inbox' in sent

    def test_a_vote_is_sent_directly_when_no_notif_server_is_configured(self, env):
        """And for the other half. The suite's config leaves `NOTIF_SERVER` unset,
        which is why every other row here reaches `send_to_remote_instance_fast`."""
        sent, async_calls, published = _announce(env, activity=self._vote())

        assert async_calls == []
        assert published == []
        assert 'https://mod-host.test/inbox' in sent


# --------------------------------------------------------------------------
# The envelope
# --------------------------------------------------------------------------


class TestTheAnnounceItself:
    def _captured(self, env, activity):
        from app.activitypub.routes import announce_activity_to_followers
        payloads = []

        with patch('app.activitypub.routes.send_to_remote_instance_fast',
                   side_effect=lambda inbox, key, key_id, payload: payloads.append(payload)), \
                patch('app.activitypub.routes.instance_banned', return_value=False):
            announce_activity_to_followers(env.community, env.creator, activity)
        assert payloads
        return payloads[0]

    def test_the_inner_object_loses_its_context(self, env):
        """The inner object of an Announce must not carry its own context -- the
        envelope supplies one. D138, fixed: it used to be deleted from the caller's
        dict in place; the caller's activity is now left as it was."""
        activity = {'@context': ['https://www.w3.org/ns/activitystreams'],
                    'id': 'https://creator-host.test/activities/3',
                    'type': 'Create', 'actor': 'https://creator-host.test/u/acreator',
                    'object': {'id': 'https://creator-host.test/p/2', 'type': 'Page'}}

        announce = self._captured(env, activity)

        assert '@context' not in announce['object']
        assert '@context' in announce
        assert activity['@context'] == ['https://www.w3.org/ns/activitystreams']

    def test_an_activity_with_no_context_is_announced_unchanged(self, env):
        """The `if '@context' in activity` guard: a peer that sent none is not a
        KeyError."""
        activity = {'id': 'https://creator-host.test/activities/4', 'type': 'Create',
                    'actor': 'https://creator-host.test/u/acreator',
                    'object': {'id': 'https://creator-host.test/p/3'}}

        announce = self._captured(env, activity)

        assert announce['object'] == activity

    def test_the_envelope_names_the_community_as_the_actor(self, env):
        """The community relays, so the community is the actor -- not the creator.
        A peer checks the signature against this."""
        announce = self._captured(env, {'id': 'x', 'type': 'Create', 'object': {}})

        assert announce['actor'] == env.community.public_url()
        assert announce['type'] == 'Announce'
        assert announce['to'] == ['https://www.w3.org/ns/activitystreams#Public']
        assert announce['cc'] == [f'{env.community.public_url()}/followers']
        assert announce['id'].startswith(
            'https://test.piefed.local/activities/announce/')
