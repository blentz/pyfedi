"""Covers app/utils.py's rewrite_href (app/utils.py:4968-4991): a four-rule
if/elif/elif/else chain that rewrites a remote ActivityPub href into a local
path when a matching row exists locally, and returns the href unchanged
otherwise.

Enumerated by AST (podman-compose exec ... python -c "ast.walk(...)"), not by
reading -- tests/README.md's coverage-ratchet section documents several
enumerations in this campaign that were wrong when re-derived:

    if at line 4969, orelse=1   <- top-level: if <post shape>
    if at line 4971, orelse=0   <- nested: if post:
    if at line 4976, orelse=1   <- top-level: elif '/comment/' in url:
    if at line 4972, orelse=1   <- nested: if post.slug: / else:
    if at line 4978, orelse=0   <- nested: if post_reply:
    if at line 4980, orelse=2   <- top-level: elif <community shape>
    if at line 4982, orelse=0   <- nested: if community and not community.is_local():
    if at line 4986, orelse=0   <- nested (inside the else body): if post is None:
    if at line 4988, orelse=0   <- nested: if post_reply:

Three If nodes (4969, 4976, 4980) form the top-level if/elif/elif chain; the
final `else` is not itself an If node (Python's ast represents `elif` as a
nested If in `orelse`, but a plain `else` is just body statements -- one of
which happens to be another If, at 4986). So there are FOUR rules, not nine:
post (4969), comment (4976), community (4980), and the else fallthrough. The
other six If nodes (4971, 4972, 4978, 4982, 4986, 4988) are lookups nested
inside a rule's own body, not additional rules -- a URL that exercises every
branch of the outer chain can still leave most of these lookup paths, and
their conditional rewrites, unexercised. One class per rule, not one per
branch: each class carries a MATCH+rewrite case and a MATCH-but-lookup-misses
case, proving the rewrite is conditional on the lookup succeeding rather than
on the URL shape alone.
"""
from app import db
from app.utils import rewrite_href
from tests.factories import make_community, make_instance, make_post, make_post_reply, make_user


def _base(name_suffix):
    """Instance + owner (id 1, the FK make_community hardcodes) + a community
    to hang posts/replies off of. Every test needs this scaffolding regardless
    of which rule it drives.
    """
    instance = make_instance('remote.example.com', software='piefed')
    owner = make_user(instance, f'owner{name_suffix}', local=True)
    community = make_community(f'community{name_suffix}')
    return instance, owner, community


class TestPostRule:
    """`if '/post/' in url or ... : post = Post.get_by_ap_id(url); if post: ...`
    -- the post rule has TWO distinct rewrite outcomes (post.slug when set,
    f'/post/{id}' when not), so it needs three tests, not two.
    """

    def test_matching_url_with_a_slug_rewrites_to_the_slug(self, app, db_session):
        """Fails if the `if post.slug: return post.slug` arm is deleted or the
        post rule's match branch is deleted entirely."""
        instance, owner, community = _base('a')
        url = 'https://remote.example.com/post/123'
        post = make_post(community, owner, ap_id=url)
        post.slug = 'a-post-about-things'
        db.session.commit()

        assert rewrite_href(url) == 'a-post-about-things'

    def test_matching_url_with_no_slug_rewrites_to_the_post_path(self, app, db_session):
        """Fails if the `else: return f'/post/{post.id}'` arm is deleted."""
        instance, owner, community = _base('b')
        url = 'https://remote.example.com/post/456'
        post = make_post(community, owner, ap_id=url)
        assert post.slug is None

        assert rewrite_href(url) == f'/post/{post.id}'

    def test_matching_url_shape_but_lookup_misses_returns_the_original_url(self, app, db_session):
        """Same URL shape as above (`/post/` in url), but no Post row has this
        ap_id. Proves the rewrite is conditional on the lookup, not the shape:
        fails if `if post:` is deleted (so an unbound/None post is dereferenced
        or the miss silently rewrites anyway)."""
        instance, owner, community = _base('c')
        url = 'https://remote.example.com/post/does-not-exist'

        assert rewrite_href(url) == url


class TestCommentRule:
    """`elif '/comment/' in url: post_reply = PostReply.get_by_ap_id(url); if
    post_reply: return f'/comment/{post_reply.id}'`."""

    def test_matching_url_with_a_reply_rewrites_to_the_comment_path(self, app, db_session):
        """Fails if the comment rule's rewrite is deleted."""
        instance, owner, community = _base('d')
        url = 'https://remote.example.com/comment/789'
        post = make_post(community, owner, ap_id='https://remote.example.com/post/789-parent')
        reply = make_post_reply(post, owner)
        reply.ap_id = url
        db.session.commit()

        assert rewrite_href(url) == f'/comment/{reply.id}'

    def test_matching_url_shape_but_lookup_misses_returns_the_original_url(self, app, db_session):
        """Same shape (`/comment/` in url), no PostReply has this ap_id.
        Fails if `if post_reply:` is deleted."""
        instance, owner, community = _base('e')
        url = 'https://remote.example.com/comment/does-not-exist'

        assert rewrite_href(url) == url


class TestCommunityRule:
    """`elif <community shape>: community = Community.query...; if community
    and not community.is_local(): url = f'/c/{community.link()}'`.

    Three cases, not two: match+rewrite, match+lookup-misses, AND match+found
    but LOCAL -- the `not community.is_local()` guard is its own case per the
    brief, since a mutation deleting just that guard would rewrite a local
    community's own href too.
    """

    def test_matching_url_for_a_remote_community_rewrites_to_the_community_link(self, app, db_session):
        """Fails if the community rule's rewrite is deleted, or if `not
        community.is_local()` is deleted (a remote community must still pass
        the guard)."""
        instance, owner, community = _base('f')
        # Turn the factory's (local-by-default) community into a remote one:
        # is_local() is `ap_id is None or profile_id().startswith(SERVER_URL)`,
        # so a non-None ap_id plus an off-instance ap_profile_id makes it remote.
        community.ap_id = 'community-f'
        community.ap_profile_id = 'https://otherinstance.example.org/c/community-f'
        db.session.commit()
        assert community.is_local() is False

        url = community.ap_profile_id
        assert rewrite_href(url) == f'/c/{community.link()}'

    def test_matching_url_shape_but_lookup_misses_returns_the_original_url(self, app, db_session):
        """Same shape (`/c/` without `/p/`), no Community row has this
        ap_profile_id. Fails if `if community` is deleted (None dereferenced)
        or the guard is over-broadened to rewrite regardless of a match."""
        instance, owner, community = _base('g')
        url = 'https://remote.example.com/c/no-such-community'

        assert rewrite_href(url) == url

    def test_matching_url_for_a_local_community_returns_the_original_url(self, app, db_session):
        """The community IS found by ap_profile_id, but it is local
        (make_community's default: ap_profile_id starts with SERVER_URL).
        Fails if `not community.is_local()` is deleted -- that mutation would
        rewrite a local community's own href."""
        instance, owner, community = _base('h')
        assert community.is_local() is True
        url = community.ap_profile_id

        assert rewrite_href(url) == url


class TestFallthroughElseRule:
    """`else: post = Post.get_by_ap_id(url); if post is None: post_reply =
    PostReply.get_by_ap_id(url); if post_reply: return f'/comment/{id}'`.

    Reached only when the URL matches none of the first three rules' shapes.
    Three cases: a PostReply match rewrites; no match of either kind returns
    the url unchanged; and a Post match (branch-covering the `if post is
    None:` guard's False arm) also returns the url unchanged, since this
    branch's own body never rewrites when a Post is found.
    """

    FALLTHROUGH_URL = 'https://remote.example.com/users/alice/statuses/321'

    def test_matching_url_with_a_reply_rewrites_to_the_comment_path(self, app, db_session):
        """Fails if the fallthrough rewrite (`if post_reply: return
        f'/comment/{post_reply.id}'`) is deleted."""
        instance, owner, community = _base('i')
        post = make_post(community, owner, ap_id='https://remote.example.com/post/321-parent')
        reply = make_post_reply(post, owner)
        reply.ap_id = self.FALLTHROUGH_URL
        db.session.commit()

        assert rewrite_href(self.FALLTHROUGH_URL) == f'/comment/{reply.id}'

    def test_matching_url_shape_but_neither_lookup_matches_returns_the_original_url(self, app, db_session):
        """Neither a Post nor a PostReply has this ap_id. Fails if the guard
        is over-broadened to rewrite (e.g. to a comment path) with no match."""
        instance, owner, community = _base('j')

        assert rewrite_href(self.FALLTHROUGH_URL) == self.FALLTHROUGH_URL

    def test_a_post_match_in_the_fallthrough_does_not_rewrite(self, app, db_session):
        """A Post row DOES have this ap_id (an unusual shape for a Post, but
        the function does not validate shape against the entity it looked up)
        -- `if post is None:` is then False, so the reply lookup is skipped
        entirely and the url returns unchanged. Branch-covers the False arm of
        `if post is None:` at line 4986, distinct from the miss case above
        where that arm is True.

        This pins a REPORTED DEFECT, not intended behaviour: `post =
        Post.get_by_ap_id(url)` here is used only as a null check to decide
        whether to fall through to the reply lookup -- the matched Post itself
        is never used to rewrite anything, so a Post match in this branch is
        silently inert. This test asserts CURRENT behaviour deliberately, so a
        future fix that makes the fallthrough also rewrite a matched Post's
        href (e.g. to post.slug / f'/post/{post.id}', mirroring the post rule)
        should change this test's expected value along with it, not be read as
        breaking a settled contract."""
        instance, owner, community = _base('k')
        make_post(community, owner, ap_id=self.FALLTHROUGH_URL)

        assert rewrite_href(self.FALLTHROUGH_URL) == self.FALLTHROUGH_URL
