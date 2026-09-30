"""Round 258: who may invite somebody to a community, and what it publishes about its flair.

Four clusters on `Community` in `app/models.py`.

    can_invite          three settings deciding who may bring somebody in -- members,
                        moderators, or the owner only. It is an authorisation check, and each
                        setting is one line.
    is_member           the anonymous arm, which every one of those settings is read through
    humanize_subscribers  the count shown on a community's page, with a `total` switch and a
                        caller-supplied override
    flair_for_ap        two OUTGOING formats, `lemmy:CommunityTag` and `CommunityPostTag`,
                        chosen by a `version` argument -- two spellings of the same five
                        fields, so a peer reading one gets nothing from the other

`can_invite` is the one that matters: a community that restricted invitations to its owner is
relying on those three lines, and a wrong one lets any member invite.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.constants import (INVITE_MEMBERS_ONLY, INVITE_MODS_ONLY, INVITE_OWNER_ONLY)
from app.models import Community, CommunityMember, Site
from tests.factories import (make_community, make_community_flair, make_community_member,
                             make_instance, make_post, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('inviteland')
    owner = make_user(api_baseline.instance_local, 'the_owner', local=True)
    moderator = make_user(api_baseline.instance_local, 'the_mod', local=True)
    member = make_user(api_baseline.instance_local, 'a_member', local=True)
    stranger = make_user(api_baseline.instance_local, 'a_stranger', local=True)
    db.session.commit()
    owner_row = make_community_member(owner, community, is_moderator=True)
    owner_row.is_owner = True
    make_community_member(moderator, community, is_moderator=True)
    make_community_member(member, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, owner=owner,
                           moderator=moderator, member=member, stranger=stranger,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# Who may invite somebody
# --------------------------------------------------------------------------


class TestWhoMayInviteSomebody:
    """Each setting is one line, and each line is `not is_<role>(u)` -- so a mistake in any of
    them widens the permission rather than narrowing it.
    """

    def _can(self, env, who):
        return env.community.can_invite(who)

    def test_with_no_restriction_anybody_signed_in_may_invite(self, env):
        """`invitations` unset. A community that has not chosen a setting is open, so the row
        for a STRANGER is the one that says so."""
        env.community.invitations = None
        db.session.commit()

        assert self._can(env, env.stranger) is True

    def test_members_only_admits_members_and_refuses_strangers(self, env):
        env.community.invitations = INVITE_MEMBERS_ONLY
        db.session.commit()

        assert self._can(env, env.member) is True
        assert self._can(env, env.stranger) is False

    def test_mods_only_refuses_an_ordinary_member(self, env):
        """The middle setting. A member passing `is_member` is not enough here, which is the
        whole difference between the two settings -- so the row uses the account the previous
        one admitted."""
        env.community.invitations = INVITE_MODS_ONLY
        db.session.commit()

        assert self._can(env, env.moderator) is True
        assert self._can(env, env.member) is False

    def test_owner_only_refuses_a_moderator(self, env):
        """The narrowest setting, and the same argument one step further: a moderator passes
        `is_moderator` and must still be refused."""
        env.community.invitations = INVITE_OWNER_ONLY
        db.session.commit()

        assert self._can(env, env.owner) is True
        assert self._can(env, env.moderator) is False

    def test_an_anonymous_visitor_may_never_invite(self, env):
        """`if user is None and current_user.is_anonymous`. Reached with no argument at all,
        which is how every template calls it."""
        env.community.invitations = None
        db.session.commit()

        with env.app.test_request_context('/'):
            assert env.community.can_invite() is False

    def test_an_anonymous_visitor_is_not_a_member(self, env):
        """`is_member`'s own first line. It is read by the members-only arm above, and
        `current_user.get_id()` for an anonymous visitor is not a user id -- so answering
        anything but False here would hand the permission to everybody."""
        with env.app.test_request_context('/'):
            from flask_login import current_user

            assert env.community.is_member(current_user) is False


# --------------------------------------------------------------------------
# The number on the community's page
# --------------------------------------------------------------------------


class TestTheSubscriberCountShown:

    @pytest.fixture(autouse=True)
    def a_locale(self, env):
        """`humanize_number` formats through `format_compact_decimal(value, locale=g.locale)`,
        and `g.locale` is set by `before_request` -- which these direct calls do not run."""
        g.locale = 'en'
        yield
        if hasattr(g, 'locale'):
            del g.locale

    def test_the_total_across_instances_is_shown_by_default(self, env):
        """`total=True` reads `total_subscriptions_count`, which is this instance's count plus
        what the community's own server reports -- the number a reader should see for a remote
        community."""
        env.community.subscriptions_count = 12
        env.community.total_subscriptions_count = 1215
        db.session.commit()

        # `format_compact_decimal` rounds to whole thousands at this magnitude, so the
        # assertion is on WHICH column was read rather than on the formatting.
        assert env.community.humanize_subscribers() == '1K'

    def test_the_local_count_is_shown_when_asked_for(self, env):
        env.community.subscriptions_count = 12
        env.community.total_subscriptions_count = 1215
        db.session.commit()

        assert env.community.humanize_subscribers(total=False) == '12'

    def test_a_missing_total_falls_back_to_the_local_count(self, env):
        """`total_subscriptions_count if ... else subscriptions_count`. The column is NULL for a
        community whose server has never reported one, and `humanize_number(None)` would be a
        page error rather than a number."""
        env.community.subscriptions_count = 7
        env.community.total_subscriptions_count = None
        db.session.commit()

        assert env.community.humanize_subscribers() == '7'

    def test_a_caller_may_supply_the_number(self, env):
        """`if "value" in kwargs`. The listing pages already have the count in hand from their
        own query, and this avoids reading the column again per row."""
        assert env.community.humanize_subscribers(value=2500) == '2K'

    def test_a_supplied_zero_is_used_rather_than_the_column(self, env):
        """`"value" in kwargs`, not `kwargs.get("value")` -- so a supplied 0 is honoured
        instead of falling through to the column, which a truthiness test would get wrong."""
        env.community.total_subscriptions_count = 999
        db.session.commit()

        assert env.community.humanize_subscribers(value=0) == '0'


class TestWhetherSomebodyHasPostedThere:
    """`has_poster` decides whether a ban notification is sent (round 250). It counts posts
    first and only queries replies if there are none, which is the cheap order for the common
    case.
    """

    def test_somebody_with_a_post_has_posted(self, env):
        make_post(env.community, env.member, ap_id='https://test.piefed.local/i/1')
        db.session.commit()

        assert env.community.has_poster(env.member)

    def test_somebody_with_only_a_reply_has_posted_too(self, env):
        """The second query, reached only when the first returns zero -- so a member who has
        only ever replied is still somebody this community has heard from."""
        post = make_post(env.community, env.owner, ap_id='https://test.piefed.local/i/2')
        db.session.commit()
        make_post_reply(post, env.member, body='only a reply')
        db.session.commit()

        assert env.community.has_poster(env.member)

    def test_somebody_who_has_done_neither_has_not(self, env):
        assert not env.community.has_poster(env.stranger)

    def test_a_post_in_another_community_does_not_count(self, env):
        """Both queries filter on `community_id`. Without it, a ban from one community would
        notify anybody who had ever posted anywhere."""
        elsewhere = make_community('otherland')
        db.session.commit()
        make_community_member(env.member, elsewhere)
        make_post(elsewhere, env.member, ap_id='https://test.piefed.local/i/3')
        db.session.commit()

        assert not env.community.has_poster(env.member)


# --------------------------------------------------------------------------
# The two flair formats
# --------------------------------------------------------------------------


class TestTheTwoFlairFormatsPublished:
    """The same five fields under two sets of names. Version 1 is Lemmy's
    `lemmy:CommunityTag` with snake_case keys; version 2 is `CommunityPostTag` with camelCase.
    A peer reading one format finds nothing in the other, so both are pinned key by key.
    """

    @pytest.fixture
    def flaired(self, env):
        flair = make_community_flair(env.community, 'Spoilers')
        flair.text_color = '#ffffff'
        flair.background_color = '#000000'
        flair.blur_images = True
        db.session.commit()
        env.flair = flair
        return env

    def test_version_one_uses_lemmys_names(self, flaired):
        entry = flaired.community.flair_for_ap(version=1)[0]

        assert entry['type'] == 'lemmy:CommunityTag'
        assert entry['display_name'] == 'Spoilers'
        assert entry['text_color'] == '#ffffff'
        assert entry['background_color'] == '#000000'
        assert entry['blur_images'] is True
        assert entry['id'].startswith(flaired.app.config['SERVER_URL'])

    def test_version_two_uses_the_newer_names(self, flaired):
        entry = flaired.community.flair_for_ap(version=2)[0]

        assert entry['type'] == 'CommunityPostTag'
        assert entry['preferredUsername'] == 'Spoilers'
        assert entry['textColor'] == '#ffffff'
        assert entry['backgroundColor'] == '#000000'
        assert entry['blurImages'] is True
        assert entry['id'] == flaired.flair.get_ap_id()

    def test_version_one_is_the_default(self, flaired):
        """The signature is `flair_for_ap(self, version=1)`, so a caller that does not say
        gets Lemmy's format -- which is what most peers read."""
        assert flaired.community.flair_for_ap() == \
            flaired.community.flair_for_ap(version=1)

    def test_an_unknown_version_publishes_nothing(self, flaired):
        """Neither arm matches, and the empty list is the honest answer: publishing a format
        this instance does not know the names for would be worse than omitting the field."""
        assert flaired.community.flair_for_ap(version=3) == []

    def test_a_community_with_no_flair_publishes_nothing(self, env):
        assert env.community.flair_for_ap(version=1) == []
        assert env.community.flair_for_ap(version=2) == []
