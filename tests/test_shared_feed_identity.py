"""A local feed's name and its ActivityPub identity: `make_feed` and `edit_feed`
in `app/shared/feed.py`.

A local Feed row carries six fields that all encode the same thing -- the feed's
actor name:

    name, machine_name
    ap_profile_id, ap_public_url, ap_followers_url, ap_following_url, ap_outbox_url

`make_feed` builds the five URLs from the submitted `url`, and `app/feed/routes.py`
serves the feed at `/f/<name>`, so `name` is not a label: it is the path the URLs
have to resolve to. Nothing kept the six in step.

D1371, measured with a probe that created a feed as "MyFeed" and renamed it to
"RenamedFeed":

    after creation        name = MyFeed
                          ap_profile_id  = https://test.piefed.local/f/myfeed
                          ap_public_url  = https://test.piefed.local/f/MyFeed

    after edit_feed       name = renamedfeed
                          ap_profile_id  = https://test.piefed.local/f/myfeed
                          ap_public_url  = https://test.piefed.local/f/MyFeed

Two separate faults:

* creation lowercased `ap_profile_id` alone and left the other four at the raw
  submitted spelling, so one actor had two ids from birth. An `id` and a `url`
  that differ in case are different URLs to every remote server that dereferences
  them, and `find_actor_or_create` matches on `ap_profile_id.lower()`, so the
  spelling a peer received from `ap_public_url` did not match the actor it could
  look up;
* `edit_feed` slugified and lowercased the name and wrote `name` and
  `machine_name`, and touched none of the five URLs -- so after ANY edit that
  passes a url, a local feed's name and its whole identity were unrelated. The
  feed answered at `/f/renamedfeed` while telling every peer it was
  `/f/myfeed`, a path the rename had made 404.

This is D1370's shape (renaming a community in the admin form left its
ActivityPub identity behind) one level out, and reachable by a wider caller: a
community rename is admin-only, and any feed owner may rename their own feed
through `PUT /api/alpha/feed`.

WHY THE TWO CONDITIONS. The rewrite is restricted to a feed that is local AND
public. A remote feed's URLs are the publishing server's to mint, and `is_local()`
is read BEFORE the URLs are rewritten, so it answers "was this ours" rather than
being decided by the write it is guarding. A private feed is not federated and
`edit_feed` gives it a name of `<url>/<owner>`, which cannot appear in a
single-segment `/f/<actor>` path -- minting `/f/a/b` would be inventing an actor
URL that nothing serves.
"""
import pytest
from flask import current_app, g

from app import db
from app.constants import SRC_API
from app.models import Feed, Site, User
from app.shared.feed import edit_feed, make_feed
from tests.factories import make_instance, make_local_feed, make_user


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def payload(url, title='A feed', public=True, **overrides):
    """Every key `make_feed`/`edit_feed` read off an API request. They index the
    dict directly, so a missing key is a KeyError rather than a default."""
    body = {'url': url, 'title': title, 'public': public, 'description': '',
            'icon_url': None, 'banner_url': None, 'nsfw': False, 'nsfl': False,
            'communities': '', 'is_instance_feed': False,
            'show_child_posts': True, 'parent_feed_id': None}
    body.update(overrides)
    return body


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.enable_nsfw = True
    g.site.enable_nsfl = True
    db.session.commit()
    return api_baseline.user2


def urls(feed):
    return {'ap_profile_id': feed.ap_profile_id,
            'ap_public_url': feed.ap_public_url,
            'ap_followers_url': feed.ap_followers_url,
            'ap_following_url': feed.ap_following_url,
            'ap_outbox_url': feed.ap_outbox_url}


def expected_urls(name):
    base = f"https://{current_app.config['SERVER_NAME']}/f/{name}"
    return {'ap_profile_id': base, 'ap_public_url': base,
            'ap_followers_url': f'{base}/followers',
            'ap_following_url': f'{base}/following',
            'ap_outbox_url': f'{base}/outbox'}


def created(env, url, **overrides):
    make_feed(payload(url, **overrides), SRC_API, auth=token(env))
    return Feed.query.order_by(Feed.id.desc()).first()


# --------------------------------------------------------------------------
# The invariant, stated once
# --------------------------------------------------------------------------


def assert_identity_is_consistent(feed):
    """What every test below is really asserting: for a local public feed, the
    name and all five URLs say the same thing.

    Written as one function so a sixth URL column added to Feed has one place to
    be added, and so each test names the state it puts the feed in rather than
    repeating five comparisons.
    """
    assert feed.machine_name == feed.name, 'machine_name drifted from name'
    assert urls(feed) == expected_urls(feed.name)
    assert feed.ap_domain == current_app.config['SERVER_NAME']
    # is_local() is computed FROM ap_profile_id (app/models.py), so a rewrite
    # that put a foreign host in it would quietly turn a local feed remote --
    # and remote feeds are excluded from the rewrite, so the damage would stick.
    assert feed.is_local()


# --------------------------------------------------------------------------
# Creation
# --------------------------------------------------------------------------


class TestCreation:
    @pytest.mark.parametrize('url', [
        'myfeed',           # already normalised: the case that always worked
        'MyFeed',           # D1371: ap_profile_id lowercased, four URLs were not
        'My Feed',          # slugify turns the space into the separator
        'my-feed',          # and the dash likewise
        'Mixed_Case_2',
        '  spaced  ',       # .strip()
        'a/b',              # .split('/')[0] -- only the first segment is a name
        'ÜberFeed',         # slugify transliterates
    ])
    def test_the_name_and_every_url_agree(self, env, url):
        feed = created(env, url)

        assert_identity_is_consistent(feed)

    def test_the_two_spellings_D1371_produced(self, env):
        """The measured defect, asserted as itself rather than through the
        invariant: `id` and `url` were two different URLs for one actor."""
        feed = created(env, 'MyFeed')

        assert feed.ap_profile_id == feed.ap_public_url
        assert feed.ap_public_url == 'https://test.piefed.local/f/myfeed'

    @pytest.mark.parametrize('url, name', [
        ('MyFeed', 'myfeed'),
        ('My Feed', 'my_feed'),
        ('a/b', 'a'),        # only the first segment is a name
        ('a/b/c', 'a'),
        ('  spaced  ', 'spaced'),
    ])
    def test_the_name_the_slug_rule_produces(self, env, url, name):
        """The invariant above says the six fields agree; this says WHAT they
        agree on. Without it, a rule that slugified `a/b` to `a_b` would satisfy
        the invariant perfectly while minting a two-segment actor."""
        assert created(env, url).name == name

    def test_the_name_is_the_path_the_urls_point_at(self, env):
        """`name` is what `/f/<name>` routes on, so the URLs resolving to some
        other spelling is the whole fault, not a cosmetic one."""
        feed = created(env, 'MyFeed')

        assert feed.ap_public_url.endswith(f'/f/{feed.name}')
        assert feed.name == 'myfeed'

    def test_a_private_feed_is_named_after_its_owner(self, env):
        """The owner suffix is part of the rule `make_feed` now owns, so a feed
        created private gets it whichever caller asked. `post_feed` used to
        append it before calling and `make_feed` applied none of the rule, so
        the name depended on the entry point."""
        feed = created(env, 'SecretFeed', public=False)

        assert feed.name == f'secretfeed/{env.user_name.lower()}'

    def test_a_private_feeds_name_still_reaches_post_feed_unchanged(self, env):
        """`post_feed` looked the new row up by re-deriving the name it had just
        computed. It takes `make_feed`'s return value now; this is the end-to-end
        proof that dropping its copy of the rule left the API answer intact."""
        from app.api.alpha.utils.feed import post_feed

        answer = post_feed(token(env), {'name': 'SecretFeed', 'title': 'Secret',
                                        'public': False})

        assert answer['name'] == f'secretfeed/{env.user_name.lower()}'


# --------------------------------------------------------------------------
# Renaming
# --------------------------------------------------------------------------


class TestRenamingALocalPublicFeed:
    def test_every_url_follows_the_new_name(self, env):
        feed = created(env, 'MyFeed')

        edit_feed(payload('RenamedFeed'), feed, SRC_API, auth=token(env))

        assert feed.name == 'renamedfeed'
        assert_identity_is_consistent(feed)

    def test_nothing_of_the_old_name_survives(self, env):
        """D1371 left `/f/myfeed` in five columns. A rewrite that missed one
        would still pass a check of the new name alone."""
        feed = created(env, 'MyFeed')

        edit_feed(payload('RenamedFeed'), feed, SRC_API, auth=token(env))
        db.session.commit()

        row = db.session.get(Feed, feed.id)
        assert 'myfeed' not in ' '.join(urls(row).values())

    @pytest.mark.parametrize('new_url', ['second', 'Third Name', 'fourth/ignored'])
    def test_it_holds_after_a_further_rename(self, env, new_url):
        """Renaming twice: the second rename reads URLs the first one wrote, so
        a rewrite that only worked from the creation spelling fails here."""
        feed = created(env, 'MyFeed')
        edit_feed(payload('Interim'), feed, SRC_API, auth=token(env))

        edit_feed(payload(new_url), feed, SRC_API, auth=token(env))

        assert_identity_is_consistent(feed)

    def test_a_stale_ap_domain_is_repaired(self, env):
        """`ap_domain` is written with the five URLs. For a feed whose domain is
        already this server the write is invisible, so it is asserted against the
        one state where it shows: a local row carrying some other domain, which
        is what a restored backup or a changed SERVER_NAME leaves behind.
        `ap_domain` is what `feed_view` reports and what the actor lookups
        filter on, so a local feed claiming a foreign domain is not cosmetic."""
        feed = created(env, 'MyFeed')
        feed.ap_domain = 'previous.piefed.local'
        db.session.commit()

        edit_feed(payload('RenamedFeed'), feed, SRC_API, auth=token(env))

        assert feed.ap_domain == current_app.config['SERVER_NAME']

    def test_renaming_to_the_same_name_changes_nothing(self, env):
        """The no-op path. `feed.name != url` guards the rewrite; without a row
        here, a rewrite that ran unconditionally would look identical."""
        feed = created(env, 'MyFeed')
        before = urls(feed)

        edit_feed(payload('MyFeed'), feed, SRC_API, auth=token(env))

        assert urls(feed) == before

    def test_an_edit_that_passes_no_url_leaves_the_name_alone(self, env):
        """`if url:` -- an edit of the title only must not touch identity."""
        feed = created(env, 'MyFeed')
        before = urls(feed)

        edit_feed(payload('', title='A new title'), feed, SRC_API,
                  auth=token(env))

        assert feed.name == 'myfeed'
        assert urls(feed) == before
        assert feed.title == 'A new title'


class TestWhatIsDeliberatelyNotRewritten:
    def test_a_remote_feeds_urls_are_left_alone(self, env):
        """A remote feed's URLs belong to the server that publishes it. This
        path is reachable: `edit_feed`'s only gate is ownership, and an admin
        passes it for any feed row, including a remote one."""
        instance = make_instance('remote.piefed.local')
        owner = make_user(instance, 'remoteowner')
        feed = Feed(user_id=owner.id, title='Remote', name='remotefeed',
                    machine_name='remotefeed', public=True,
                    ap_id='remotefeed@remote.piefed.local',
                    ap_profile_id='https://remote.piefed.local/f/remotefeed',
                    ap_public_url='https://remote.piefed.local/f/remotefeed',
                    ap_followers_url='https://remote.piefed.local/f/remotefeed/followers',
                    ap_following_url='https://remote.piefed.local/f/remotefeed/following',
                    ap_outbox_url='https://remote.piefed.local/f/remotefeed/outbox',
                    ap_domain='remote.piefed.local', instance_id=instance.id)
        db.session.add(feed)
        db.session.commit()
        before = urls(feed)
        assert not feed.is_local()

        admin = db.session.get(User, 1)  # User.is_admin() is true for id 1
        assert admin.is_admin()

        edit_feed(payload('RenamedFeed'), feed, SRC_API, auth=token(admin))

        assert urls(feed) == before
        assert feed.ap_domain == 'remote.piefed.local'

    def test_a_private_feed_keeps_its_urls(self, env):
        """A private feed's name gains a `/<owner>` suffix, which no `/f/<actor>`
        path can serve, and it is not federated. Rewriting would mint
        `/f/secretfeed/<owner>` -- a URL for an actor that does not exist."""
        feed = created(env, 'MyFeed')
        before = urls(feed)

        edit_feed(payload('SecretFeed', public=False), feed, SRC_API,
                  auth=token(env))

        assert feed.name == f'secretfeed/{env.user_name.lower()}'
        assert urls(feed) == before
        assert '/f/secretfeed/' not in ' '.join(urls(feed).values())

    def test_a_private_feed_made_public_again_is_rewritten(self, env):
        """The other side of that condition: once the feed is public its name is
        a bare slug again, so identity must catch up. Without this row, a guard
        of `public` that never let anything through would still pass."""
        feed = created(env, 'MyFeed')
        edit_feed(payload('SecretFeed', public=False), feed, SRC_API,
                  auth=token(env))

        edit_feed(payload('PublicAgain', public=True), feed, SRC_API,
                  auth=token(env))

        assert feed.name == 'publicagain'
        assert_identity_is_consistent(feed)


class TestAgainstAFeedMadeTheOtherWay:
    """`make_local_feed` (tests/factories.py) is what the rest of the suite
    builds local feeds with. If its rows do not satisfy the invariant, every
    other feed test is asserting against a state the product cannot produce.
    """

    def test_the_factorys_local_feed_is_consistent(self, app, api_baseline):
        feed = make_local_feed('factoryfeed', public=True)
        db.session.commit()

        assert_identity_is_consistent(feed)


class TestTheWidthOfTheName:
    """`Feed.machine_name` is String(50) and `Feed.name` is String(256), and
    `make_feed` writes both from one value. The web form caps its own field at 50
    (app/feed/forms.py:68) and nothing capped the API arm, so a longer name
    reached the commit and raised DataError -- D1372's measurement on the remote
    side of the same two columns.
    """

    def test_a_long_name_is_cut_to_the_narrower_column(self, env):
        feed = created(env, 'n' * 300)

        assert feed.name == 'n' * 50
        assert_identity_is_consistent(feed)

    def test_renaming_to_a_long_name_is_cut_too(self, env):
        feed = created(env, 'MyFeed')

        edit_feed(payload('r' * 300), feed, SRC_API, auth=token(env))

        assert feed.name == 'r' * 50
        assert_identity_is_consistent(feed)

    def test_a_private_feeds_owner_suffix_counts_towards_the_width(self, env):
        """The suffix is appended before the cut, so `name` and `machine_name`
        still agree -- which is the invariant that matters, since one of them is
        what the feed is served at."""
        feed = created(env, 'n' * 60, public=False)

        assert feed.name == feed.machine_name
        assert len(feed.name) == 50
