"""Round 231: what the site RSS feed puts in front of a token holder.

`tests/test_main_index_rss.py` covers the token itself -- D1356, the banned and deleted
accounts whose feeds kept working. What it does not cover is the half of `index_rss` that
decides WHICH communities a request may read, and that half is where a private community
leaks if it leaks at all:

    1390          `private_communities = tuple(pc + [0])` -- the branch taken by a token
                  holder who IS a member of a private community
    1411-1415     the local feed's two arms: the anonymous SQL, which has a bare
                  `c.private is false`, and the authenticated one, which widens it with
                  `OR c.id IN (...)`

The `[0]` and the `tuple([0, 0])` fallbacks are not decoration. `private_communities` is
interpolated into the SQL as a Python tuple, and a one-element tuple renders as `(5,)`,
which PostgreSQL will not parse. Every arm therefore pads, and a row per arm is the only
way to notice a pad going missing -- the failure is a 500 on a feed, for the exact users
whose membership the padding exists to express.

Also here: the entry-level fields in the same function that no post in the suite had the
shape to reach -- a site logo, a site description, a post with a slug, and a post whose
`url` is a media file, which becomes an RSS `<enclosure>`.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Community, CommunityMember, Post, Site
from tests.factories import make_community, make_community_member, make_post, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    # `index_rss` aborts 404 on a private instance before any of this runs.
    site.private_instance = False
    g.site = site
    reader = make_user(api_baseline.instance_local, 'feedreader', local=True)
    reader.verified = True
    reader.private_key = 'x'
    reader.rss_token = 'r' * 20
    db.session.commit()
    return SimpleNamespace(app=app, client=app.test_client(), site=site, reader=reader,
                           baseline=api_baseline, token=reader.rss_token)


def a_private_community(name='innercircle'):
    community = make_community(name)
    community.private = True
    community.instance_id = 1
    db.session.commit()
    return community


def a_post_in(community, author, title, **kwargs):
    post = make_post(community, author, ap_id=f'https://test.piefed.local/p/{title}',
                     title=title, **kwargs)
    post.instance_id = 1
    db.session.commit()
    return post


# --------------------------------------------------------------------------
# Who may read a private community's posts
# --------------------------------------------------------------------------


class TestAPrivateCommunityInTheSiteFeed:
    """`Community.private` is invite-only access control, not a content warning. The local
    and popular feeds both express it as `c.private is false OR c.id IN (...)`, where the
    list is the token holder's own memberships -- so these rows are about the OR, from both
    sides.
    """

    @pytest.fixture
    def seeded(self, env):
        author = make_user(env.baseline.instance_local, 'feedauthor', local=True)
        public = make_community('townsquare')
        public.instance_id = 1
        private = a_private_community()
        db.session.commit()
        a_post_in(public, author, 'anyone-may-read-this')
        a_post_in(private, author, 'members-only-secret')
        db.session.commit()
        env.public = public
        env.private = private
        env.author = author
        return env

    def test_an_anonymous_reader_is_not_shown_the_private_community(self, seeded):
        """`:1412`. The anonymous SQL has no `OR` at all -- `c.private is false` and
        nothing else -- so this is the row that would fail first if the two arms were ever
        collapsed into one."""
        response = seeded.client.get('/index/feed/local')

        assert response.status_code == 200
        assert 'anyone-may-read-this' in response.text
        assert 'members-only-secret' not in response.text

    def test_a_token_holder_who_is_not_a_member_is_not_shown_it_either(self, seeded):
        """The authenticated arm with an EMPTY membership list. Holding a token widens
        nothing by itself, which is the whole point of the `OR` being driven by
        `community_membership_private` rather than by authentication."""
        response = seeded.client.get(f'/index/feed/local?token={seeded.token}')

        assert response.status_code == 200
        assert 'anyone-may-read-this' in response.text
        assert 'members-only-secret' not in response.text

    def test_a_member_with_a_token_is_not_shown_it_either(self, seeded):
        """`:1390` and `:1415`: a non-empty `pc` takes the `tuple(pc + [0])` branch and the
        widened SQL runs -- and the post STILL does not appear.

        `index_rss` widens its own `community_ids` query by the token holder's private
        memberships, but the posts themselves come from `get_deduped_post_ids`, which reads
        `current_user` -- and a token request is not a logged-in session, so `current_user`
        is anonymous there and its unconditional `c.private is false` applies. The widening
        in `index_rss` is therefore inert: two independent restrictions, the stricter one
        winning, which is the direction that fails safe.

        Asserted rather than described, because the comment at the top of this route
        (D1356) says the token 'went on delivering posts from the PRIVATE communities that
        account belonged to' -- true of the token check it was fixing, but no longer true
        of the feed as a whole.
        """
        make_community_member(seeded.reader, seeded.private)
        db.session.commit()

        response = seeded.client.get(f'/index/feed/local?token={seeded.token}')

        assert response.status_code == 200
        assert 'anyone-may-read-this' in response.text
        assert 'members-only-secret' not in response.text

    def test_a_banned_member_is_not_shown_it(self, seeded):
        """`community_membership_private` requires `cm.is_banned is false`, so a
        membership row is not on its own enough -- and a ban is the case where showing the
        posts anyway would be the leak that matters."""
        membership = make_community_member(seeded.reader, seeded.private)
        membership.is_banned = True
        db.session.commit()

        response = seeded.client.get(f'/index/feed/local?token={seeded.token}')

        assert response.status_code == 200
        assert 'members-only-secret' not in response.text

    def test_the_popular_feed_withholds_it_from_both(self, seeded):
        """The same pair of restrictions, in the other feed type. Two spellings of one rule
        is two places to get it wrong, so each is asserted rather than assumed to follow
        from the other."""
        seeded.private.show_popular = True
        seeded.public.show_popular = True
        db.session.commit()
        make_community_member(seeded.reader, seeded.private)
        db.session.commit()

        anonymous = seeded.client.get('/index/feed/popular')
        member = seeded.client.get(f'/index/feed/popular?token={seeded.token}')

        assert 'members-only-secret' not in anonymous.text
        assert 'members-only-secret' not in member.text


class TestTheTuplePadding:
    """`private_communities` is interpolated into the SQL as a Python tuple. A one-element
    tuple is `(5,)`, which PostgreSQL rejects, so every arm pads with a `0` that matches no
    community. These rows exist because the symptom of a missing pad is a 500 on the feed
    of exactly the accounts the padding is for -- one private community is the common case,
    not an edge one.
    """

    def test_a_member_of_exactly_one_private_community_gets_a_feed(self, env):
        """One membership is the case that needs the pad: without it the SQL reads
        `c.id IN (5,)` and PostgreSQL refuses to parse it. The assertion is a 200 and a
        well-formed feed, not the private post -- which `get_deduped_post_ids` withholds
        from a token request regardless."""
        author = make_user(env.baseline.instance_local, 'padauthor', local=True)
        public = make_community('openhouse')
        public.instance_id = 1
        private = a_private_community('onlyone')
        db.session.commit()
        a_post_in(public, author, 'ordinary-post')
        a_post_in(private, author, 'padded-correctly')
        make_community_member(env.reader, private)
        db.session.commit()

        response = env.client.get(f'/index/feed/local?token={env.token}')

        assert response.status_code == 200
        assert 'ordinary-post' in response.text
        assert 'padded-correctly' not in response.text

    def test_a_member_of_two_private_communities_also_gets_a_feed(self, env):
        """The case that needs no padding, kept beside the one that does: a two-element
        tuple renders legally on its own, so this row is what shows the pad is not what
        makes the query work."""
        author = make_user(env.baseline.instance_local, 'padauthor', local=True)
        public = make_community('openhouse')
        public.instance_id = 1
        first = a_private_community('firstcircle')
        second = a_private_community('secondcircle')
        db.session.commit()
        a_post_in(public, author, 'ordinary-post')
        a_post_in(first, author, 'first-secret')
        a_post_in(second, author, 'second-secret')
        make_community_member(env.reader, first)
        make_community_member(env.reader, second)
        db.session.commit()

        response = env.client.get(f'/index/feed/local?token={env.token}')

        assert response.status_code == 200
        assert 'ordinary-post' in response.text


# --------------------------------------------------------------------------
# The channel and entry fields
# --------------------------------------------------------------------------


class TestTheFeedsOwnDescription:

    def test_the_site_logo_becomes_the_feed_logo(self, env):
        """`:1434`. The `else` arm -- a default apple-touch-icon -- is what every other row
        in the suite takes, because the fixture site has no logo."""
        # `Site.logo` is varchar(40), so it holds a path rather than a URL.
        env.site.logo = '/static/images/sitelogo.png'
        db.session.commit()

        response = env.client.get('/index/feed')

        assert '/static/images/sitelogo.png' in response.text
        assert 'apple-touch-icon' not in response.text

    def test_the_site_description_becomes_the_subtitle(self, env):
        """`:1438`. Its `else` writes a single space, because feedgen requires the element
        to be present -- so a row that only checked for a `<subtitle>` would pass either
        way."""
        env.site.description = 'A place for testing things'
        db.session.commit()

        response = env.client.get('/index/feed')

        assert 'A place for testing things' in response.text

    def test_a_long_description_is_shortened(self, env):
        """`shorten_string(..., 150)` sits on the same line, and a feed is a place where an
        unbounded site description would be copied verbatim into every reader."""
        # `Site.description` is varchar(256), so 200 is the longest round number that
        # fits and still exceeds the 150 the feed shortens to.
        env.site.description = 'x' * 200
        db.session.commit()

        response = env.client.get('/index/feed')

        assert 'x' * 200 not in response.text
        assert 'x' * 140 in response.text


class TestWhatAnEntryCarries:

    @pytest.fixture
    def seeded(self, env):
        author = make_user(env.baseline.instance_local, 'entryauthor', local=True)
        community = make_community('entryland')
        community.instance_id = 1
        db.session.commit()
        env.author = author
        env.community = community
        return env

    def test_a_post_with_a_slug_is_linked_by_its_slug(self, seeded):
        """`:1448`. The `else` builds `/post/<id>`, so the two arms give a reader different
        URLs for the same post and only one of them is the canonical one."""
        post = a_post_in(seeded.community, seeded.author, 'has-a-slug')
        post.slug = '/post/has-a-slug'
        db.session.commit()

        response = seeded.client.get('/index/feed')

        assert 'https://test.piefed.local/post/has-a-slug' in response.text

    def test_a_post_linking_to_media_carries_an_enclosure(self, seeded):
        """`:1452-1454`. An `<enclosure>` is what makes a podcast client download the file,
        so it is added only when the linked URL is not text -- which is why `mimetype_from_url`
        is consulted rather than the presence of a URL."""
        post = a_post_in(seeded.community, seeded.author, 'has-audio')
        post.url = 'https://media.example/episode.mp3'
        db.session.commit()

        response = seeded.client.get('/index/feed')

        assert 'episode.mp3' in response.text
        assert 'enclosure' in response.text

    def test_a_post_linking_to_a_web_page_carries_no_enclosure(self, seeded):
        """The `and not type.startswith('text/')` half. A link post pointing at an article
        is the common case, and turning every one of them into an enclosure would tell
        every podcast client to download the HTML."""
        post = a_post_in(seeded.community, seeded.author, 'has-article')
        post.url = 'https://news.example/story.html'
        db.session.commit()

        response = seeded.client.get('/index/feed')

        assert 'has-article' in response.text
        assert 'enclosure' not in response.text

    def test_a_url_with_no_recognisable_type_carries_no_enclosure(self, seeded):
        """The `type and` half, which is the arm a URL with no extension takes:
        `mimetype_from_url` answers None and `None.startswith` would be the crash."""
        post = a_post_in(seeded.community, seeded.author, 'has-bare-url')
        post.url = 'https://example.com/some/path'
        db.session.commit()

        response = seeded.client.get('/index/feed')

        assert 'has-bare-url' in response.text
        assert 'enclosure' not in response.text


class TestEachFeedTypeAnswersWithItsOwnCommunities:
    """D1419. `/index/feed/<feed_type>` names four feeds, and a reader without a token got
    the LOCAL one whichever they asked for -- the chain's second arm was
    `feed_type == 'local' or not current_user_is_authenticated`, so it caught everything.
    The title still said 'Popular' or 'All', which is why the existing rows, which assert
    titles, did not notice.

    Each row here asks for a feed type and asserts a community that only THAT type
    includes, which is the assertion the title cannot make.
    """

    @pytest.fixture
    def seeded(self, env):
        author = make_user(env.baseline.instance_local, 'typeauthor', local=True)
        local = make_community('homebrew')
        local.instance_id = 1
        local.show_popular = False
        remote = make_community('faraway', host='remote.example')
        remote.instance_id = env.baseline.instance_remote.id
        remote.show_popular = True
        db.session.commit()
        a_post_in(local, author, 'local-only-post')
        remote_post = a_post_in(remote, author, 'remote-only-post')
        remote_post.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        env.author = author
        return env

    def test_an_anonymous_popular_feed_is_not_the_local_feed(self, seeded):
        """The regression. `homebrew` has `show_popular` false and `faraway` has it true,
        so a popular feed that still contains the local community is the local feed wearing
        a different title."""
        response = seeded.client.get('/index/feed/popular')

        assert response.status_code == 200
        assert 'remote-only-post' in response.text
        assert 'local-only-post' not in response.text

    def test_an_anonymous_all_feed_includes_remote_communities(self, seeded):
        """`all` is the `[-1]` sentinel, which `get_deduped_post_ids` reads as 'every
        community'. Before D1419 an anonymous reader got only `instance_id = 1`.

        A mutant that deletes the `elif feed_type == 'all'` arm entirely survives, and
        provably so: `community_ids` is initialised to `[-1]` at the top of the function,
        so the arm restates the value a request would have anyway. It is kept because it
        says which types the chain knows about -- without it, `all` and a typo would be the
        same branch -- and noted here so the survivor is not mistaken for a missing row.
        """
        response = seeded.client.get('/index/feed/all')

        assert 'local-only-post' in response.text
        assert 'remote-only-post' in response.text

    def test_the_local_feed_is_still_local(self, seeded):
        """The control. The reordering must not widen the one type that is meant to be
        narrow."""
        response = seeded.client.get('/index/feed/local')

        assert 'local-only-post' in response.text
        assert 'remote-only-post' not in response.text

    def test_a_token_holder_gets_the_same_three(self, seeded):
        """The authenticated arms of the same three types, which were already reachable --
        kept so that a later change cannot fix one viewer and break the other."""
        local = seeded.client.get(f'/index/feed/local?token={seeded.token}')
        popular = seeded.client.get(f'/index/feed/popular?token={seeded.token}')
        every = seeded.client.get(f'/index/feed/all?token={seeded.token}')

        assert 'remote-only-post' not in local.text
        assert 'local-only-post' not in popular.text
        assert 'remote-only-post' in every.text

    def test_subscribed_without_a_token_falls_back_to_local(self, seeded):
        """`subscribed` is the one type that genuinely needs a token, so it is the one type
        whose anonymous answer is still the local feed -- now by naming it in the `local`
        arm rather than by catching every unauthenticated request."""
        response = seeded.client.get('/index/feed/subscribed')

        assert 'local-only-post' in response.text
        assert 'remote-only-post' not in response.text

    def test_an_unknown_feed_type_is_answered_as_all_for_either_viewer(self, seeded):
        """`tests/test_main_index_rss.py` says a guessed feed name falls off the chain and
        is answered as All. That was true of a token holder and NOT of an anonymous reader,
        who took the catch-all `local` arm; the title it asserts is the same either way. It
        is true of both now."""
        anonymous = seeded.client.get('/index/feed/FOO')
        with_token = seeded.client.get(f'/index/feed/FOO?token={seeded.token}')

        for response in (anonymous, with_token):
            assert response.status_code == 200
            assert 'local-only-post' in response.text
            assert 'remote-only-post' in response.text
