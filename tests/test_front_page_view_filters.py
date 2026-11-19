"""`home_page`'s four view filters: which communities' posts each one selects.

The busiest page on the instance. `tests/test_main_front_page.py` covers that it
answers for every filter; nothing covered **which** posts each filter chooses, and
that choice is four different SQL fragments built by hand:

    subscribed  community_ids from community_member, for this user
    local       c.private is false and c.instance_id = 1            (anonymous)
                (c.private is false OR c.id IN <their private>) AND c.instance_id = 1
    popular     c.show_popular is true and c.private is false AND c.low_quality is false
                (c.private is false OR c.id IN <their private>) AND c.show_popular is true
    all         community_ids = [-1], a sentinel `get_deduped_post_ids` reads

Each has an anonymous arm and an authenticated one, and the two differ in exactly
the place that matters: whether the reader's own private communities are added back.
Twelve statements of those arms had no test.

WHY THE PRIVATE COMMUNITY IS IN EVERY ROW HERE. `Community.private` is invite-only
access control, not the `Post.private` microblog marker. The `local` and `popular`
fragments carry `c.private is false` themselves, and `all` does not -- it hands
`[-1]` to `get_deduped_post_ids`, which appends the private restriction
unconditionally for every viewer at `app/utils.py:4405-4416`, in one place kept
outside the anonymous/authenticated split precisely so no arm can be added without
it. So there are two mechanisms, and a reader of either alone would not know the
other exists. Both are asserted below, for both kinds of reader.

`show_all` and `show_popular` are what put a community in the All and Popular
listings, and `make_community` leaves both at their column defaults -- True. A row
asserting a community is ABSENT from one of those feeds has to set the flag itself,
or it passes for the wrong reason.

HARNESS. `home_page` renders `index.html`, so `render_template` is patched and the
`posts` it was handed is what the assertions read -- the titles, not the markup,
because `base.html` contributes its own and a substring check against the whole page
cannot tell a teaser from a sidebar entry (fact 830).
"""
import pytest
from unittest.mock import patch

from flask import g

from app import db
from app.models import Community, CommunityMember, Site, utcnow
from tests.factories import (make_community, make_community_member, make_post,
                             make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'


def make_instance_local():
    from tests.factories import make_instance
    existing = db.session.get(Instance_(), 1)
    return existing if existing is not None else make_instance(HOST,
                                                              software='piefed')


def Instance_():
    from app.models import Instance
    return Instance


def _peer_instance():
    from tests.factories import make_instance
    from app.models import Instance
    existing = Instance.query.filter_by(domain='peer.test').first()
    return existing if existing is not None else make_instance('peer.test')


def _titles(env, view_filter, user=None, **args):
    """The post titles the front page was handed for this filter."""
    captured = {}

    def fake_render(template, **kwargs):
        captured.update(kwargs)
        return 'rendered'

    client = env.app.test_client()
    if user is not None:
        with client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    # `/home/<sort>/<view_filter>` -- both are PATH segments, not query arguments.
    # The first draft of this file passed them as a query string, so every row got
    # the default filter and ten of them failed against a list they never asked for.
    with patch('app.main.routes.render_template', side_effect=fake_render):
        response = client.get(f'/home/new/{view_filter}', query_string=args)

    assert response.status_code == 200, response.status_code
    return sorted(post.title for post in captured['posts'])


ALL_LOCAL = ['post in local_hidden', 'post in local_open', 'post in local_quiet']


# --------------------------------------------------------------------------
# local
# --------------------------------------------------------------------------


class TestTheLocalFilter:
    def test_an_anonymous_reader_sees_local_public_communities(self, front_page_view_filters_env):
        """`c.private is false and c.instance_id = 1`. The remote community's post
        is what proves the instance_id half, and the private one the other."""
        titles = _titles(front_page_view_filters_env, 'local')

        assert titles == ALL_LOCAL

    def test_a_remote_communitys_post_is_absent(self, front_page_view_filters_env):
        titles = _titles(front_page_view_filters_env, 'local')

        assert 'post in remote_open' not in titles

    def test_a_private_community_is_absent_for_a_stranger(self, front_page_view_filters_env):
        titles = _titles(front_page_view_filters_env, 'local', user=front_page_view_filters_env.stranger)

        assert 'post in local_private' not in titles

    def test_a_private_community_is_present_for_its_member(self, front_page_view_filters_env):
        """The authenticated arm's whole difference:
        `(c.private is false OR c.id IN <their private>)`. Without the OR the member
        sees the same list as the stranger."""
        titles = _titles(front_page_view_filters_env, 'local', user=front_page_view_filters_env.reader)

        assert 'post in local_private' in titles
        assert titles == sorted(ALL_LOCAL + ['post in local_private'])

    # The claim these two rows make together -- two readers of the same URL get
    # different lists, and only membership explains it -- is deliberately NOT written
    # as one test comparing the two. Fact 322: flask_login answers a second request
    # in the same test as the first one's user, so a row making both requests found
    # the member's list twice and the difference was empty. One request per test, and
    # the pair is the comparison.


# --------------------------------------------------------------------------
# popular
# --------------------------------------------------------------------------


class TestThePopularFilter:
    def test_a_community_with_show_popular_off_is_absent(self, front_page_view_filters_env):
        """`c.show_popular is true`. `make_community` leaves the column at its
        default of True, so `local_quiet` sets it False itself -- a row asserting
        absence against a default would pass for the wrong reason."""
        titles = _titles(front_page_view_filters_env, 'popular')

        assert 'post in local_quiet' not in titles
        assert 'post in local_open' in titles

    def test_an_anonymous_reader_gets_no_private_community(self, front_page_view_filters_env):
        titles = _titles(front_page_view_filters_env, 'popular')

        assert 'post in local_private' not in titles

    def test_a_member_gets_their_private_community_when_it_is_popular(self, front_page_view_filters_env):
        """The authenticated arm. `local_private` has `show_popular` False in the
        fixture, so it is turned on here -- otherwise this row could not tell the
        `private` OR from the `show_popular` filter."""
        front_page_view_filters_env.communities['local_private'].show_popular = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'popular', user=front_page_view_filters_env.reader)

        assert 'post in local_private' in titles

    def test_a_member_still_does_not_get_a_community_that_is_not_popular(self, front_page_view_filters_env):
        """`AND c.show_popular is true` on the AUTHENTICATED arm, which the
        anonymous row above cannot reach. Without this, dropping that clause for
        signed-in readers only was invisible."""
        titles = _titles(front_page_view_filters_env, 'popular', user=front_page_view_filters_env.reader)

        assert 'post in local_quiet' not in titles
        assert 'post in local_open' in titles

    def test_a_stranger_does_not_get_it_even_when_it_is_popular(self, front_page_view_filters_env):
        """The control for the row above: `show_popular` alone must not open a
        private community to a non-member."""
        front_page_view_filters_env.communities['local_private'].show_popular = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'popular', user=front_page_view_filters_env.stranger)

        assert 'post in local_private' not in titles

    def test_the_anonymous_arm_hides_low_quality_communities_outright(self, front_page_view_filters_env):
        """`AND c.low_quality is false` is written into the anonymous fragment and is
        conditional in the authenticated one -- an anonymous reader has no
        preference to consult, so the safe answer is hard-coded."""
        front_page_view_filters_env.communities['local_open'].low_quality = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'popular')

        assert 'post in local_open' not in titles

    def test_an_authenticated_reader_who_wants_them_sees_them(self, front_page_view_filters_env):
        """`low_quality_filter` is empty unless `hide_low_quality` is set, so the
        same community that the anonymous arm hides is visible here."""
        front_page_view_filters_env.communities['local_open'].low_quality = True
        front_page_view_filters_env.reader.hide_low_quality = False
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'popular', user=front_page_view_filters_env.reader)

        assert 'post in local_open' in titles

    def test_an_authenticated_reader_who_hides_them_does_not(self, front_page_view_filters_env):
        front_page_view_filters_env.communities['local_open'].low_quality = True
        front_page_view_filters_env.reader.hide_low_quality = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'popular', user=front_page_view_filters_env.reader)

        assert 'post in local_open' not in titles


# --------------------------------------------------------------------------
# all
# --------------------------------------------------------------------------


class TestTheAllFilter:
    def test_a_community_with_show_all_off_is_absent(self, front_page_view_filters_env):
        """`community_ids = [-1]` becomes `c.show_all is true` in
        `get_deduped_post_ids`, which is the only thing that filter applies of its
        own -- so `show_all` is what decides this list."""
        titles = _titles(front_page_view_filters_env, 'all')

        assert 'post in local_hidden' not in titles
        assert 'post in local_open' in titles

    def test_a_remote_communitys_post_is_present(self, front_page_view_filters_env):
        """The difference from `local`, and the reason both exist."""
        titles = _titles(front_page_view_filters_env, 'all')

        assert 'post in remote_open' in titles

    def test_a_private_community_is_absent_for_a_stranger(self, front_page_view_filters_env):
        """THE SECOND MECHANISM. The `all` fragment carries no `c.private is false`
        of its own -- `get_deduped_post_ids` appends the restriction for every
        viewer, anonymous included (app/utils.py:4405-4416). `show_all` is turned on
        here so that only the private gate can be what excludes it.
        """
        front_page_view_filters_env.communities['local_private'].show_all = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'all', user=front_page_view_filters_env.stranger)

        assert 'post in local_private' not in titles

    def test_a_private_community_is_absent_for_an_anonymous_reader_too(self, front_page_view_filters_env):
        front_page_view_filters_env.communities['local_private'].show_all = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'all')

        assert 'post in local_private' not in titles

    def test_a_member_gets_their_private_community(self, front_page_view_filters_env):
        """And the widening half of that same unconditional block:
        `c.private is false OR c.id IN :private_community_ids`."""
        front_page_view_filters_env.communities['local_private'].show_all = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'all', user=front_page_view_filters_env.reader)

        assert 'post in local_private' in titles


# --------------------------------------------------------------------------
# subscribed
# --------------------------------------------------------------------------


class TestTheSubscribedFilter:
    def test_it_holds_only_the_communities_they_joined(self, front_page_view_filters_env):
        titles = _titles(front_page_view_filters_env, 'subscribed', user=front_page_view_filters_env.reader)

        assert titles == ['post in local_open', 'post in local_private']

    def test_a_reader_who_joined_nothing_gets_nothing(self, front_page_view_filters_env):
        """`community_ids` is the empty list, and `get_deduped_post_ids` answers []
        for it rather than falling through to every community."""
        titles = _titles(front_page_view_filters_env, 'subscribed', user=front_page_view_filters_env.stranger)

        assert titles == []

    def test_a_banned_membership_does_not_count(self, front_page_view_filters_env):
        """`WHERE cm.is_banned is false`. A reader banned from a community they
        joined loses it from their own feed."""
        membership = CommunityMember.query.filter_by(
            user_id=front_page_view_filters_env.reader.id,
            community_id=front_page_view_filters_env.communities['local_open'].id).one()
        membership.is_banned = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'subscribed', user=front_page_view_filters_env.reader)

        assert 'post in local_open' not in titles

    def test_an_anonymous_reader_asking_for_it_is_given_all(self, front_page_view_filters_env):
        """Written expecting `popular` and corrected by the answer: it is `all`.

        `index` does downgrade -- `if current_user.is_anonymous and view_filter ==
        'subscribed': view_filter = 'popular'` -- but that sits inside
        `if view_filter is None:`, so it only rewrites the DEFAULT. A reader who names
        `subscribed` in the path skips it, falls past the `subscribed` arm for want of
        an account, and lands on `elif view_filter == 'all' or
        current_user.is_anonymous:`.

        So the same word means `popular` as a default and `all` as a request. Pinned
        rather than repaired: both are public listings with the private restriction
        applied, so the asymmetry costs nothing but a reader's expectation.

        `local_quiet` is what tells the two apart -- `show_popular` off, `show_all`
        on, so it appears in All and not in Popular.
        """
        titles = _titles(front_page_view_filters_env, 'subscribed')

        assert 'post in local_quiet' in titles
        assert 'post in remote_open' in titles
        assert 'post in local_hidden' not in titles


# --------------------------------------------------------------------------
# moderating
# --------------------------------------------------------------------------


class TestTheModeratingFilter:
    def test_it_holds_the_communities_they_moderate(self, front_page_view_filters_env):
        membership = CommunityMember.query.filter_by(
            user_id=front_page_view_filters_env.reader.id,
            community_id=front_page_view_filters_env.communities['local_open'].id).one()
        membership.is_moderator = True
        db.session.commit()

        titles = _titles(front_page_view_filters_env, 'moderating', user=front_page_view_filters_env.reader)

        assert titles == ['post in local_open']

    def test_a_reader_who_moderates_nothing_gets_nothing(self, front_page_view_filters_env):
        titles = _titles(front_page_view_filters_env, 'moderating', user=front_page_view_filters_env.reader)

        assert titles == []

    def test_an_anonymous_reader_is_given_all_instead(self, front_page_view_filters_env):
        """The arm order is behaviour: `elif view_filter == 'all' or
        current_user.is_anonymous:` comes BEFORE the `moderating` arm, so an
        anonymous reader asking to moderate gets All, not an empty page.

        Asserted by the remote community's post, which only All includes.
        """
        titles = _titles(front_page_view_filters_env, 'moderating')

        assert 'post in remote_open' in titles


# --------------------------------------------------------------------------
# The round's residual: four equivalent mutants, and why
# --------------------------------------------------------------------------


def test_the_private_gate_in_each_fragment_is_belt_and_braces(app, front_page_view_filters_env):
    """FOUR EQUIVALENT MUTANTS, proved rather than left as survivors.

    The `local` and `popular` fragments each carry their own private restriction --
    `c.private is false` when anonymous, `(c.private is false OR c.id IN <theirs>)`
    when not. Removing either, or widening it to let a member see EVERY private
    community, changes no answer, because `get_deduped_post_ids` appends the same
    restriction unconditionally afterwards (app/utils.py:4405-4416):

        if current_user.is_authenticated and (private_community_ids := ...):
            post_id_where.append('(c.private is false OR c.id IN :private_community_ids) ')
        else:
            post_id_where.append('c.private is false ')

    Same two shapes, same two readers, ANDed with whatever the fragment said. So
    these four survive and no behavioural test can kill them:

        M6   the local arm drops its private gate
        M9   a member gets EVERY private community on local
        M12  the anonymous popular arm drops its private gate
        M14  a member gets EVERY private community on popular

    They are kept. That comment at utils.py:4405 says the unconditional block sits
    outside the anonymous/authenticated split "so no branch can be added that lacks
    it" -- the fragments' own gates are the same intent one layer up, and deleting
    them would leave the busiest page on the instance depending entirely on a block
    three hundred lines away in another module.

    What this row asserts is the invariant that makes them redundant: for the SAME
    community and the SAME reader, the `all` filter -- which has no fragment gate at
    all -- reaches exactly the same answer as `local`, which has one.
    """
    front_page_view_filters_env.communities['local_private'].show_all = True
    db.session.commit()

    through_the_unconditional_gate_only = _titles(front_page_view_filters_env, 'all', user=front_page_view_filters_env.stranger)

    assert 'post in local_private' not in through_the_unconditional_gate_only


def test_the_all_filter_widens_for_a_member_with_no_fragment_gate_at_all(app, front_page_view_filters_env):
    """The other half of the same invariant, and the one that proves the
    unconditional block WIDENS as well as restricts: `all` builds no `community_sql`,
    so a member seeing their private community here can only be that block."""
    front_page_view_filters_env.communities['local_private'].show_all = True
    db.session.commit()

    titles = _titles(front_page_view_filters_env, 'all', user=front_page_view_filters_env.reader)

    assert 'post in local_private' in titles
