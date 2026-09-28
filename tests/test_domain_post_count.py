"""`Domain.post_count`, and the two things that read it.

`adjust_domain_post_count` in `app/models.py` and its eight call sites: the three
creates, the two local deletes and their restores, the federated delete and restore,
and the two ban purges.

D1362. The count was incremented when a post was created and decremented in exactly
one place -- an edit that moved a post from one domain to another. Deleting a post
left it alone, and unlike `Community.post_count` and `Tag.post_count` there is no
nightly recount for domains, so the number only ever grew. Measured: a domain at 1
with one post stayed at 1 after that post was deleted.

Two readers, both wrong once it has drifted:

  * `app/domain/routes.py` offers the domain's RSS feed only `if domain.post_count >
    0`, so a domain whose only post was deleted keeps advertising an empty feed;
  * that feed's ETag is `f"{domain.id}_{hash(domain.post_count)}"`, so a reader
    holding it gets a 304 and KEEPS THE DELETED POST until another post arrives on
    the same domain. That is the one with teeth: deleted content stays readable.
"""
import pytest
from flask import g

from app import db
from app.constants import SRC_WEB
from app.models import Community, Domain, Post, Site, adjust_domain_post_count, utcnow


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    domain = Domain(name='example.test', post_count=0, banned=False)
    db.session.add(domain)
    community = db.session.get(Community, api_baseline.community1.id)
    community.instance_id = 1
    community.private = False
    db.session.commit()
    return SimpleNamespace(baseline=api_baseline, domain=domain,
                           community=community, client=app.test_client(),
                           app=app)


def a_link_post(env, title='a link', url='https://example.test/article'):
    post = Post(user_id=env.baseline.user2.id, community_id=env.community.id,
                title=title, url=url, domain_id=env.domain.id, type=1,
                posted_at=utcnow(), last_active=utcnow(), deleted=False, status=1,
                from_bot=False, nsfw=False, nsfl=False, sticky=False,
                indexable=True, microblog=False)
    db.session.add(post)
    env.domain.post_count += 1
    db.session.commit()
    return post


def count(env):
    """The committed value.

    The app factory sets `autoflush=False`, so `db.session.refresh` on its own
    DISCARDS a pending change and re-reads the old row -- three tests here failed
    that way before the commit was added.
    """
    db.session.commit()
    db.session.refresh(env.domain)
    return env.domain.post_count


class TestTheHelper:
    def test_it_takes_one_off(self, env):
        post = a_link_post(env)

        adjust_domain_post_count(post, -1)

        assert count(env) == 0

    def test_it_puts_one_back(self, env):
        post = a_link_post(env)

        adjust_domain_post_count(post, -1)
        adjust_domain_post_count(post, 1)

        assert count(env) == 1

    def test_it_never_goes_below_zero(self, env):
        """Rows written before this existed are already too high, and a later
        delete must not push them negative."""
        post = a_link_post(env)
        env.domain.post_count = 0
        db.session.commit()

        adjust_domain_post_count(post, -1)

        assert count(env) == 0

    def test_a_post_with_no_domain_is_nothing_to_do_with_it(self, env):
        post = a_link_post(env)
        post.domain_id = None
        db.session.commit()

        adjust_domain_post_count(post, -1)

        assert count(env) == 1

    def test_a_domain_row_that_has_gone(self, env):
        """`post.domain_id` has a foreign key, so a row that does not exist cannot
        be committed -- the object is built without being added to the session,
        which is all the helper reads."""
        orphan = Post(user_id=env.baseline.user2.id, community_id=env.community.id,
                      title='x', domain_id=999999, type=1)

        adjust_domain_post_count(orphan, -1)  # no row, no error

    def test_a_null_count_is_treated_as_zero(self, env):
        post = a_link_post(env)
        env.domain.post_count = None
        db.session.commit()

        adjust_domain_post_count(post, 1)

        assert count(env) == 1


class TestDeletingAPost:
    def login(self, env, user):
        with env.client.session_transaction() as session:
            session['_user_id'] = str(user.id)
            session['_fresh'] = True

    def test_the_authors_own_delete_takes_one_off(self, env, monkeypatch):
        import app.shared.post as shared_post

        post = a_link_post(env)
        monkeypatch.setattr(shared_post, 'current_user', env.baseline.user2,
                            raising=False)

        shared_post.delete_post(post.id, federate_deletion=False, src=SRC_WEB,
                                auth=None)

        assert count(env) == 0

    def test_restoring_it_puts_one_back(self, env, monkeypatch):
        import app.shared.post as shared_post

        post = a_link_post(env)
        monkeypatch.setattr(shared_post, 'current_user', env.baseline.user2,
                            raising=False)
        shared_post.delete_post(post.id, federate_deletion=False, src=SRC_WEB,
                                auth=None)

        shared_post.restore_post(post.id, src=SRC_WEB, auth=None)

        assert count(env) == 1

    def test_a_federated_delete_takes_one_off(self, env):
        from app.activitypub.util import delete_post_or_comment

        post = a_link_post(env)

        delete_post_or_comment(env.baseline.user2, post, False,
                               {'id': 'https://remote.test/activities/delete/1'}, '')

        assert count(env) == 0

    def test_a_federated_restore_puts_one_back(self, env):
        from app.activitypub.util import (delete_post_or_comment,
                                          restore_post_or_comment)

        post = a_link_post(env)
        delete_post_or_comment(env.baseline.user2, post, False,
                               {'id': 'https://remote.test/activities/delete/2'}, '')

        restore_post_or_comment(env.baseline.user2, post, False,
                                {'id': 'https://remote.test/activities/undo/2'}, '')

        assert count(env) == 1

    def test_a_moderators_removal_takes_one_off(self, env, monkeypatch):
        """`mod_remove_post` is a fourth way a post stops counting, and nothing
        drove it -- its mutant survived the first pass of this round."""
        import app.shared.post as shared_post
        from tests.factories import make_community_member

        post = a_link_post(env)
        moderator = env.baseline.user4
        make_community_member(moderator, env.community, is_moderator=True)
        monkeypatch.setattr(shared_post, 'current_user', moderator, raising=False)

        shared_post.mod_remove_post(post.id, 'spam', SRC_WEB, None)

        assert count(env) == 0

    def test_a_moderators_restore_puts_one_back(self, env, monkeypatch):
        import app.shared.post as shared_post
        from tests.factories import make_community_member

        post = a_link_post(env)
        moderator = env.baseline.user4
        make_community_member(moderator, env.community, is_moderator=True)
        monkeypatch.setattr(shared_post, 'current_user', moderator, raising=False)
        shared_post.mod_remove_post(post.id, 'spam', SRC_WEB, None)

        shared_post.mod_restore_post(post.id, 'mistake', SRC_WEB, None)

        assert count(env) == 1

    def test_a_post_with_no_url_does_not_move_it(self, env, monkeypatch):
        """Only link posts have a domain; a discussion post must not take somebody
        else's count down."""
        import app.shared.post as shared_post

        a_link_post(env)  # leaves the count at 1
        discussion = Post(user_id=env.baseline.user2.id,
                          community_id=env.community.id, title='a discussion',
                          type=0, posted_at=utcnow(), last_active=utcnow(),
                          deleted=False, status=1, from_bot=False, nsfw=False,
                          nsfl=False, sticky=False, indexable=True, microblog=False)
        db.session.add(discussion)
        db.session.commit()
        monkeypatch.setattr(shared_post, 'current_user', env.baseline.user2,
                            raising=False)

        shared_post.delete_post(discussion.id, federate_deletion=False,
                                src=SRC_WEB, auth=None)

        assert count(env) == 1


class TestWhatTheCountIsFor:
    """The two readers, because a counter nobody reads is not worth a defect
    number."""

    def test_the_feed_stops_being_offered_once_the_last_post_is_gone(self, env,
                                                                    monkeypatch):
        """`if domain.post_count > 0` in `app/domain/routes.py` decides whether the
        domain page advertises an RSS feed at all."""
        import app.shared.post as shared_post

        post = a_link_post(env)
        monkeypatch.setattr(shared_post, 'current_user', env.baseline.user2,
                            raising=False)

        shared_post.delete_post(post.id, federate_deletion=False, src=SRC_WEB,
                                auth=None)

        assert count(env) == 0

    def test_the_feeds_etag_changes_when_a_post_is_deleted(self, env, monkeypatch):
        """The one with teeth. The ETag is `f"{domain.id}_{hash(post_count)}"`, so
        while the count did not move, a conditional request got a 304 and the reader
        kept the copy containing the deleted post.
        """
        import app.shared.post as shared_post

        post = a_link_post(env)
        first = env.client.get(f'/d/{env.domain.id}/feed')
        assert first.status_code == 200
        assert 'a link' in first.text
        etag = first.headers['ETag']
        monkeypatch.setattr(shared_post, 'current_user', env.baseline.user2,
                            raising=False)

        shared_post.delete_post(post.id, federate_deletion=False, src=SRC_WEB,
                               auth=None)

        again = env.client.get(f'/d/{env.domain.id}/feed',
                               headers={'If-None-Match': etag})
        assert again.status_code == 200, 'a 304 would keep the deleted post'
        assert 'a link' not in again.text

    def test_an_unchanged_domain_still_gets_its_304(self, env):
        """The other side: the ETag has to keep working, or every reader refetches
        the feed every time."""
        a_link_post(env)
        first = env.client.get(f'/d/{env.domain.id}/feed')

        again = env.client.get(f'/d/{env.domain.id}/feed',
                               headers={'If-None-Match': first.headers['ETag']})

        assert again.status_code == 304
