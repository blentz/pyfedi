"""Which reported posts and comments a moderator may see.

`reported_posts` and `reported_post_replies` in `app/utils.py`. The first is called
from nine templates and route contexts, so the sidebar's report count comes from it.
The second had no callers.

D1366. The reply twin could not have run either way. It passed a LIST to an
`IN :community_ids` parameter, which `text()` renders as a Postgres array literal
rather than expanding into a value list, and it had no guard for a moderator who
moderates nothing. Measured for a moderator of no communities:

    reported_posts         -> []
    reported_post_replies  -> ProgrammingError: (psycopg2.errors.SyntaxError)
                              syntax error at or near "'{}'"

Both take the user alone since RBAC step 8, and work out from their grants which communities' reports they may
see: every one for an instance-wide holder of `administer all communities`, else the ones they moderate and those
homed on the domains they hold it on.
"""
import pytest
from flask import g

from app import db
from app.models import Community, CommunityMember, Post, PostReply, Site, utcnow
from app.utils import reported_post_replies, reported_posts


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    g.site = db.session.get(Site, 1)
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline,
                           admin=api_baseline.user1,
                           moderator=api_baseline.user4,
                           community=db.session.get(Community,
                                                    api_baseline.community1.id),
                           other=db.session.get(Community,
                                                api_baseline.community2.id))


def moderates(user, community):
    db.session.add(CommunityMember(user_id=user.id, community_id=community.id,
                                   is_moderator=True, is_owner=False))
    db.session.commit()


def a_reported_post(env, community, reports=1):
    post = Post(user_id=env.baseline.user2.id, community_id=community.id,
                title='a reported post', type=0, posted_at=utcnow(),
                last_active=utcnow(), deleted=False, status=1, from_bot=False,
                nsfw=False, nsfl=False, sticky=False, indexable=True,
                microblog=False, reports=reports)
    db.session.add(post)
    db.session.commit()
    return post


def a_reported_reply(env, community, reports=1):
    reply = PostReply(user_id=env.baseline.user2.id,
                      post_id=env.baseline.post1.id, community_id=community.id,
                      body='a reported reply', body_html='<p>x</p>',
                      posted_at=utcnow(), deleted=False, reports=reports)
    db.session.add(reply)
    db.session.commit()
    return reply


class TestAModeratorOfNothing:
    """The empty-`IN` case. `reported_posts` returns `[]`; the reply twin raised."""

    def test_posts(self, env):
        assert reported_posts(env.moderator.id) == []

    def test_replies(self, env):
        assert reported_post_replies(env.moderator.id) == []

    def test_replies_even_when_some_exist_elsewhere(self, env):
        a_reported_reply(env, env.community)

        assert reported_post_replies(env.moderator.id) == []


class TestNoUser:
    def test_posts(self, env):
        assert reported_posts(None) == []

    def test_replies(self, env):
        assert reported_post_replies(None) == []

    def test_a_missing_user_claiming_admin_is_still_answered_first(self, env):
        """`if user_id is None` comes before the admin branch, so an anonymous
        caller cannot reach the unscoped query by passing `is_admin=True`.

        Reported content has to EXIST for this to mean anything: with none, the
        admin branch answers `[]` as well, and the mutant that removes the guard
        survives. That is what it did on this round's first pass.
        """
        a_reported_post(env, env.community)
        a_reported_reply(env, env.community)

        assert reported_post_replies(None) == []
        assert reported_posts(None) == []


class TestAModeratorOfOneCommunity:
    def test_a_reported_post_in_their_community(self, env):
        moderates(env.moderator, env.community)
        post = a_reported_post(env, env.community)

        assert reported_posts(env.moderator.id) == [post.id]

    def test_a_reported_reply_in_their_community(self, env):
        moderates(env.moderator, env.community)
        reply = a_reported_reply(env, env.community)

        assert reported_post_replies(env.moderator.id) == [reply.id]

    def test_a_report_in_a_community_they_do_not_moderate(self, env):
        moderates(env.moderator, env.community)
        a_reported_post(env, env.other)
        a_reported_reply(env, env.other)

        assert reported_posts(env.moderator.id) == []
        assert reported_post_replies(env.moderator.id) == []

    def test_unreported_content_is_not_listed(self, env):
        moderates(env.moderator, env.community)
        a_reported_post(env, env.community, reports=0)
        a_reported_reply(env, env.community, reports=0)

        assert reported_posts(env.moderator.id) == []
        assert reported_post_replies(env.moderator.id) == []

    def test_two_communities_are_both_included(self, env):
        """More than one id in the `IN` list, which is what the list-versus-tuple
        parameter got wrong: a single-element case can pass by accident."""
        moderates(env.moderator, env.community)
        moderates(env.moderator, env.other)
        first = a_reported_reply(env, env.community)
        second = a_reported_reply(env, env.other)

        assert sorted(reported_post_replies(env.moderator.id)) == \
            sorted([first.id, second.id])


class TestAnAdmin:
    def test_every_reported_post(self, env):
        first = a_reported_post(env, env.community)
        second = a_reported_post(env, env.other)

        assert sorted(reported_posts(env.admin.id)) == \
            sorted([first.id, second.id])

    def test_every_reported_reply(self, env):
        """No community filter at all on this branch, which is why an admin sees
        reports from a community they do not moderate."""
        first = a_reported_reply(env, env.community)
        second = a_reported_reply(env, env.other)

        assert sorted(reported_post_replies(env.admin.id)) == \
            sorted([first.id, second.id])

    def test_a_holder_scoped_to_a_domain_sees_that_domains_reports(self, env):
        """RBAC step 8: `administer all communities` on a domain reaches the reports in that domain's communities,
        and no other community's the holder does not moderate."""
        from app.models import Role, RolePermission, SiteDomain, role_grant
        from app.storefront import clear_domain_cache
        domain = SiteDomain(domain='x.reported.example', scheme='https', federates=True)
        db.session.add(domain)
        db.session.commit()
        clear_domain_cache()
        env.community.site_domain_id = domain.id
        role = Role(name='Domain admins')
        db.session.add(role)
        db.session.commit()
        db.session.add(RolePermission(role_id=role.id, permission='administer all communities'))
        db.session.execute(role_grant.insert().values(user_id=env.moderator.id, role_id=role.id,
                                                      site_domain_id=domain.id))
        db.session.commit()
        mine, theirs = a_reported_post(env, env.community), a_reported_post(env, env.other)
        mine_reply, theirs_reply = a_reported_reply(env, env.community), a_reported_reply(env, env.other)
        try:
            assert reported_posts(env.moderator.id) == [mine.id]
            assert reported_post_replies(env.moderator.id) == [mine_reply.id]
        finally:
            clear_domain_cache()
        assert theirs.id and theirs_reply.id


class TestTheTwoAgree:
    """One question, two content types. Their signatures and their answers for the
    same situation have to match, which is what D1366 was about."""

    def test_the_same_shape_of_arguments(self):
        import inspect

        posts = inspect.signature(reported_posts).parameters
        replies = inspect.signature(reported_post_replies).parameters

        assert list(posts) == list(replies)

    def test_both_answer_empty_for_a_moderator_of_nothing(self, env):
        assert reported_posts(env.moderator.id) == \
            reported_post_replies(env.moderator.id) == []

    def test_both_see_their_own_communitys_reports(self, env):
        moderates(env.moderator, env.community)
        post = a_reported_post(env, env.community)
        reply = a_reported_reply(env, env.community)

        assert reported_posts(env.moderator.id) == [post.id]
        assert reported_post_replies(env.moderator.id) == [reply.id]
