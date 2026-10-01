"""Round 242: what an incoming post triggers, and what an outgoing reply addresses.

Two clusters in `app/models.py`.

`Post.new` is the federated ingest path: a peer's Create becomes a Post here. Two of its
blocks had no rows, and both act on somebody else's content:

    the cross-post stripper   Lemmy inserts a "cross-posted from: https://..." line into
                              the body of a link post; PieFed has its own cross-post UI, so
                              the line is removed and the HTML re-rendered
    the suspicious-domain     a Domain row can be flagged `notify_mods` or
    notifications             `notify_admins`, and a post from it raises a notification to
                              each -- the moderators of the community, then the site admins
                              who are not already among them

`PostReply.in_reply_to` and `PostReply.to` are the other half: they decide what an OUTGOING
reply names as its parent and who it is addressed to. A top-level reply points at the post;
a nested one points at the reply above it, and the two arms are separate lookups.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.activitypub.util import create_post
from app.constants import NOTIF_REPORT
from app.models import (Domain, Notification, Post, PostReply, Site, User, utcnow)
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_site, make_user)

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'


@pytest.fixture
def ingest(db_session, http_mock):
    """An author, a community, and the Site row `Post.new` reaches through
    `blocked_phrases()`.

    `Post.new` runs `fixup_url` / `is_image_url` over the peer's link, and those issue a
    HEAD request -- respx refuses anything unmocked, so the link's host is served here.
    `assert_all_called=False` because not every row in this file reaches the fetch.
    """
    http_mock.head('https://news.example/story').respond(
        200, headers={'Content-Type': 'text/html'})
    http_mock.get('https://news.example/story').respond(
        200, headers={'Content-Type': 'text/html'}, text='<html></html>')
    http_mock._assert_all_called = False
    author = make_user(make_instance('m.example'), 'alice')
    community = make_community()
    make_site()
    return SimpleNamespace(author=author, community=community)


def a_link_create(url, body='a shared story', title='A shared link'):
    """A Create wrapping a Page with a link, which is what makes `Post.new` take the
    POST_TYPE_LINK branch the cross-post stripper lives in.

    `body` has a default because `create_post` refuses an object with no `content` at all;
    every row here wants a post that arrives.
    """
    obj = {
        'id': 'https://m.example/users/alice/statuses/1',
        'type': 'Page',
        'name': title,
        'attributedTo': 'https://m.example/users/alice',
        'to': [PUBLIC],
        # `Post.new` takes the url from an ATTACHMENT, not from the object's own `url`
        # key -- a Link attachment is the Lemmy < 0.19.4 shape.
        'attachment': [{'type': 'Link', 'href': url}],
    }
    if body is not None:
        # `Post.new` reads the body only when `content` is present; `source` is what makes
        # it prefer the peer's own markdown over the rendered HTML (D1346).
        obj['content'] = f'<p>{body}</p>'
        obj['source'] = {'content': body, 'mediaType': 'text/markdown'}
    return {
        'id': 'https://m.example/users/alice/statuses/1/activity',
        'type': 'Create',
        'to': [PUBLIC],
        'object': obj,
    }


# --------------------------------------------------------------------------
# The cross-post line Lemmy adds
# --------------------------------------------------------------------------


class TestTheCrossPostedFromLine:
    """Lemmy writes `cross-posted from: https://...` into the body of a link post. PieFed
    shows cross-posts in its own UI, so the line is stripped on the way in -- and the HTML
    is re-rendered from the stripped markdown, not patched.
    """

    def test_the_line_is_removed_and_the_html_rebuilt(self, ingest):
        body = ('cross-posted from: https://lemmy.example/post/99\n'
                '\n'
                'the part a reader wrote')

        post = create_post(False, ingest.community,
                           a_link_create('https://news.example/story', body=body),
                           ingest.author)

        assert post is not None
        assert 'cross-posted from' not in post.body
        assert 'the part a reader wrote' in post.body
        # The HTML is regenerated, so the stripped line cannot survive in the rendered
        # copy -- which is what a reader actually sees.
        assert 'cross-posted from' not in post.body_html

    def test_the_double_spaced_spelling_is_removed_too(self, ingest):
        """Both spellings are matched: `cross-posted from: ` and `cross-posted from:  `
        with two spaces. Lemmy has emitted both, and a stripper that knew only one would
        leave the other in every body."""
        body = ('cross-posted from:  https://lemmy.example/post/99\n'
                '\n'
                'the part a reader wrote')

        post = create_post(False, ingest.community,
                           a_link_create('https://news.example/story', body=body),
                           ingest.author)

        assert 'cross-posted from' not in post.body

    def test_a_body_without_the_line_is_left_alone(self, ingest):
        """The guard. Re-rendering every body would be a silent rewrite of other people's
        content, so the block runs only when the phrase is present."""
        body = 'just an ordinary body with a link to https://news.example/story in it'

        post = create_post(False, ingest.community,
                           a_link_create('https://news.example/story', body=body),
                           ingest.author)

        assert post.body == body

    def test_an_html_body_is_not_re_rendered_as_markdown(self, ingest):
        """Why the guard is worth having, in the one shape where the difference is visible.

        A peer that sends HTML rather than markdown gets `body_html = allowlist_html(...)`
        and `body = html_to_text(...)`, so the two are NOT a markdown round trip. Rebuilding
        `body_html` from `body` -- which is what the stripper does after removing a line --
        would throw the peer's markup away. Here there is no cross-post line, so it must not
        happen: the anchor survives.
        """
        activity = a_link_create('https://news.example/story', body=None)
        activity['object']['content'] = (
            '<p>an <a href="https://news.example/story">ordinary</a> body</p>')
        activity['object']['mediaType'] = 'text/html'

        post = create_post(False, ingest.community, activity, ingest.author)

        assert '<a href="https://news.example/story"' in post.body_html

    def test_only_the_offending_lines_go(self, ingest):
        """The filter is per LINE, so a body whose other lines mention the same domain
        keeps them."""
        body = ('cross-posted from: https://lemmy.example/post/99\n'
                'see also https://lemmy.example/post/12\n'
                'and the rest')

        post = create_post(False, ingest.community,
                           a_link_create('https://news.example/story', body=body),
                           ingest.author)

        assert 'see also https://lemmy.example/post/12' in post.body
        assert 'and the rest' in post.body
        assert 'cross-posted from' not in post.body


# --------------------------------------------------------------------------
# Notifications about a suspicious domain
# --------------------------------------------------------------------------


class TestAPostFromAFlaggedDomain:
    """An admin can flag a `Domain` so that a post linking to it notifies the community's
    moderators, the site's admins, or both. This is the FEDERATED path -- the post arrived
    from a peer -- so it is the one where nobody here chose to publish the link.
    """

    @pytest.fixture
    def flagged(self, ingest):
        domain = Domain(name='news.example', post_count=0, notify_mods=False,
                        notify_admins=False)
        db.session.add(domain)
        moderator = make_user(None, 'themod', local=True)
        make_community_member(moderator, ingest.community, is_moderator=True)
        db.session.commit()
        ingest.domain = domain
        ingest.moderator = moderator
        return ingest

    def _ingest(self, env):
        return create_post(False, env.community,
                           a_link_create('https://news.example/story'), env.author)

    def _notified(self):
        return {(n.user_id, n.subtype) for n in Notification.query.all()}

    def test_nothing_is_raised_for_an_unflagged_domain(self, flagged):
        """The control. Both flags default off, and a notification per link post would be
        noise on every community."""
        self._ingest(flagged)

        assert self._notified() == set()

    def test_the_communitys_moderators_are_told(self, flagged):
        flagged.domain.notify_mods = True
        db.session.commit()

        post = self._ingest(flagged)

        assert (flagged.moderator.id, 'post_from_suspicious_domain') in self._notified()
        notification = Notification.query.filter_by(
            user_id=flagged.moderator.id).one()
        assert notification.notif_type == NOTIF_REPORT
        assert notification.url == post.ap_id

    def test_a_remote_moderator_is_not_told(self, flagged):
        """D288, fixed (owner ruling): the third copy of this loop gets the same
        `is_local()` gate as `edit_post`'s and the federated Update's -- a remote
        moderator can never see a Notification row on this instance."""
        remote_mod = make_user(flagged.author.instance, 'remotemod')
        make_community_member(remote_mod, flagged.community, is_moderator=True)
        flagged.domain.notify_mods = True
        db.session.commit()
        assert remote_mod.ap_id is not None

        self._ingest(flagged)

        assert {user_id for user_id, _ in self._notified()} == {flagged.moderator.id}

    def test_the_admins_are_told(self, flagged):
        """`Site.admins()` -- a separate loop with its own flag, so an instance can watch a
        domain site-wide without telling every community's moderators.

        `Site.admins()` reads `g.admin_ids` when the request context has it, which is the
        shortcut `before_request` fills in; naming the account there is how a row says who
        the admins are without building roles.
        """
        admin = make_user(None, 'siteadmin', local=True)
        db.session.commit()
        g.admin_ids = [admin.id]
        flagged.domain.notify_admins = True
        db.session.commit()

        self._ingest(flagged)

        assert (admin.id, 'post_from_suspicious_domain') in self._notified()

    def test_nobody_is_told_twice(self, flagged):
        """`already_notified`. An admin who also moderates the community is in both loops,
        and two identical notifications for one post is what the set prevents."""
        g.admin_ids = [flagged.moderator.id]
        flagged.domain.notify_mods = True
        flagged.domain.notify_admins = True
        db.session.commit()

        self._ingest(flagged)

        assert Notification.query.filter_by(user_id=flagged.moderator.id).count() == 1

    def test_a_banned_domain_refuses_the_post_outright(self, flagged):
        """The line below the notifications: a banned domain raises rather than storing the
        post, so the two are not alternatives -- a domain can notify AND be banned, and the
        notification is written first."""
        flagged.domain.banned = True
        flagged.domain.notify_mods = True
        db.session.commit()

        post = self._ingest(flagged)

        assert post is None

    def test_an_accepted_post_counts_towards_the_domains_total(self, flagged):
        """The `else`. `Domain.post_count` is what the domain listing sorts on, so it must
        count the posts that were kept and not the ones that were refused."""
        before = flagged.domain.post_count

        self._ingest(flagged)

        db.session.refresh(flagged.domain)
        assert flagged.domain.post_count == before + 1


# --------------------------------------------------------------------------
# What a reply addresses
# --------------------------------------------------------------------------


class TestWhatAReplyPointsAt:
    """`in_reply_to` and `to` are read when a reply is sent OUT. Each has two arms -- the
    reply is either top-level or nested -- and each arm looks somewhere different, so a
    mistake sends a reply addressed to the wrong person or threaded under the wrong object.
    """

    @pytest.fixture
    def seeded(self, db_session):
        make_site()
        peer = make_instance('reply.example')
        author = make_user(peer, 'postauthor')
        replier = make_user(peer, 'firstreplier')
        nested_author = make_user(peer, 'secondreplier')
        community = make_community('replyland')
        db.session.commit()
        post = make_post(community, author, ap_id='https://reply.example/p/1')
        parent = make_post_reply(post, replier, body='the first reply')
        parent.ap_id = 'https://reply.example/c/1'
        child = make_post_reply(post, nested_author, body='a nested reply')
        child.ap_id = 'https://reply.example/c/2'
        child.parent_id = parent.id
        db.session.commit()
        return SimpleNamespace(post=post, parent=parent, child=child, author=author,
                               replier=replier, nested_author=nested_author)

    def test_a_top_level_reply_points_at_the_post(self, seeded):
        assert seeded.parent.in_reply_to() == seeded.post.ap_id

    def test_a_nested_reply_points_at_the_reply_above_it(self, seeded):
        """Not at the post: threading in every other implementation follows
        `inReplyTo`, so a nested reply naming the post would flatten the thread for
        everybody who receives it."""
        assert seeded.child.in_reply_to() == seeded.parent.ap_id

    def test_a_top_level_reply_is_addressed_to_the_posts_author(self, seeded):
        assert seeded.parent.to() == seeded.author.public_url()

    def test_a_nested_reply_is_addressed_to_the_parent_replys_author(self, seeded):
        """The person being answered, who is NOT the post's author -- which is why the two
        arms look at different objects and why the fixture gives each a different
        account."""
        assert seeded.child.to() == seeded.replier.public_url()
        assert seeded.child.to() != seeded.post.author.public_url()

    def test_the_relative_time_falls_back_to_english_for_an_unknown_locale(self, seeded):
        """`posted_at_localized` asks pendulum for a locale that comes from the request, so
        a locale pendulum does not know is a `ValueError` on a rendered page. The fallback
        is English rather than an error."""
        assert seeded.parent.posted_at_localized('en') != ''
        assert seeded.parent.posted_at_localized('not-a-locale') == \
            seeded.parent.posted_at_localized('en')
