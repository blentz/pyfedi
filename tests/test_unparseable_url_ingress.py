"""Reject a URL urlparse cannot read at INGRESS, so it is never stored.

The second half of a two-layer defence. The first half (commit 43138ac7)
guarded the RENDER path, because production databases may already hold such
rows and no amount of new ingress validation cleans them retroactively. This
half stops new ones arriving.

Why the ingress hole exists at all, and why it is this branch's own doing:
commits 180d9435 and 2e5265b5 guarded eleven functions so urlparse's
ValueError no longer escapes -- each returns a safe value instead of raising.
That fixed real 500s. It also opened this hole, in
`CreateLinkForm.validate_link_url`:

    domain = domain_from_url(field.data, create=False)
    if domain and domain.banned:
        ...
        return False
    return True

Before the guard, `domain_from_url` RAISED on a malformed URL -- an ugly 500 at
submission, but the URL never persisted. After the guard it returns None, so
`if domain and domain.banned` is False and the validator returns True. The URL
is accepted and stored. `domain_from_url(create=False)` returns None for two
different reasons -- "unparseable" and "parseable but no Domain row yet" -- so
the validator could not tell them apart without an explicit parseability check
ahead of the domain lookup. `app.utils.url_is_parseable` is that check.

**The attack URL is 'https://youtube.com[abc', and the choice matters.**
The obvious 'http://[abc/youtube.com' is WRONG for this: it types as
POST_TYPE_LINK, so it never reaches the video render path, and an end-to-end
test built on it passes against unfixed code. 'https://youtube.com[abc' clears
every gate instead -- `'youtube.com' in url` is True, urlparse raises
ValueError('Invalid IPv6 URL'), and `is_video_hosting_site` matches it on
startswith('https://youtube.com'), so app/models.py:1982 and
app/shared/post.py:654 type it POST_TYPE_VIDEO of their own accord.
TestTheCraftedUrlReallyDoesClearEveryGate asserts all of that rather than
taking it on trust, so no test here is arranging its own outcome.

**The over-correction guard is the class that matters most.** A parseability
check that is too strict would reject legitimate URLs and still pass every
rejection test in this file. TestOrdinaryUrlsAreStillAccepted pins ports,
userinfo, IDN, punycode, percent-encoding and a well-formed IPv6 literal for
exactly that reason.

**Federation stores None, it does not reject the activity.** Dropping a peer's
whole post because its url will not parse would hand peers a way to make us
drop content. None rather than '' because None is what `Post.url` holds for
every post that has no url, and because `post_to_page`
(app/activitypub/util.py:170) is the one consumer that distinguishes them:
`post.url is not None` gates the outbound attachment, so '' would federate
`{"href": "", "type": "Link"}` out again while None federates nothing. Every
other consumer in app/ reads post.url for truth ('if post.url:'), which treats
the two alike.
"""

import re
from urllib.parse import urlparse

import pytest
from flask import g

from app import db
from app.activitypub.util import update_post_from_activity
from app.community.forms import CreateLinkForm, CreateVideoForm
from app.models import Domain, Post, Site
from app.utils import is_video_hosting_site, url_is_parseable
from tests.factories import make_community, make_domain, make_instance, make_post, make_user

pytestmark = pytest.mark.usefixtures('site')

CRAFTED = 'https://youtube.com[abc'

PARSE_ERROR = 'could not be understood'

# Shapes that MUST keep passing. Each one is a real URL a person can legitimately
# submit, and each exercises a different part of the authority or the path that a
# too-strict check would trip over.
LEGITIMATE_URLS = [
    'https://example.com/article',                       # the ordinary case
    'https://example.com:8443/article',                  # explicit port
    'http://192.0.2.1:8080/probe',                       # IPv4 literal with a port
    'https://reader:hunter2@example.com/article',        # userinfo
    'https://user@example.com/article',                  # userinfo, no password
    'https://exämple.com/artikel',                  # IDN, unicode host
    'https://xn--exmple-cua.com/artikel',                # the same host, punycode
    'https://example.com/caf%C3%A9?q=a%20b#frag',        # percent-encoding, query, fragment
    'https://[2001:db8::1]:8443/article',                # well-formed IPv6 literal
    'https://example.com/a+b/c;d,e=f',                   # sub-delims in the path
]


def link_form():
    """A validated CreateLinkForm, reading link_url from the enclosing
    test_request_context's form data.

    Only link_url's own errors are ever asserted on: the other fields are left
    empty on purpose, so this exercises the validator under test rather than a
    fully-populated submission. g.site is set by hand because before_request,
    which normally populates it for CreatePostForm.validate_nsfw, does not run
    for a bare test_request_context.
    """
    g.site = db.session.query(Site).get(1)
    form = CreateLinkForm()
    form.communities.choices = []
    form.language_id.choices = []
    form.validate()
    return form


def video_form():
    """As link_form, for CreateVideoForm's video_url."""
    g.site = db.session.query(Site).get(1)
    form = CreateVideoForm()
    form.communities.choices = []
    form.language_id.choices = []
    form.validate()
    return form


def errors_on(field):
    return [str(e) for e in field.errors]


class TestTheCraftedUrlReallyDoesClearEveryGate:
    """Asserted, not assumed. If any of these stops holding, every rejection
    test below is testing something weaker than it claims to.
    """

    def test_urlparse_refuses_it(self):
        with pytest.raises(ValueError):
            urlparse(CRAFTED)

    def test_the_substring_gate_that_guards_the_render_path_passes_it(self):
        """`'youtube.com' in url` is the test Post.youtube_can_embed used to
        rely on. It says nothing about the authority."""
        assert 'youtube.com' in CRAFTED

    def test_the_regexp_validator_on_the_form_field_passes_it(self):
        """r'^https?://' is all link_url and video_url had."""
        assert re.match(r'^https?://', CRAFTED)

    def test_it_types_as_a_video_on_its_own(self):
        """So app/models.py:1982 and app/shared/post.py:654 route it to the
        video render path without any test setting post.type by hand."""
        assert is_video_hosting_site(CRAFTED) is True

    def test_url_is_parseable_says_no(self):
        assert url_is_parseable(CRAFTED) is False


class TestTheFormRefusesAnUnparseableUrl:
    """Test 1. Pre-fix the form ACCEPTS this, so pre-fix these fail by finding
    no error at all.
    """

    def test_a_crafted_link_url_is_rejected(self, app, db_session):
        with app.test_request_context('/', method='POST', data={'link_url': CRAFTED}):
            form = link_form()
        assert any(PARSE_ERROR in e for e in errors_on(form.link_url)), \
            f'expected a parse error on link_url, got {errors_on(form.link_url)}'

    def test_the_message_does_not_leak_a_python_exception(self, app, db_session):
        with app.test_request_context('/', method='POST', data={'link_url': CRAFTED}):
            form = link_form()
        joined = ' '.join(errors_on(form.link_url))
        # Assert the rejection happened first: without this the test would pass
        # vacuously against unfixed code, which produces no message at all.
        assert PARSE_ERROR in joined
        assert 'ValueError' not in joined and 'IPv6' not in joined and 'urlparse' not in joined

    def test_no_domain_row_is_created_for_it(self, app, db_session):
        """The validator uses create=False, and the refusal must not change
        that: a rejected URL leaves no Domain row behind to be banned later."""
        with app.test_request_context('/', method='POST', data={'link_url': CRAFTED}):
            link_form()
        assert db.session.query(Domain).count() == 0


# There is deliberately NO end-to-end test of POST /community/<name>/submit/link
# here. One was written and then removed, because it did not discriminate: under
# this harness the route stores no Post for ANY url -- an ordinary one included
# -- so `Post.query.count() == 0` passes whether the validator rejects the
# crafted url or not. Reaching a stored post through that route needs a Language
# row, a non-'/'-less timezone, an open registration_mode, a keyed local user
# (can_create_post refuses a local user with private_key None) and then survives
# only as far as make_post's own unrelated "expected string or bytes-like
# object, got 'NoneType'". All of that is fixture work for a test whose
# assertion would still be carried by the form-level tests above: add_post gates
# post creation on form.validate_on_submit(), so a form that reports invalid
# cannot reach make_post. Recorded rather than left for someone to rediscover.


class TestOrdinaryUrlsAreStillAccepted:
    """Test 2, the over-correction guard, and the one that matters most: a
    check that rejected everything would pass every other test in this file.
    """

    @pytest.mark.parametrize('url', LEGITIMATE_URLS)
    def test_a_legitimate_link_url_raises_no_parse_error(self, url, app, db_session):
        with app.test_request_context('/', method='POST', data={'link_url': url}):
            form = link_form()
        assert not any(PARSE_ERROR in e for e in errors_on(form.link_url)), \
            f'{url!r} was wrongly reported unparseable: {errors_on(form.link_url)}'

    @pytest.mark.parametrize('url', LEGITIMATE_URLS)
    def test_a_legitimate_video_url_raises_no_parse_error(self, url, app, db_session):
        with app.test_request_context('/', method='POST', data={'video_url': url}):
            form = video_form()
        assert not any(PARSE_ERROR in e for e in errors_on(form.video_url)), \
            f'{url!r} was wrongly reported unparseable: {errors_on(form.video_url)}'

    @pytest.mark.parametrize('url', LEGITIMATE_URLS)
    def test_url_is_parseable_accepts_it(self, url):
        assert url_is_parseable(url) is True

    def test_an_empty_field_is_not_reported_unparseable(self, app, db_session):
        """DataRequired/Regexp own the empty case; this check must not add a
        second, confusing error to a blank field."""
        with app.test_request_context('/', method='POST', data={'link_url': ''}):
            form = link_form()
        assert not any(PARSE_ERROR in e for e in errors_on(form.link_url))


class TestVideoUrlIsCheckedToo:
    """video_url had no validate_video_url hook; its domain-ban check lived
    inline in CreateVideoForm.validate. It is now the same check as link_url's.
    """

    def test_a_crafted_video_url_is_rejected(self, app, db_session):
        with app.test_request_context('/', method='POST', data={'video_url': CRAFTED}):
            form = video_form()
        assert any(PARSE_ERROR in e for e in errors_on(form.video_url)), \
            f'expected a parse error on video_url, got {errors_on(form.video_url)}'

    def test_the_form_as_a_whole_reports_invalid(self, app, db_session):
        with app.test_request_context('/', method='POST', data={'video_url': CRAFTED}):
            g.site = db.session.query(Site).get(1)
            form = CreateVideoForm()
            form.communities.choices = []
            form.language_id.choices = []
            assert form.validate() is False


class TestABannedDomainIsStillRejected:
    """Test 4. The validator's existing job has to survive the change -- an
    over-eager rewrite that returned early on the parse check would break it.
    """

    def test_a_banned_domain_link_is_still_refused(self, app, db_session):
        domain = make_domain('bannedingress.example')
        domain.banned = True
        db.session.commit()
        url = 'https://bannedingress.example/article'
        with app.test_request_context('/', method='POST', data={'link_url': url}):
            form = link_form()
        assert any('are not allowed' in e for e in errors_on(form.link_url)), \
            f'expected a banned-domain error, got {errors_on(form.link_url)}'

    def test_the_banned_domain_message_still_names_the_domain(self, app, db_session):
        domain = make_domain('bannedingress.example')
        domain.banned = True
        db.session.commit()
        url = 'https://bannedingress.example/article'
        with app.test_request_context('/', method='POST', data={'link_url': url}):
            form = link_form()
        assert any('bannedingress.example' in e for e in errors_on(form.link_url))

    def test_a_banned_domain_video_is_still_refused(self, app, db_session):
        domain = make_domain('bannedingress.example')
        domain.banned = True
        db.session.commit()
        url = 'https://bannedingress.example/watch'
        with app.test_request_context('/', method='POST', data={'video_url': url}):
            form = video_form()
        assert any('are not allowed' in e for e in errors_on(form.video_url)), \
            f'expected a banned-domain error, got {errors_on(form.video_url)}'

    def test_an_unbanned_domain_link_is_still_accepted(self, app, db_session):
        """The other direction: a Domain row that exists and is NOT banned must
        still pass, or the ban check would just be 'reject everything known'."""
        make_domain('okingress.example')
        url = 'https://okingress.example/article'
        with app.test_request_context('/', method='POST', data={'link_url': url}):
            form = link_form()
        assert errors_on(form.link_url) == []


@pytest.fixture
def federated_post(db_session):
    """A remote post with no url, ready to receive an Update."""
    make_instance('test.piefed.local', software='piefed')
    author = make_user(make_instance('ingress.example'), 'ingressauthor')
    community = make_community('ingressfed')
    post = make_post(community, author, 'https://ingress.example/notes/1', title='a post')
    assert post.url is None
    return post


class TestFederationStoresNoUrlRatherThanRaising:
    """Test 3. A peer sends an Update whose attachment url will not parse.

    Pre-fix this RAISES: since the urlparse guard landed, domain_from_url
    returns None for these instead of raising, and the very next line does
    `new_domain.banned` -- AttributeError: 'NoneType' object has no attribute
    'banned'. The activity dies and the peer's post is lost.
    """

    def test_an_unparseable_attachment_url_does_not_raise_and_stores_no_url(
            self, app, db_session, federated_post):
        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/1',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Note',
                'name': 'a post',
                'content': '<p>body</p>',
                'mediaType': 'text/html',
                'attachment': [{'type': 'Link', 'href': CRAFTED}],
            },
        })
        stored = db.session.query(Post).filter_by(ap_id='https://ingress.example/notes/1').one()
        assert stored.url is None

    def test_the_post_itself_survives_the_update(
            self, app, db_session, federated_post):
        """'rather than dropping the post' -- the body still updates."""
        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/2',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Note',
                'name': 'a retitled post',
                'content': '<p>the new body</p>',
                'mediaType': 'text/html',
                'attachment': [{'type': 'Link', 'href': CRAFTED}],
            },
        })
        stored = db.session.query(Post).filter_by(ap_id='https://ingress.example/notes/1').one()
        assert stored.title == 'a retitled post'
        assert 'the new body' in stored.body_html

    def test_an_ordinary_attachment_url_is_still_stored(
            self, app, db_session, http_mock, federated_post):
        """The federation-side over-correction guard.

        A url with an explicit port -- the shape a naive "authority must be a
        bare hostname" check would reject. The HEAD is is_image_url's; the
        path is unique to this test so mime_type_using_head's @cache.memoize
        key cannot collide with another test's and mask a reverted guard.
        """
        url = 'https://example.com:8443/ingress-attachment'
        http_mock.head(url).respond(200, headers={'Content-Type': 'text/html'})
        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/3',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Note',
                'name': 'a post',
                'content': '<p>body</p>',
                'mediaType': 'text/html',
                'attachment': [{'type': 'Link', 'href': url}],
            },
        })
        stored = db.session.query(Post).filter_by(ap_id='https://ingress.example/notes/1').one()
        assert stored.url == url


class TestFederationDoesNotStoreAnUnparseableMicroblogLink:
    """The other peer-supplied write, `post.url = link` (the microblog branch
    of update_post_from_activity).

    `link` is the href of an anchor in the peer's own content. The object type
    is 'Video' here for a reason and it is not decoration: the Links section
    further down overwrites post.url for every object type that reaches it, so
    the only way this write survives the function at all is one of the two
    early returns above it. That is worth recording -- for a plain Note the
    microblog write is immediately superseded, which is a separate finding.
    """

    def test_a_microblog_link_that_will_not_parse_is_not_stored(
            self, app, db_session, federated_post):
        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/4',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Video',
                'content': f'<h1><a href="{CRAFTED}">A crafted microblog headline</a></h1>',
                'mediaType': 'text/html',
            },
        })
        stored = db.session.query(Post).filter_by(ap_id='https://ingress.example/notes/1').one()
        assert stored.url is None

    def test_an_ordinary_microblog_link_is_still_stored(
            self, app, db_session, federated_post):
        """The over-correction guard for the same write."""
        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/5',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Video',
                'content': '<h1><a href="https://example.com/story">An ordinary headline</a></h1>',
                'mediaType': 'text/html',
            },
        })
        stored = db.session.query(Post).filter_by(ap_id='https://ingress.example/notes/1').one()
        assert stored.url == 'https://example.com/story'
