"""Round 274: the last scattered lines, across six modules.

Each is one line in a different file, so this round is grouped by what the line protects.

    a 404 rather than a page   `topic/routes.py` and `feed/routes.py` each end a lookup with
                              `abort(404)`. Without it the template renders with None and the
                              failure becomes a 500, which is the difference between "no such topic"
                              and "this instance is broken".
    an upload that produced nothing   `shared/upload.py` raises rather than returning a null url,
                              because the caller stores what it is given as an image url.
    a flair with no id        `shared/community.py` refuses to publish a flair it cannot name.
    a `/f/` prefix a person pasted   `feed/routes.py` strips it twice, on create and on copy, so a
                              pasted feed url becomes a feed name rather than a nested path.
    three delegations         `Site.active_daily` / `active_weekly` / `active_6monthly`, the numbers
                              on the instance's own about page and in its nodeinfo.
    two small readers         `Post.get_by_slug` and `Post.url_domain`.
"""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Community, File, Post, Site, User
from tests.factories import (make_community, make_community_member, make_instance, make_post,
                             make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('finalland')
    author = make_user(api_baseline.instance_local, 'finalauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# A 404 rather than a broken page
# --------------------------------------------------------------------------


class TestAskingForSomethingThatIsNotThere:
    """Both routes carry `@check_anoobis`, which redirects an ANONYMOUS visitor to the
    proof-of-work challenge (round 261) -- so these rows sign in, or every one of them is a 302 that
    says nothing about the lookup.

    WHAT THESE ROWS ASSERT, precisely: the user-visible answer for a topic or feed that is not here.
    They do NOT distinguish `topic/routes.py:212`'s and `feed/routes.py:616`'s `abort(404)`, and the
    mutation pass says so -- replacing either with `pass` leaves these rows green, because the 404
    they see comes from the ROUTER. `/f/<actor>` is registered in `app/activitypub/routes.py`, which
    calls `show_feed` only for a feed it has already found, and an unmatched topic path never reaches
    the view at all. Those two aborts are the arm for a path the router matched and the lookup inside
    did not resolve; reaching it needs a request that gets past the router's own answer, which no row
    here does. Recorded rather than left looking covered.
    """

    @pytest.fixture
    def client(self, env):
        client = env.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(env.author.id)
            session['_fresh'] = True
        return client

    def test_an_unknown_topic_is_a_404(self, env, client):
        """`topic/routes.py`'s `else: abort(404)`. The template above it is handed the topic's own
        fields, so without the abort a wrong path renders with None and answers 500 -- and a 500
        tells a reader the instance is broken rather than that the topic does not exist. Search
        engines read the difference too.
        """
        response = client.get('/topic/no-such-topic-at-all')

        assert response.status_code == 404

    def test_an_unknown_feed_is_a_404(self, env, client):
        """The same decision in `feed/routes.py`, for a feed path."""
        response = client.get('/f/no-such-feed-at-all')

        assert response.status_code == 404

    def test_a_topic_that_exists_is_a_page(self, env, client):
        """The control for the abort: a real path must not 404, or the row above would pass for a
        route that is broken for everybody."""
        from app.models import Topic

        topic = Topic(name='A Real Topic', machine_name='a-real-topic', num_communities=0)
        db.session.add(topic)
        db.session.commit()

        response = client.get('/topic/a-real-topic')

        assert response.status_code == 200


# --------------------------------------------------------------------------
# An upload that produced nothing
# --------------------------------------------------------------------------


class TestAnUploadThatProducedNoUrl:
    """`if not url: raise Exception('unable to process upload')` is UNREACHABLE, and this row records
    why rather than chasing it.

    `url` is assigned unconditionally at `app/shared/upload.py:86`, immediately after the file is
    written and its size read, and every path that does not reach that line raises first --
    `filetype not allowed`, `SVG file could not be sanitized`, or `file not uploaded` above it. The
    S3 branch below reassigns it. So there is no way to arrive at the final guard with `url` unset.
    Kept as a belt-and-braces check on a function whose answer is stored as an image url, but not
    coverable. Fact 781's shape, in the plain-control-flow version rather than the schema one.
    """

    def test_an_upload_with_no_file_raises_before_that_guard(self, env):
        """The raise that DOES fire for an empty upload, three arms above it."""
        from werkzeug.datastructures import FileStorage

        from app.shared.upload import process_upload

        with pytest.raises(Exception, match='file not uploaded'):
            process_upload(FileStorage(filename='', stream=None), env.author)


# --------------------------------------------------------------------------
# A flair this instance cannot name
# --------------------------------------------------------------------------


class TestPublishingAFlairWithNoId:

    def test_a_flair_whose_id_cannot_be_derived_is_not_published(self, env):
        """`if not ap_id: return`. The `id` is what a peer uses to refer to the flair afterwards --
        `update_community_flair_from_tags` matches on it (round 260) -- so publishing an entry
        without one would give every peer a flair they cannot reconcile later.
        """
        from app.shared.community import comm_flair_ap_format
        from tests.factories import make_community_flair

        flair = make_community_flair(env.community, 'Spoilers')
        flair.ap_id = None
        db.session.commit()

        with patch.object(type(flair), 'get_ap_id', return_value=None):
            assert comm_flair_ap_format(flair) is None

    def test_a_flair_with_a_derivable_id_is_published(self, env):
        from app.shared.community import comm_flair_ap_format
        from tests.factories import make_community_flair

        flair = make_community_flair(env.community, 'Spoilers')
        db.session.commit()

        published = comm_flair_ap_format(flair)

        assert published['type'] == 'CommunityPostTag'
        assert published['preferredUsername'] == 'Spoilers'
        assert published['id']


# --------------------------------------------------------------------------
# A feed url somebody pasted
# --------------------------------------------------------------------------


# `communities` carries DataRequired, and an invalid form re-renders a template that references
# `form.csrf_token` -- which WTF_CSRF_ENABLED=False removes -- so an incomplete payload fails as a
# Jinja error rather than as a form error. One community per line is what the field expects.
COMMUNITIES = '!finalland@test.piefed.local'


class TestAFeedUrlSomebodyPasted:
    """D1436, RECORDED AND NOT FIXED. `feed/routes.py:58` and `:250` strip a leading `/f/` from a
    pasted feed url, and NEITHER CAN RUN: both sit after `form.validate_on_submit()`, and
    `AddCopyFeedForm.validate` calls `apply_feed_url_rules`, which for a public feed does

        elif self.public.data and '/' in self.url.data.strip():
            self.url.data = self.url.data.strip().split('/', 1)[0]

    -- and `'/f/pasted_feed'.split('/', 1)[0]` is `''`, which then fails the
    `^[a-zA-Z0-9_]+$` charset check. So somebody pasting the address out of their URL bar gets "Url
    is invalid" rather than the strip the view was written to do, and the two lines are dead.

    Fixing it means moving the strip into the form, ahead of `apply_feed_url_rules`, or teaching that
    helper about the prefix -- a change to validation ORDER on a form that also serves the copy and
    edit paths, which needs a maintainer's decision rather than a coverage round's. The rows below
    pin what happens TODAY, deliberately: the plain name works, and the pasted one is refused.
    """

    def _signed_in(self, env):
        """A signed-in client and a CSRF token (fact 355).

        The token has to be generated in one request context and its raw value written into the
        client's session. And the payload must VALIDATE: an invalid form re-renders a template that
        references `form.csrf_token`, which `WTF_CSRF_ENABLED = False` removes, so an incomplete
        payload fails as a Jinja error rather than as a form error.
        """
        from flask import session as flask_session
        from flask_wtf.csrf import generate_csrf

        client = env.app.test_client()
        env.author.verified = True
        env.author.private_key = '-----BEGIN PRIVATE KEY-----x'
        db.session.commit()
        with env.app.test_request_context():
            token = generate_csrf()
            raw = flask_session['csrf_token']
        with client.session_transaction() as session:
            session['_user_id'] = str(env.author.id)
            session['_fresh'] = True
            session['csrf_token'] = raw
        return client, token

    def test_a_plain_feed_name_is_accepted(self, env):
        from app.models import Feed

        client, token = self._signed_in(env)

        client.post('/feed/new', data={
            'csrf_token': token, 'title': 'Plain Feed', 'url': 'plain_feed',
            'description': '', 'parent_feed_id': 0,
            'communities': COMMUNITIES,
            'public': 'y', 'submit': 'Save'}, follow_redirects=False)

        assert db.session.query(Feed).filter_by(name='plain_feed').count() == 1

    def test_a_pasted_url_is_refused_rather_than_stripped(self, env):
        """The observed behaviour of D1436, asserted so that fixing it is a visible change: no feed
        is created, because validation rejected the url before the view's strip could run."""
        from app.models import Feed

        client, token = self._signed_in(env)
        before = db.session.query(Feed).count()

        with pytest.raises(Exception):
            # The re-render is the Jinja failure described above, which is itself the evidence that
            # validation refused the form rather than the view handling the prefix.
            client.post('/feed/new', data={
                'csrf_token': token, 'title': 'Pasted Feed', 'url': '/f/pasted_feed',
                'description': '', 'parent_feed_id': 0,
                'communities': COMMUNITIES,
                'public': 'y', 'submit': 'Save'}, follow_redirects=False)

        assert db.session.query(Feed).count() == before
        assert db.session.query(Feed).filter_by(name='pasted_feed').count() == 0


# --------------------------------------------------------------------------
# The numbers on the about page
# --------------------------------------------------------------------------


class TestTheActiveUserCounts:
    """`Site.active_daily`, `active_weekly` and `active_6monthly` each delegate to one of
    `app/activitypub/util.py`'s counters. They are published in nodeinfo and rendered on the about
    page, so a delegation pointing at the wrong window misreports the instance's size to every peer
    that reads it -- and the windows are what those numbers MEAN.
    """

    def test_each_reader_delegates_to_its_own_window(self, env):
        from app.activitypub import util as ap_util

        site = db.session.get(Site, 1)
        with patch.object(ap_util, 'active_day', return_value=11), \
                patch.object(ap_util, 'active_week', return_value=22), \
                patch.object(ap_util, 'active_month', return_value=33), \
                patch.object(ap_util, 'active_half_year', return_value=44):
            assert site.active_daily() == 11
            assert site.active_weekly() == 22
            assert site.active_monthly() == 33
            assert site.active_6monthly() == 44

    def test_the_counts_are_real_numbers_without_the_doubles(self, env):
        """The control: each counter runs its own SQL, so the row above would pass even if all four
        were broken. This one says they answer."""
        site = db.session.get(Site, 1)

        for value in (site.active_daily(), site.active_weekly(), site.active_monthly(),
                      site.active_6monthly()):
            assert isinstance(value, int)


class TestTwoSmallPostReaders:

    def test_a_post_is_found_by_its_slug(self, env):
        """`Post.get_by_slug` is how a friendly url resolves to a post, so it is on the path of
        every link a peer federated out with `post_url_type = 'friendly'`."""
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/sl/1')
        post.slug = 'a-findable-slug'
        db.session.commit()

        assert Post.get_by_slug('a-findable-slug').id == post.id
        assert Post.get_by_slug('no-such-slug') is None

    def test_a_posts_url_domain_is_its_host(self, env):
        """`url_domain` is rendered beside a link post and is what a reader judges the source by, so
        it has to be the HOST rather than anything else in the url -- a path or a query that looked
        like a domain would misattribute the link."""
        post = make_post(env.community, env.author, ap_id='https://test.piefed.local/sl/2')
        post.url = 'https://news.example/section/story?utm_source=elsewhere.example'
        db.session.commit()

        assert post.url_domain() == 'https://news.example/'
