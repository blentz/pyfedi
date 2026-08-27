"""put_community_subscribe's add/remove/reject paths.

**Every `.first()` in this file carries an `ORDER BY`, and the first query also
filters `community_ban`. Both are load-bearing; do not drop either.**

This file used to fail intermittently in full-suite runs and pass in isolation.
The cause was `Community.query.filter(...).first()` with no `ORDER BY`.
`Community.query` eager-joins `file` (the `icon` relationship), so PostgreSQL has
a real join-strategy choice to make, and it picks differently depending on the
statistics it holds for `community` and `file`:

- Nested Loop Left Join (community scanned in heap order) -> community1 first.
- Hash Right Join (community hashed) -> community3 first.

Both were observed from this fixture. The heap layout itself never varied --
`db_session`'s `TRUNCATE ... RESTART IDENTITY` makes the ids and the ctids
identical on every run -- so physical row order was not the variable. Planner
statistics were: the test database is long-lived (tmpfs, never `--down`), so
autovacuum's autoanalyze fires at unpredictable points across a campaign and
flips the plan under otherwise identical runs.

`api_baseline` leaves exactly two communities matching "not already subscribed
and not itself banned": community1, and community3 -- which user1 is
`CommunityBan`'d from. So under the Hash Right Join plan the "normal add /
remove" block below picked the one community it is impossible to subscribe to
and died on `Exception: You are banned from this community.` The block passed
only when the planner happened to hand it the other row.

That is two defects, and the fix is two changes:

- The query did not express its own premise. "A community I can newly subscribe
  to" excludes communities I am banned from; the filter said nothing about
  `community_ban`. `test_the_candidate_query_skips_a_community_the_user_is_banned_from`
  is the discriminator.
- `.first()` without `ORDER BY` returns whichever row the plan emits first, which
  is not a stable property of the data.
  `test_the_subscribable_community_is_the_same_under_either_join_plan` drives
  both plans and is the discriminator.

The `.scalars()` results are materialised with `.all()` rather than handed to
`not_in()` as live `ScalarResult` iterators. A `ScalarResult` is consumable
exactly once, so the lazy form silently degrades to an empty `NOT IN ()` if the
same object is ever reused -- it worked here only because each block built a
fresh one.
"""

import pytest
from sqlalchemy import text

from app import db
from app.models import Community, CommunityBan, User


def _subscribable_community(user_id):
    """The lowest-id community `user_id` could newly subscribe to, or None.

    "Could newly subscribe to" is all three of: not already subscribed, the
    community is not banned, and the user is not banned from it. See this
    module's docstring for why each clause and the ordering are here.
    """
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 1'),
        {"user_id": user_id}).scalars().all()
    existing_bans = db.session.execute(
        text('SELECT community_id FROM "community_ban" WHERE user_id = :user_id'),
        {"user_id": user_id}).scalars().all()
    return Community.query.filter(Community.id.not_in(existing_subs),
                                  Community.id.not_in(existing_bans),
                                  Community.banned == False).order_by(Community.id).first()


def test_api_community_subscriptions(app, api_baseline):
    from app.api.alpha.utils.community import put_community_subscribe

    user_id = api_baseline.user1.id
    user = User.query.get(user_id)
    assert user is not None and hasattr(user, 'id')
    jwt = user.encode_jwt_token()
    assert jwt is not None
    auth = f'Bearer {jwt}'

    # normal add / remove subscription
    community = _subscribable_community(user_id)
    assert community is not None and hasattr(community, 'id')

    data = {"community_id": community.id, "subscribe": True}
    result = put_community_subscribe(auth, data)
    assert result is not None and result['community_view']['activity_alert'] == True
    data = {"community_id": community.id, "subscribe": False}
    result = put_community_subscribe(auth, data)
    assert result is not None and result['community_view']['activity_alert'] == False

    # remove from non-existing
    data = {"community_id": community.id, "subscribe": False}
    with pytest.raises(Exception) as ex:
        put_community_subscribe(auth, data)
    assert str(ex.value) == 'A subscription for this community did not exist.'

    # add to existing
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 1'),
        {"user_id": user_id}).scalars().all()
    community = Community.query.filter(Community.id.in_(existing_subs),
                                       Community.banned == False).order_by(Community.id).first()
    assert community is not None and hasattr(community, 'id')
    if community:
        data = {"community_id": community.id, "subscribe": True}
        with pytest.raises(Exception) as ex:
            put_community_subscribe(auth, data)
        assert str(ex.value) == 'A subscription for this community already existed.'

    # add to a banned community
    community = Community.query.filter(Community.banned == True).order_by(Community.id).first()
    if community:
        data = {"community_id": community.id, "subscribe": True}
        with pytest.raises(Exception):
            result = put_community_subscribe(auth, data)

    # remove from a banned community
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 1'),
        {"user_id": user_id}).scalars().all()
    community = Community.query.filter(Community.id.in_(existing_subs),
                                       Community.banned == True).order_by(Community.id).first()
    if community:
        data = {"community_id": community.id, "subscribe": False}
        with pytest.raises(Exception):
            result = put_community_subscribe(auth, data)

    # add to a community this user is banned from
    existing_bans = db.session.execute(text('SELECT community_id FROM "community_ban" WHERE user_id = :user_id'),
                                       {"user_id": user_id}).scalars().all()
    existing_subs = db.session.execute(
        text('SELECT entity_id FROM "notification_subscription" WHERE user_id = :user_id AND type = 1'),
        {"user_id": user_id}).scalars().all()
    community = Community.query.filter(Community.id.in_(existing_bans), Community.id.not_in(existing_subs),
                                       Community.banned == False).order_by(Community.id).first()
    assert community is not None and hasattr(community, 'id')
    if community:
        data = {"community_id": community.id, "subscribe": True}
        with pytest.raises(Exception) as ex:
            result = put_community_subscribe(auth, data)
        assert str(ex.value) == 'You are banned from this community.'


def test_the_subscribable_community_is_the_same_under_either_join_plan(app, api_baseline):
    """The row `_subscribable_community` returns does not depend on the query plan.

    `enable_nestloop` is toggled to elicit the two join strategies PostgreSQL
    genuinely chose for this query in the wild (see the module docstring): with
    nested loops available it scans `community` in heap order, without them it
    hashes `community` and emits it in hash-bucket order. Neither is wrong; an
    unordered `.first()` simply has no stable answer across them.

    `api_baseline` on its own leaves exactly ONE subscribable community, and a
    one-row result is plan-independent for free -- which would let an `ORDER BY`
    regression pass unnoticed. The extra community here is what makes the
    ordering load-bearing: with two candidates, the two plans disagree unless the
    query orders.

    `SET LOCAL` is scoped to the surrounding transaction, which `db_session`
    rolls back, so neither setting escapes this test.
    """
    from tests.factories import make_community

    second_candidate = make_community('community5')
    assert second_candidate.id > api_baseline.community1.id

    chosen = []
    for nestloop in ('on', 'off'):
        db.session.execute(text(f'SET LOCAL enable_nestloop = {nestloop}'))
        community = _subscribable_community(api_baseline.user1.id)
        assert community is not None
        chosen.append(community.id)

    assert chosen == [api_baseline.community1.id, api_baseline.community1.id]


def test_the_candidate_query_skips_a_community_the_user_is_banned_from(app, api_baseline):
    """A community the user is CommunityBan'd from is not a subscribe candidate.

    Without the `community_ban` clause this returns community3 -- the community
    `api_baseline` bans user1 from -- and every "normal add / remove" assertion
    downstream of it fails with 'You are banned from this community.'
    """
    user_id = api_baseline.user1.id
    assert _subscribable_community(user_id).id == api_baseline.community1.id

    db.session.add(CommunityBan(user_id=user_id, community_id=api_baseline.community1.id, banned_by=user_id))
    db.session.commit()

    remaining = _subscribable_community(user_id)
    assert remaining is None, (
        'api_baseline leaves community2 (already subscribed), community3 (banned from) and '
        'bannedcommunity (banned) as the only other rows, so banning user1 from community1 '
        f'must exhaust the candidates -- got community id {remaining.id if remaining else None}'
    )
