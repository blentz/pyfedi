"""Three modules too small to be rounds of their own.

MEASUREMENT BASIS, from the full-suite --cov=app run at ae2dfca0e:

    app/translation.py      20.930   34 gaps
    app/main/util.py        71.429   22 gaps
    app/markdown_extras.py  96.396    4 gaps

No production change. Bundled for the reason sub-project 69 bundled its three:
the alternative is three rounds of ceremony around a handful of assertions each.
"""
import pytest
from datetime import timedelta
from unittest.mock import patch

import httpx
from flask_login import login_user

from app import db
from app.main.util import (_base_list_communities_context, reload_url,
                           sidebar_active_communities, sidebar_new_communities,
                           sidebar_new_instances, sidebar_upcoming_events)
from app.models import BannedInstances, Community, Site, utcnow
from app.translation import LibreTranslateAPI
from tests.factories import (make_community, make_community_ban, make_community_block,
                             make_instance, make_instance_block, make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')


# --------------------------------------------------------------------------
# app/translation.py -- the LibreTranslate client
# --------------------------------------------------------------------------


@pytest.mark.parametrize('given, expected', [
    (None, 'https://translate.terraprint.co/'),
    ('https://lt.example', 'https://lt.example/'),
    ('https://lt.example/', 'https://lt.example/'),
    ('https://lt.example/sub', 'https://lt.example/sub/'),
])
def test_the_endpoint_always_ends_in_a_slash(given, expected):
    """Every method builds its URL as `self.url + "translate"`, so a base
    without the trailing slash would produce `...exampletranslate`. The `None`
    row is the documented default.
    """
    assert LibreTranslateAPI(given).url == expected


def test_an_empty_url_is_refused():
    """R1, pinned as the behaviour it is: the constructor uses a bare `assert`,
    which `python -O` strips -- and with it gone `self.url[-1]` raises
    IndexError on the same input instead. Registered rather than repaired,
    because what a misconfigured endpoint should do at startup is an
    operator-facing decision.
    """
    with pytest.raises(AssertionError):
        LibreTranslateAPI('')


def test_a_translation_returns_only_the_translated_text(app, http_mock):
    """The method reads one key out of the response and discards the rest, so
    the extra key here is what proves it is not returning the whole document.
    """
    route = http_mock.post('https://lt.example/translate').respond(
        200, json={'translatedText': 'Hola', 'alternatives': ['Buenas']})

    result = LibreTranslateAPI('https://lt.example').translate('Hello', 'en', 'es')

    assert result == 'Hola'
    assert route.calls.last.request.content == b'q=Hello&source=en&target=es'


def test_a_translation_uses_the_documented_default_languages(app, http_mock):
    """`source="en"`, `target="es"` are defaults on the signature, and a caller
    that omits them gets them on the wire.
    """
    route = http_mock.post('https://lt.example/translate').respond(
        200, json={'translatedText': 'Hola'})

    LibreTranslateAPI('https://lt.example').translate('Hello')

    assert b'source=en' in route.calls.last.request.content
    assert b'target=es' in route.calls.last.request.content


def test_a_detection_returns_the_whole_document(app, http_mock):
    """Unlike translate, detect returns the parsed body itself -- a list of
    candidates with confidences -- so the assertion is on the structure.
    """
    http_mock.post('https://lt.example/detect').respond(
        200, json=[{'confidence': 0.6, 'language': 'en'}])

    result = LibreTranslateAPI('https://lt.example').detect('Hello World')

    assert result == [{'confidence': 0.6, 'language': 'en'}]


def test_the_language_list_is_fetched_with_get_not_post(app, http_mock):
    """`languages()` is the only method of the three that GETs, and it sends
    its parameters in the query string rather than the body.
    """
    route = http_mock.get('https://lt.example/languages').respond(
        200, json=[{'code': 'en', 'name': 'English'}])

    result = LibreTranslateAPI('https://lt.example').languages()

    assert result == [{'code': 'en', 'name': 'English'}]
    assert route.calls.last.request.method == 'GET'


@pytest.mark.parametrize('method, verb, path, body', [
    ('translate', 'post', 'translate', {'translatedText': 'Hola'}),
    ('detect', 'post', 'detect', []),
    ('languages', 'get', 'languages', []),
])
def test_an_api_key_is_sent_when_one_is_configured(app, http_mock, method, verb, path,
                                                   body):
    """`if self.api_key:` on all three methods -- one row each, because the
    three build their parameters separately and a key added to one says nothing
    about the others.
    """
    route = getattr(http_mock, verb)(f'https://lt.example/{path}').respond(200, json=body)
    client = LibreTranslateAPI('https://lt.example', api_key='secret')

    getattr(client, method)('Hello') if method != 'languages' else client.languages()

    request = route.calls.last.request
    sent = request.content if verb == 'post' else str(request.url).encode()
    assert b'api_key=secret' in sent


@pytest.mark.parametrize('method, verb, path, body', [
    ('translate', 'post', 'translate', {'translatedText': 'Hola'}),
    ('detect', 'post', 'detect', []),
    ('languages', 'get', 'languages', []),
])
def test_no_api_key_is_sent_when_none_is_configured(app, http_mock, method, verb, path,
                                                    body):
    """The other arm of the same three guards. Without these rows a mutant
    making the key unconditional would send `api_key=None` to a public endpoint
    and nothing would notice.
    """
    route = getattr(http_mock, verb)(f'https://lt.example/{path}').respond(200, json=body)
    client = LibreTranslateAPI('https://lt.example')

    getattr(client, method)('Hello') if method != 'languages' else client.languages()

    request = route.calls.last.request
    sent = request.content if verb == 'post' else str(request.url).encode()
    assert b'api_key' not in sent


@pytest.mark.parametrize('method, verb, path', [
    ('translate', 'post', 'translate'),
    ('detect', 'post', 'detect'),
    ('languages', 'get', 'languages'),
])
def test_an_error_from_the_endpoint_is_raised_not_swallowed(app, http_mock, method,
                                                            verb, path):
    """`raise_for_status()` on all three. A translation service that answers
    500 must not be reported to the caller as an empty result -- the caller
    cannot tell that apart from "nothing to translate".
    """
    getattr(http_mock, verb)(f'https://lt.example/{path}').respond(503)
    client = LibreTranslateAPI('https://lt.example')

    with pytest.raises(httpx.HTTPStatusError):
        getattr(client, method)('Hello') if method != 'languages' else client.languages()


def test_the_request_timeout_is_passed_through(app, http_mock):
    """The `timeout` argument exists so a slow translation service cannot hold
    a request open; it is asserted at the transport rather than by timing.
    """
    http_mock.post('https://lt.example/translate').respond(200, json={'translatedText': 'x'})

    with patch('app.translation.httpx_client.post',
               wraps=__import__('app').translation.httpx_client.post) as post:
        LibreTranslateAPI('https://lt.example').translate('Hello', timeout=5)

    assert post.call_args.kwargs['timeout'] == 5


# --------------------------------------------------------------------------
# app/main/util.py -- the sidebar's community lists
# --------------------------------------------------------------------------


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    burn = make_user(instance, 'burnseat', local=True)
    assert burn.id == 1
    alice = make_user(instance, 'alice', local=True)
    db.session.commit()
    return instance, alice


def _with_site():
    """Put Site on `g` for the duration of a bare request context.

    `_base_list_communities_context` reads `g.site.community_creation_admin_only`,
    and `g.site` is set by a before_request hook that `test_request_context`
    does not run -- without this the helper raises AttributeError: site.
    """
    from flask import g

    g.site = db.session.get(Site, 1)
    return g.site


def _listable(name, **kwargs):
    """A community both sidebar functions will return.

    `show_all=True` is the one that is easy to miss: the queries filter on it,
    and make_community leaves it at the column default.
    """
    community = make_community(name)
    community.show_all = True
    community.last_active = utcnow()
    community.created_at = utcnow()
    for key, value in kwargs.items():
        setattr(community, key, value)
    db.session.commit()
    return community


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_an_anonymous_reader_sees_the_unfiltered_list(app, db_session, listing):
    """`user_id == 0` -- the fast path, which skips all three per-user queries.
    Both functions carry it, and both are exercised: they are the same code
    twice (R3).
    """
    _seed()
    _listable('microblogs')

    result = listing(0)

    assert [c.name for c in result] == ['microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_a_reader_banned_from_a_community_is_not_offered_it(app, db_session, listing):
    instance, alice = _seed()
    keep = _listable('microblogs')
    barred = _listable('barred')
    make_community_ban(alice, barred)
    db.session.commit()

    result = listing(alice.id)

    assert [c.name for c in result] == ['microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_a_community_the_reader_blocked_is_not_offered(app, db_session, listing):
    instance, alice = _seed()
    _listable('microblogs')
    blocked = _listable('blockedcomm')
    make_community_block(alice, blocked)
    db.session.commit()

    result = listing(alice.id)

    assert [c.name for c in result] == ['microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_a_community_on_a_blocked_instance_is_not_offered(app, db_session, listing):
    instance, alice = _seed()
    _listable('microblogs')
    peer = make_instance('peer.example', software='lemmy')
    elsewhere = _listable('elsewhere')
    elsewhere.instance_id = peer.id
    db.session.commit()
    make_instance_block(alice, peer)
    db.session.commit()

    result = listing(alice.id)

    assert [c.name for c in result] == ['microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_a_reader_who_has_blocked_nothing_sees_everything(app, db_session, listing):
    """The other arm of all three guards at once: with no bans and no blocks,
    none of the three narrowing filters is applied.
    """
    instance, alice = _seed()
    _listable('microblogs')
    _listable('another')

    result = listing(alice.id)

    assert sorted(c.name for c in result) == ['another', 'microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
@pytest.mark.parametrize('column', ['banned', 'nsfw', 'nsfl', 'private'])
def test_a_community_the_sidebar_never_shows(app, db_session, listing, column):
    """The four `filter_by` columns, one row each, with a listable community
    beside each so a filter that stopped working returns the other rather than
    nothing.
    """
    instance, alice = _seed()
    _listable('microblogs')
    _listable('hidden', **{column: True})

    result = listing(alice.id)

    assert [c.name for c in result] == ['microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_a_community_that_has_not_asked_to_be_listed_is_not(app, db_session, listing):
    """`show_all=True` is a filter, not a default -- a community with it unset
    is invisible to both lists.
    """
    instance, alice = _seed()
    _listable('microblogs')
    quiet = make_community('quiet')
    quiet.show_all = False
    quiet.last_active = utcnow()
    db.session.commit()

    result = listing(alice.id)

    assert [c.name for c in result] == ['microblogs']


@pytest.mark.parametrize('listing', [sidebar_active_communities, sidebar_new_communities])
def test_the_sidebar_stops_at_five(app, db_session, listing):
    instance, alice = _seed()
    for index in range(7):
        _listable(f'community{index}')

    assert len(listing(alice.id)) == 5


def test_only_recently_created_communities_are_new(app, db_session):
    """The 30-day cutoff, which is the only thing separating
    sidebar_new_communities from sidebar_active_communities besides the sort.
    """
    instance, alice = _seed()
    _listable('recent')
    old = _listable('ancient')
    old.created_at = utcnow() - timedelta(days=31)
    db.session.commit()

    assert [c.name for c in sidebar_new_communities(alice.id)] == ['recent']
    assert sorted(c.name for c in sidebar_active_communities(alice.id)) == ['ancient', 'recent']


def test_the_new_instance_list_shows_only_live_unbanned_piefed_peers(app, db_session):
    """Four conjuncts and an outer join, one row: each disqualifying instance
    is seeded alongside a qualifying one, so a dropped filter returns extras
    rather than nothing.
    """
    _seed()
    wanted = make_instance('wanted.example', software='piefed')
    make_instance('gone.example', software='piefed').gone_forever = True
    make_instance('dormant.example', software='piefed').dormant = True
    make_instance('lemmy.example', software='lemmy')
    banned = make_instance('banned.example', software='piefed')
    db.session.add(BannedInstances(domain='banned.example'))
    db.session.commit()

    result = sidebar_new_instances()
    domains = [i.domain for i in result]

    # test.piefed.local, seeded by _seed(), qualifies on every condition too --
    # it is a live unbanned piefed peer. The assertion names what must and must
    # not appear rather than the whole list, so it does not silently depend on
    # how many instances the seed happens to create.
    assert 'wanted.example' in domains
    for disqualified in ['gone.example', 'dormant.example', 'lemmy.example',
                         'banned.example']:
        assert disqualified not in domains


def test_upcoming_events_are_future_undeleted_and_published(app, db_session):
    """The raw SQL's four conditions. The event rows are built directly: there
    is no factory for them, and the query joins on post_id.
    """
    from sqlalchemy import text

    instance, alice = _seed()
    community = _listable('microblogs')
    soon = make_post(community, alice, ap_id='https://test.piefed.local/post/soon',
                     title='soon')
    past = make_post(community, alice, ap_id='https://test.piefed.local/post/past',
                     title='past')
    deleted = make_post(community, alice, ap_id='https://test.piefed.local/post/deleted',
                        title='deleted')
    deleted.deleted = True
    unreviewed = make_post(community, alice,
                           ap_id='https://test.piefed.local/post/unreviewed',
                           title='unreviewed')
    unreviewed.status = 0
    db.session.commit()
    for post, start in [(soon, utcnow() + timedelta(days=1)),
                        (past, utcnow() - timedelta(days=1)),
                        (deleted, utcnow() + timedelta(days=1)),
                        (unreviewed, utcnow() + timedelta(days=1))]:
        db.session.execute(text('INSERT INTO "event" (post_id, start) VALUES (:p, :s)'),
                           {'p': post.id, 's': start})
    db.session.commit()

    result = sidebar_upcoming_events()

    assert [row[1] for row in result] == ['soon']


# --------------------------------------------------------------------------
# app/main/util.py -- the context helpers
# --------------------------------------------------------------------------


@pytest.mark.parametrize('role, is_admin, is_staff', [
    ('admin', True, False),
    ('staff', False, True),
    ('ordinary', False, False),
])
def test_the_community_list_context_names_the_readers_powers(app, db_session, role,
                                                             is_admin, is_staff):
    """`current_user.is_authenticated and current_user.is_X()` twice. The
    template hangs the "create a community" control on these, and an anonymous
    reader is the row that makes the first conjunct load-bearing --
    is_admin() does not exist on an anonymous user.
    """
    from app.models import Role

    instance, alice = _seed()
    if role in ('admin', 'staff'):
        name = 'Admin' if role == 'admin' else 'Staff'
        existing = Role.query.filter_by(name=name).first()
        if existing is None:
            existing = Role(name=name, weight=10)
            db.session.add(existing)
            db.session.commit()
        alice.roles.append(existing)
        db.session.commit()

    with app.test_request_context('/'):
        _with_site()
        login_user(alice)
        context = _base_list_communities_context()

    assert context['is_admin'] is is_admin
    assert context['is_staff'] is is_staff


def test_the_community_list_context_for_an_anonymous_reader(app, db_session):
    """The `current_user.is_authenticated` conjunct from the other side, and
    the joined/pending lists built from `get_id()` being None.
    """
    _seed()
    with app.test_request_context('/'):
        _with_site()
        context = _base_list_communities_context()

    assert context['is_admin'] is False
    assert context['is_staff'] is False
    assert context['joined_communities'] == []
    assert context['pending_communities'] == []


def test_the_community_list_context_carries_the_sites_creation_policy(app, db_session):
    _seed()
    site = db.session.get(Site, 1)
    site.community_creation_admin_only = True
    db.session.commit()

    with app.test_request_context('/'):
        _with_site()
        context = _base_list_communities_context()

    assert context['create_admin_only'] is True
    assert context['default_user_add_remote'] is True


@pytest.mark.parametrize('authenticated, sort, expected', [
    (True, 'new', '/home/new/subscribed?fragment=1'),
    (True, 'top', '/home/top/subscribed?fragment=1'),
    (True, 'top_12h', '/home/top_12h/subscribed?fragment=1'),
    (True, 'hot', ''),
    (True, 'active', ''),
    (False, 'new', ''),
])
def test_the_reload_url_is_offered_only_for_the_sorts_that_need_it(app, db_session,
                                                                   authenticated, sort,
                                                                   expected):
    """Two guards. The `hot` row is the one that was missing: an authenticated
    reader on a sort that is neither `new` nor a `top*` gets the empty string,
    and without it a mutant dropping the inner condition would survive.
    """
    instance, alice = _seed()
    with app.test_request_context('/'):
        if authenticated:
            login_user(alice)

        assert reload_url(sort, 'subscribed') == expected


# --------------------------------------------------------------------------
# app/markdown_extras.py -- the residual
# --------------------------------------------------------------------------


def _markdown_with_enhanced_images(text):
    """The real pipeline: markdown2 with the extra, then the post-processor.

    Copied in shape from tests/test_enhanced_images.py, which is where the rest
    of this module's coverage comes from.

    NOTE the ` :: ` marker every caller here uses. The extra only engages for
    an image whose alt text carries it; without it markdown2 renders an
    ordinary <img> and none of the branches below are reached, so a row written
    without it passes while testing nothing.
    """
    import markdown2
    from app.markdown_extras import apply_enhanced_image_attributes

    md = markdown2.Markdown(extras={'enhanced-images': True})
    return apply_enhanced_image_attributes(md.convert(text), md)


def test_an_image_marker_with_no_usable_id_is_left_alone():
    """`:158-159`, `add_attrs`'s early return.

    The OUTER pattern matches any `data-enhanced-img=`; the INNER one requires
    `="<at least one character>"`. A tag carrying the marker with an empty
    value therefore matches the first and fails the second, and the callback
    returns the tag untouched rather than rewriting it from an empty attribute
    store.

    Driven through the real `apply_enhanced_image_attributes` rather than a
    local copy of the callback: an earlier version of this row reimplemented
    `add_attrs` in the test and asserted against its own copy, which left the
    module at 96.396% and proved nothing about the code.
    """
    from app.markdown_extras import apply_enhanced_image_attributes

    md = type('FakeMd', (), {'_enhanced_images_attrs': {}})()
    html = '<img data-enhanced-img="" src="https://example.com/a.png"/>'

    assert apply_enhanced_image_attributes(html, md) == html


def test_html_is_returned_untouched_when_nothing_was_enhanced():
    """`:146-147`. `apply_enhanced_image_attributes` is called on every
    rendered body, most of which contain no enhanced image at all, so the
    markdown instance never grew the attribute store.
    """
    from app.markdown_extras import apply_enhanced_image_attributes

    html = '<p>no images here</p>'

    assert apply_enhanced_image_attributes(html, object()) == html


def test_a_titled_image_with_one_url_keeps_its_title_and_gains_no_anchor():
    """Arc `[71, 84]`: a comma inside the TITLE, with only one URL.

    `if ',' in url_and_title` is true because the title contains one, the title
    is lifted out, and `if ',' in url_part` is then false -- so no second URL
    is found and the image is not wrapped in an anchor. Without this row the
    inner guard is never seen false.
    """
    result = _markdown_with_enhanced_images(
        '![A cat :: width=200px](cat.jpg "Tabby, sleeping")')

    assert 'src="cat.jpg"' in result
    assert '<a href=' not in result


def test_a_trailing_comma_makes_an_empty_full_size_url():
    """What the removed `len(urls) == 2` guard was standing in front of.

    That guard could never be false. This branch runs only inside
    `if ',' in url_and_title`, and `split(',', 1)` on a string containing a
    comma returns exactly two parts every time -- measured:

        'cat.jpg,' -> ['cat.jpg', '']      ','    -> ['', '']
        'a,b'      -> ['a', 'b']           'a,b,c'-> ['a', 'b,c']

    so coverage reported its false arm as an unreachable arc. Removed rather
    than pragma'd, following D822. A trailing comma therefore yields an EMPTY
    second url, which is falsy, so no anchor is written -- the same outcome the
    guard produced, reached without a branch that cannot be taken.
    """
    result = _markdown_with_enhanced_images('![A cat :: width=200px](cat.jpg,)')

    assert 'src="cat.jpg"' in result
    assert '<a href=' not in result


def test_the_comma_split_always_yields_two_parts():
    """The proof behind the row above, asserted rather than argued, so a future
    reader does not have to re-derive why the guard was safe to remove.
    """
    for value in ['cat.jpg,', ',', 'a,b', 'a,b,c', ',,']:
        assert len(value.split(',', 1)) == 2


def test_a_thumbnail_with_a_full_size_url_is_wrapped_in_an_anchor():
    """The other arm of both guards above, so the two rows are not passing for
    want of the feature working at all.
    """
    result = _markdown_with_enhanced_images(
        '![A cat :: width=200px](thumb.jpg, full.jpg)')

    assert '<a href="full.jpg">' in result
    assert 'src="thumb.jpg"' in result
