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

import httpx

import pytest
from flask import g, request
from werkzeug.datastructures import MultiDict

from app import db
from app.activitypub.util import create_post, update_post_from_activity
from app.community.forms import CreateLinkForm, CreateVideoForm
from app.constants import POST_TYPE_LINK
from app.models import Domain, File, Post, Site
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


# The CreatePostForm fields a submission must carry for super().validate() to
# pass. No database rows are needed for them: SelectField.pre_validate only
# compares the submitted value against `choices`, and CreateVideoForm never
# looks the community or the language up.
BASE_SUBMISSION = {
    'communities': '1',
    'title': 'An entirely ordinary post title',
    'language_id': '1',
    # get_timezones() drops every zone without a '/', so 'UTC' is not a choice.
    'timezone': 'Europe/London',
    'repeat': 'none',
}


def video_form():
    """As link_form, for CreateVideoForm's video_url -- but built on a
    COMPLETE submission, which link_form does not have to be.

    The difference is where the two checks live. link_url's is an inline
    `validate_link_url` hook, so WTForms runs it as part of every field's
    validator chain whether or not other fields failed. video_url's lives in
    `CreateVideoForm.validate`, AFTER `if not super().validate(...): return
    False` -- so on a half-empty submission the guard returns first and
    video_url is never examined. Before that guard existed the override
    discarded super()'s verdict and reached the check regardless, which is why
    this helper used to get away with sending video_url alone.

    `request.form` is immutable and was fixed when the caller opened its
    request context, so the base fields are merged in as explicit formdata
    instead; `request.files` is untouched.
    """
    g.site = db.session.query(Site).get(1)
    form = CreateVideoForm(formdata=MultiDict({**BASE_SUBMISSION, **request.form.to_dict()}))
    form.communities.choices = [(1, 'a community')]
    form.language_id.choices = [(1, 'English')]
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
            form = CreateVideoForm(
                formdata=MultiDict({**BASE_SUBMISSION, 'video_url': CRAFTED}))
            form.communities.choices = [(1, 'a community')]
            form.language_id.choices = [(1, 'English')]
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
    early returns above it.

    **That "separate finding" was chased down and is closed: the write is NOT
    dead, and must not be removed.** Two things keep it:

    - Two object types reach it and keep the value outright, by returning
      before the Links section -- 'Video' (`app/activitypub/util.py`, the
      PeerTube early return, which commits first) and 'Question' (the Poll
      early returns, four of them). 'Video' is the one these two tests drive.
    - For every other type the value is READ before it is overwritten:
      `old_url = post.url` at the top of the Links section is this write's
      output. It decides whether the `old_url != new_url` arm runs at all --
      and that arm deletes the post's image, retypes the post, recalculates
      cross posts and drives the banned-domain notification. Store a different
      value here and a plain Note takes a different branch there. A write whose
      result is consumed six lines later is an intermediate value, not dead
      code.

    Concretely, for a nameless Note (the only shape that reaches the microblog
    branch) whose anchor href will not parse, sent to a post that already has a
    url and an image: this write stores None, so `old_url` is None, `new_url` is
    None, and the arm is SKIPPED -- the post keeps its type and its image.
    Delete the write and `old_url` is the post's previous url instead, the arm
    fires, and the post is retyped to POST_TYPE_ARTICLE with its image dropped.
    Same activity, opposite outcome.
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

    def test_the_write_is_read_by_the_links_section_before_it_is_overwritten(
            self, app, db_session, federated_post):
        """The write is not dead for the types that DON'T return early either.

        A nameless Note (the only shape that reaches the microblog branch) whose
        anchor href will not parse, sent to a post that already has a url and an
        image. This write stores None, so `old_url` is None, `new_url` is None
        (no attachment), the `old_url != new_url` arm is skipped, and the post
        keeps its type and its image.

        Neutralize the write and `old_url` is the post's previous url instead:
        the arm fires, the post is retyped to POST_TYPE_ARTICLE, image_id is
        cleared and the File row is deleted at the end of the function. Verified
        both ways by neutralizing the line, which is why this asserts type and
        image alongside url -- `url is None` alone holds in both directions and
        would not discriminate.
        """
        image = File(source_url='https://ingress.example/pic.png', file_name='pic.png')
        db.session.add(image)
        db.session.commit()
        federated_post.url = 'https://ingress.example/previous'
        federated_post.type = POST_TYPE_LINK
        federated_post.image_id = image.id
        db.session.commit()

        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/6',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Note',
                'content': f'<h1><a href="{CRAFTED}">A crafted microblog headline</a></h1>',
                'mediaType': 'text/html',
            },
        })
        stored = db.session.query(Post).filter_by(ap_id='https://ingress.example/notes/1').one()
        assert stored.url is None
        assert stored.type == POST_TYPE_LINK
        assert stored.image_id == image.id
        assert db.session.query(File).filter_by(id=image.id).count() == 1


# ---------------------------------------------------------------------------
# Round 2: the twin ingress path, the None-dereference sweep, and events.
# ---------------------------------------------------------------------------

HOSTLESS = 'https:///x'

# The keys Post.new()'s Event branch (app/models.py) reads unconditionally.
EVENT_KEYS = {
    'startTime': '2030-01-01T10:00:00',
    'endTime': '2030-01-01T12:00:00',
    'timezone': 'Europe/London',
    'maximumAttendeeCapacity': 50,
    'participantCount': 0,
    'onlineLink': '',
    'joinMode': 'free',
    'externalParticipationUrl': '',
    'anonymousParticipation': False,
    'isOnline': False,
    'buyTicketsLink': '',
    'feeCurrency': 'GBP',
    'feeAmount': 0,
    'location': {'type': 'Place', 'name': 'somewhere'},
}


@pytest.fixture
def federated_create(db_session):
    """An author and a community for a federated Create.

    The Site row comes from this module's `site` usefixtures mark;
    Post.new() -> blocked_phrases() needs it.
    """
    author = make_user(make_instance('create.example'), 'createauthor')
    community = make_community('createcomm')
    return author, community


def create_activity(obj_extra, seq=1):
    """A Create wrapping a public, titled Note. Titled on purpose: the
    microblog branch is covered separately, and a title keeps these tests on
    the attachment path.
    """
    return {
        'id': f'https://create.example/users/a/statuses/{seq}/activity',
        'type': 'Create',
        'to': ['https://www.w3.org/ns/activitystreams#Public'],
        'object': {
            'id': f'https://create.example/users/a/statuses/{seq}',
            'type': 'Note',
            'name': 'An entirely ordinary title',
            'content': '<p>hello world, at some length so a title can be derived</p>',
            'attributedTo': 'https://create.example/users/a',
            'to': ['https://www.w3.org/ns/activitystreams#Public'],
            **obj_extra,
        },
    }


class TestTheFederatedCreatePathRefusesAnUnparseableUrl:
    """`Post.new()` is the Create twin of `update_post_from_activity`, and it
    was left unguarded when the Update path was fixed.

    It writes a peer-supplied url from seven places (`app/models.py:1907,
    :1931, :1933, :1937, :1943, :1952, :1959`) and then does
    `domain = domain_from_url(post.url)` followed immediately by
    `domain.notify_mods`. Since the urlparse guard landed, `domain_from_url`
    returns None for an unparseable url rather than raising, so pre-fix this
    is `AttributeError: 'NoneType' object has no attribute 'notify_mods'` on
    peer-controlled input -- and the peer's whole post is lost.
    """

    def test_an_unparseable_attachment_href_does_not_raise(self, app, db_session, federated_create):
        author, community = federated_create
        post = create_post(False, community,
                           create_activity({'attachment': [{'type': 'Link', 'href': CRAFTED}]}),
                           author)
        assert post is not None

    def test_the_post_is_stored_with_no_url(self, app, db_session, federated_create):
        author, community = federated_create
        create_post(False, community,
                    create_activity({'attachment': [{'type': 'Link', 'href': CRAFTED}]}, seq=2),
                    author)
        stored = db.session.query(Post).filter_by(
            ap_id='https://create.example/users/a/statuses/2').one()
        assert stored.url is None
        assert stored.title == 'An entirely ordinary title'

    def test_an_ordinary_attachment_href_is_still_stored(self, app, db_session, http_mock,
                                                         federated_create):
        """The Create-side over-correction guard. The HEAD is is_image_url's;
        the path is unique to this test so mime_type_using_head's
        @cache.memoize key cannot collide with another test's.
        """
        author, community = federated_create
        url = 'https://example.com:8443/create-attachment'
        http_mock.head(url).respond(200, headers={'Content-Type': 'text/html'})
        create_post(False, community,
                    create_activity({'attachment': [{'type': 'Link', 'href': url}]}, seq=3),
                    author)
        stored = db.session.query(Post).filter_by(
            ap_id='https://create.example/users/a/statuses/3').one()
        assert stored.url == url


class TestADomainThatCannotBeResolvedIsNotDereferenced:
    """The second half of the same shape, found by sweeping every
    `domain_from_url(...)` call site rather than patching the two that were
    reported.

    `domain_from_url` returns None for a url that PARSES but has no hostname
    ('https:///x' -- `urlparse` accepts it, `.hostname` is None), so a
    parseability check alone does not stop the dereference. Three sites in
    `app/shared/post.py` (`:192`, `:443`, `:566`) already guard with
    `if domain:`; these two did not.
    """

    def test_a_hostless_attachment_href_does_not_crash_the_create_path(
            self, app, db_session, http_mock, federated_create):
        """httpx does build a request for a hostless url -- respx sees
        `HEAD /x`, no host -- so is_image_url's probe has to be answered. It is
        answered with a CONNECT ERROR, which is what production gets when it
        tries to connect to no host; mime_type_using_head catches
        httpx.HTTPError and returns '', is_image_url is False, and the call
        reaches app/models.py's dereference the same way production does.
        Answering with a 200 instead would be answering with something
        production cannot produce, and httpx's cookie handling then raises
        ValueError('unknown url type') on the hostless request URL.
        """
        author, community = federated_create
        http_mock.route(method='HEAD', path='/x').mock(side_effect=httpx.ConnectError)
        post = create_post(False, community,
                           create_activity({'attachment': [{'type': 'Link', 'href': HOSTLESS}]},
                                           seq=4),
                           author)
        assert post is not None

    def test_a_hostless_attachment_href_does_not_crash_the_update_path(
            self, app, db_session, http_mock, federated_post):
        # A connect error, not a 200: see the sibling test above for why.
        http_mock.route(method='HEAD', path='/x').mock(side_effect=httpx.ConnectError)
        update_post_from_activity(federated_post, {
            'id': 'https://ingress.example/activities/update/9',
            'object': {
                'id': federated_post.ap_id,
                'type': 'Note',
                'name': 'a post',
                'content': '<p>body</p>',
                'mediaType': 'text/html',
                'attachment': [{'type': 'Link', 'href': HOSTLESS}],
            },
        })
        stored = db.session.query(Post).filter_by(
            ap_id='https://ingress.example/notes/1').one()
        assert stored.title == 'a post'


class TestFederatedEventUrlsAreCheckedToo:
    """`Post.new()`'s Event branch (`app/models.py:2182`) assigns
    `post.url = attachment_item['href']` AFTER the domain block above it, so
    nothing validated it at all.

    That is the interaction the two layers were supposed to close: a url
    `CreateEventForm` refuses on submission still reached a template through
    its federated copy, and only the render guards from 43138ac7 kept that
    from being a 500.
    """

    def test_an_unparseable_event_link_is_not_stored(self, app, db_session, federated_create):
        author, community = federated_create
        create_post(False, community,
                    create_activity({'type': 'Event',
                                     'attachment': [{'type': 'Link', 'href': CRAFTED}],
                                     **EVENT_KEYS}, seq=5),
                    author)
        stored = db.session.query(Post).filter_by(
            ap_id='https://create.example/users/a/statuses/5').one()
        # None, not '': the Event branch's "no url" value was aligned to the
        # column's own, because post_to_page gates the outbound attachment on
        # `post.url is not None` and '' federated `{"href": ""}` to peers.
        # tests/test_federated_event_url_sentinel.py carries that reasoning.
        assert stored.url is None

    def test_an_ordinary_event_link_is_still_stored(self, app, db_session, http_mock,
                                                    federated_create):
        author, community = federated_create
        url = 'https://example.com:8443/event-link'
        http_mock.head(url).respond(200, headers={'Content-Type': 'text/html'})
        create_post(False, community,
                    create_activity({'type': 'Event',
                                     'attachment': [{'type': 'Link', 'href': url}],
                                     **EVENT_KEYS}, seq=6),
                    author)
        stored = db.session.query(Post).filter_by(
            ap_id='https://create.example/users/a/statuses/6').one()
        assert stored.url == url


class TestUrlIsParseableChecksTheLoweredFormToo:
    """`domain_from_url` parses `url.lower()`, not the raw string. A string
    that parsed raw but failed lowered would be accepted here and then get no
    ban check at all, because `domain_from_url` would return None for it --
    a silently skipped policy check rather than a crash.

    `url_is_parseable` now checks both. The sweep below is what says that
    costs nothing: over every case-changing codepoint in Python's Unicode
    tables, in three netloc positions, the raw and lowered verdicts agree --
    so the extra check rejects nothing the raw check accepted. If a future
    CPython changes that, this fails and names the codepoint instead of
    letting an over-correction ship silently.
    """

    @pytest.mark.parametrize('url', LEGITIMATE_URLS)
    def test_no_legitimate_url_is_lost_to_the_extra_check(self, url):
        assert url_is_parseable(url) is True
        assert url_is_parseable(url.upper()) is True

    def test_raw_and_lowered_agree_for_every_case_changing_codepoint(self):
        templates = ('https://a{0}b.example/x', 'https://{0}.example/x', 'https://ex.{0}/x')
        divergent = []
        checked = 0
        for cp in range(0x110000):
            ch = chr(cp)
            if ch.lower() == ch:
                continue
            for template in templates:
                candidate = template.format(ch)
                checked += 1
                if _parses(candidate) != _parses(candidate.lower()):
                    divergent.append((hex(cp), template))
        assert checked > 4000, f'the sweep degenerated: only {checked} shapes checked'
        assert divergent == [], f'raw/lowered divergence found: {divergent[:10]}'


def _parses(url):
    try:
        urlparse(url)
    except ValueError:
        return False
    return True
