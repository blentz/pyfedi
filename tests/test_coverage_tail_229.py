"""Round 229: the filters `/communities` and `/health2` apply to what they list.

`app/main/routes.py` stood at 86.36%. The largest coherent block of red was the filter
stack both listing endpoints run over `Community.query`: who is banned from what, which
communities and instances the viewer has blocked, their keyword filters, the NSFW and NSFL
switches, and the subscribed/not-subscribed and local/remote selectors. Not one of those
filters had a row, in either endpoint.

These are worth more than their line count. Every one of them is the difference between a
listing a viewer asked for and a listing that shows them something they have said they do
not want, and three of them -- the ban list, the block list and the instance block list --
are decisions someone else made about them. A filter that silently stops filtering looks
exactly like one that works, in any test that does not check WHICH communities came back.
So every row here names a community that must be in the answer and a community that must
not, rather than counting rows or asserting a 200.

D1418 was found writing them: `list_communities` reset `hide_nsfw` to False partway down,
discarding the site-level `enable_nsfw` decision made at the top, so an instance with NSFW
disabled still rendered the NSFW selector. See the class at the end of this file.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import (Community, CommunityBan, CommunityBlock, Filter, Instance,
                        InstanceBlock, Site)
from tests.factories import make_community, make_community_member, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_nsfw = True
    g.site.enable_nsfl = True
    # The fixture site is a PRIVATE instance, and `/communities` carries
    # `@login_required_if_private_instance`, so an anonymous client is redirected before
    # any of this module's filters run. Every anonymous row below is about the `else` arm
    # of `if current_user.is_authenticated`, not about the private-instance gate, which
    # has its own file.
    g.site.private_instance = False
    user = make_user(api_baseline.instance_local, 'listtester', local=True)
    user.verified = True
    user.private_key = 'x'
    # `User.hide_nsfw` and `hide_nsfl` both default to 1, which forces `nsfw` to 'no'
    # before the selector is read. The rows that are ABOUT those preferences set them
    # themselves; everything else wants a viewer who has expressed none.
    user.hide_nsfw = 0
    user.hide_nsfl = 0
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, client=client, anonymous=app.test_client(),
                           user=user, baseline=api_baseline, site=g.site)


def listed(response):
    """The community names the page actually offers.

    The template prints each community's title inside its link, and these titles are
    single distinctive words, so a substring test over the body is enough -- and it is
    what a reader of the page sees, which is the thing under test.
    """
    body = response.get_data(as_text=True)
    return {c.title for c in Community.query.all() if c.title in body}


# --------------------------------------------------------------------------
# /communities, signed in
# --------------------------------------------------------------------------


class TestWhatASignedInViewerIsShown:

    def test_a_community_they_are_banned_from_is_not_listed(self, env):
        """:439. The ban is a moderator's decision about this viewer, so it is not enough
        that the community is hard to reach -- it must not be advertised either."""
        allowed = make_community('permitted')
        banned = make_community('forbidden')
        db.session.add(CommunityBan(user_id=env.user.id, community_id=banned.id))
        db.session.commit()

        names = listed(env.client.get('/communities'))

        assert 'permitted' in names
        assert 'forbidden' not in names

    def test_a_community_they_have_blocked_is_not_listed(self, env):
        """:452. The viewer's own decision, and the one case where showing it anyway would
        be read as the block having silently failed."""
        kept = make_community('kept')
        blocked = make_community('unwanted')
        db.session.add(CommunityBlock(user_id=env.user.id, community_id=blocked.id))
        db.session.commit()

        names = listed(env.client.get('/communities'))

        assert 'kept' in names
        assert 'unwanted' not in names

    def test_a_community_on_a_blocked_instance_is_not_listed(self, env):
        """:455. The filter is `instance_id NOT IN (...) OR instance_id IS NULL`, so a
        community with no instance at all must survive it -- which is why this row keeps a
        third community with `instance_id` unset."""
        instance = Instance(domain='blocked.example', software='piefed')
        db.session.add(instance)
        db.session.commit()
        local = make_community('homegrown')
        remote = make_community('faraway')
        remote.instance_id = instance.id
        instanceless = make_community('stateless')
        instanceless.instance_id = None
        db.session.add(InstanceBlock(user_id=env.user.id, instance_id=instance.id))
        db.session.commit()

        names = listed(env.client.get('/communities'))

        assert 'homegrown' in names
        assert 'stateless' in names
        assert 'faraway' not in names

    def test_a_community_matching_a_keyword_filter_is_not_listed(self, env):
        """:458. `filtered_out_communities` matches the keyword against both `name` and
        `title`, so the row that proves the filter ran needs a community whose name does
        NOT contain the keyword but whose title does."""
        env.user.community_keyword_filter = 'cricket'
        kept = make_community('football')
        by_name = make_community('cricketclub')
        by_title = make_community('sportsdesk')
        by_title.title = 'cricket news'
        db.session.commit()

        names = listed(env.client.get('/communities'))
        body = env.client.get('/communities').get_data(as_text=True)

        assert 'football' in names
        assert 'cricketclub' not in names
        assert 'cricket news' not in body

    def test_a_low_quality_community_is_hidden_when_they_ask(self, env):
        """:436. `hide_low_quality` is a preference, so the same two communities must both
        appear once it is off -- otherwise this row would pass against a filter that drops
        low-quality communities from everyone."""
        good = make_community('curated')
        poor = make_community('memedump')
        poor.low_quality = True
        env.user.hide_low_quality = True
        db.session.commit()

        hidden = listed(env.client.get('/communities'))

        env.user.hide_low_quality = False
        db.session.commit()
        shown = listed(env.client.get('/communities'))

        assert 'curated' in hidden and 'memedump' not in hidden
        assert 'memedump' in shown

    def test_the_nsfw_selector_filters_both_ways(self, env):
        """:445-448, the arm taken by a viewer who has NOT set `hide_nsfw`. Both values are
        asserted: a filter stuck on `False` satisfies `?nsfw=no` by itself."""
        plain = make_community('wholesome')
        adult = make_community('spicy')
        adult.nsfw = True
        env.user.hide_nsfw = 0
        db.session.commit()

        without = listed(env.client.get('/communities?nsfw=no'))
        only = listed(env.client.get('/communities?nsfw=yes'))

        assert 'wholesome' in without and 'spicy' not in without
        assert 'spicy' in only and 'wholesome' not in only

    def test_the_viewers_own_nsfw_preference_overrides_the_selector(self, env):
        """:441-443. `hide_nsfw == 1` forces `nsfw` to 'no' BEFORE the selector is read, so
        `?nsfw=yes` from such a viewer must not produce an NSFW listing. Without this row
        the override and the selector are indistinguishable."""
        make_community('wholesome')
        adult = make_community('spicy')
        adult.nsfw = True
        env.user.hide_nsfw = 1
        db.session.commit()

        names = listed(env.client.get('/communities?nsfw=yes'))

        assert 'wholesome' in names
        assert 'spicy' not in names


# --------------------------------------------------------------------------
# /communities, the selectors anybody can drive
# --------------------------------------------------------------------------


class TestTheSelectors:

    def test_remote_only_hides_local_communities(self, env):
        """:376. `home_select=remote` is `ap_id IS NOT NULL`; the factory leaves `ap_id`
        unset, which is what makes a community local."""
        local = make_community('townhall')
        remote = make_community('elsewhere')
        remote.ap_id = 'elsewhere@remote.example'
        db.session.commit()

        remote_only = listed(env.client.get('/communities?home_select=remote'))
        local_only = listed(env.client.get('/communities?home_select=local'))

        assert 'elsewhere' in remote_only and 'townhall' not in remote_only
        assert 'townhall' in local_only and 'elsewhere' not in local_only

    def test_subscribed_and_not_subscribed_are_complements(self, env):
        """:385, :387, :391, :394. The joined-id list is built from both memberships and
        moderatorships, and the two selector values must partition the same set -- a
        community cannot be missing from both answers."""
        joined = make_community('mine')
        other = make_community('theirs')
        make_community_member(env.user, joined)
        db.session.commit()

        subscribed = listed(env.client.get('/communities?subscribe_select=subscribed'))
        not_subscribed = listed(
            env.client.get('/communities?subscribe_select=not_subscribed'))

        assert 'mine' in subscribed and 'theirs' not in subscribed
        assert 'theirs' in not_subscribed and 'mine' not in not_subscribed

    def test_a_moderated_community_counts_as_subscribed(self, env):
        """:387 on its own. Moderating a community without joining it is exactly the case
        the second loop exists for, and the only one that tells the two loops apart."""
        moderated = make_community('mypatch')
        make_community_member(env.user, moderated, is_moderator=True)
        db.session.commit()

        names = listed(env.client.get('/communities?subscribe_select=subscribed'))

        assert 'mypatch' in names

    def test_the_prompt_argument_flashes_an_invitation(self, env):
        """:333. A message rendered from a query-string argument, which is worth pinning
        as the literal text a viewer reads."""
        response = env.client.get('/communities?prompt=1')

        assert 'You did not choose any topics' in response.get_data(as_text=True)


# --------------------------------------------------------------------------
# /communities, signed out
# --------------------------------------------------------------------------


class TestWhatAnAnonymousViewerIsShown:

    def test_nsfl_is_always_hidden_from_anonymous_viewers(self, env):
        """The `else` arm's first line: an anonymous viewer has no preference to read, so
        NSFL is unconditional rather than a selector."""
        plain = make_community('ordinary')
        nsfl = make_community('grim')
        nsfl.nsfl = True
        db.session.commit()

        names = listed(env.anonymous.get('/communities'))

        assert 'ordinary' in names
        assert 'grim' not in names

    def test_an_anonymous_viewer_can_still_use_the_nsfw_selector(self, env):
        """:464-465, the anonymous twin of the signed-in selector."""
        plain = make_community('safeforwork')
        adult = make_community('adultsonly')
        adult.nsfw = True
        db.session.commit()

        without = listed(env.anonymous.get('/communities?nsfw=no'))
        only = listed(env.anonymous.get('/communities?nsfw=yes'))

        assert 'safeforwork' in without and 'adultsonly' not in without
        assert 'adultsonly' in only and 'safeforwork' not in only


# --------------------------------------------------------------------------
# D1418: the site-level NSFW switch
# --------------------------------------------------------------------------


class TestTheSiteWideNsfwSwitch:
    """`list_communities` decides `hide_nsfw` from `g.site.enable_nsfw` at the top, then
    re-derived it from the viewer's own preference further down -- with an unconditional
    `hide_nsfw = False` in between, which threw the site's decision away. The template
    renders the NSFW All/Yes/No selector when `hide_nsfw` is falsy.
    """

    def test_an_instance_with_nsfw_off_does_not_offer_the_selector(self, env):
        """The D1418 regression. The reset line is removed, so the site decision survives
        to the template."""
        env.site.enable_nsfw = False
        db.session.commit()

        body = env.client.get('/communities').get_data(as_text=True)

        assert 'id="nsfw"' not in body

    def test_an_instance_with_nsfw_on_does_offer_it(self, env):
        """:330, the other arm, and the control for the row above: without it, deleting the
        selector from the template would satisfy that assertion."""
        env.site.enable_nsfw = True
        db.session.commit()

        body = env.client.get('/communities').get_data(as_text=True)

        assert 'id="nsfw"' in body

    def test_nsfw_communities_are_hidden_on_such_an_instance_either_way(self, env):
        """What the switch is FOR, asserted separately from the control it draws. This held
        before D1418 too -- `nsfw` is forced to 'no' above -- and saying so is the reason
        D1418 is recorded as an inert control rather than as a leak."""
        env.site.enable_nsfw = False
        make_community('familyfriendly')
        adult = make_community('afterhours')
        adult.nsfw = True
        db.session.commit()

        names = listed(env.client.get('/communities?nsfw=yes'))

        assert 'familyfriendly' in names
        assert 'afterhours' not in names


# --------------------------------------------------------------------------
# /health2, which runs the same filters and takes no login
# --------------------------------------------------------------------------


class TestTheHealthProbeRunsTheSameFilters:
    """`/health2` is a performance probe: it builds the same query as `list_communities`,
    runs it, and throws the rows away. Nothing can be read out of its answer, so these rows
    assert what CAN be observed -- that each filter is reached and the endpoint still
    answers -- and exist because the two bodies are meant to stay the same. A filter
    dropped from one and not the other is the failure they are here to make visible.
    """

    @pytest.fixture
    def filtered(self, env):
        instance = Instance(domain='blocked2.example', software='piefed')
        db.session.add(instance)
        db.session.commit()
        community = make_community('probe')
        community.instance_id = instance.id
        community.nsfw = True
        db.session.add(InstanceBlock(user_id=env.user.id, instance_id=instance.id))
        db.session.add(CommunityBan(user_id=env.user.id, community_id=community.id))
        env.user.community_keyword_filter = 'probe'
        env.user.hide_low_quality = True
        env.user.hide_nsfl = 1
        env.user.hide_nsfw = 0
        db.session.commit()
        return env

    @pytest.mark.parametrize('query', ['', '?nsfw=no', '?nsfw=yes'])
    def test_the_signed_in_probe_answers_with_every_filter_engaged(self, filtered, query):
        response = filtered.client.get(f'/health2{query}')

        assert response.status_code == 200

    @pytest.mark.parametrize('query', ['', '?nsfw=no', '?nsfw=yes'])
    def test_the_anonymous_probe_answers_too(self, filtered, query):
        """The `else` arm, which is the one an uptime checker actually drives."""
        response = filtered.anonymous.get(f'/health2{query}')

        assert response.status_code == 200

    def test_the_probe_honours_the_site_nsfw_switch(self, filtered):
        """:1622-1623. `nsfw` defaults to None here rather than 'all', so the default is
        filled in only when the site allows NSFW at all -- the opposite order from
        `list_communities`, and the reason these two lines are separate."""
        filtered.site.enable_nsfw = True
        db.session.commit()

        assert filtered.anonymous.get('/health2').status_code == 200

        filtered.site.enable_nsfw = False
        db.session.commit()

        assert filtered.anonymous.get('/health2').status_code == 200

    def test_the_probe_honours_the_viewers_own_nsfw_preference(self, filtered):
        """:1673-1674. `hide_nsfw == 1` sets `nsfw` to None here rather than to 'no' --
        `/health2` uses None for "no opinion" where `list_communities` uses 'all' -- and
        then filters NSFW out regardless of what the selector asked for."""
        filtered.user.hide_nsfw = 1
        db.session.commit()

        assert filtered.client.get('/health2?nsfw=yes').status_code == 200


class TestThePaginationLinksCarryTheNormalisedFilters:
    """`nsfw = 'no'` inside the `hide_nsfw == 1` arm survived a mutant at first: the line
    below it already filters `Community.nsfw == False`, so deleting the assignment changes
    no listing. Its one observable effect is `args_dict["nsfw"]`, which is spread into the
    next- and previous-page links -- so a viewer who has asked never to see NSFW gets
    pagination links that say `nsfw=no`, rather than links that repeat the `nsfw=yes` the
    server has already overruled.

    Reaching that needs more communities than fit on a page -- 100 for a signed-in viewer
    -- which is why this is one row with a bulk insert rather than a parametrized set.
    """

    def test_a_forced_filter_is_written_into_the_page_links(self, env):
        env.user.hide_nsfw = 1
        for n in range(101):
            db.session.add(Community(
                name=f'bulk{n}', title=f'bulk{n}', instance_id=1, user_id=1,
                ap_profile_id=f'https://test.piefed.local/c/bulk{n}',
                ap_public_url=f'https://test.piefed.local/c/bulk{n}',
                ap_followers_url=f'https://test.piefed.local/c/bulk{n}/followers',
                ap_domain='test.piefed.local', subscriptions_count=0,
                local_only=False, nsfw=False))
        db.session.commit()

        body = env.client.get('/communities?nsfw=yes').get_data(as_text=True)

        assert 'Next page' in body
        assert 'nsfw=no' in body
        assert 'nsfw=yes' not in body
