"""The community's syndication surfaces: RSS out, iCal out, RSS in.

Sub-project 80, slice G. Five defects, all measured:

* a moderator of ANY community could rewrite -- or delete -- another
  community's incoming RSS feed, because `feed_id` and `community_id` both
  came from the URL and nothing tied them together (D1010). The feed's url is
  the input to the background fetcher that creates posts in that other
  community, so this was a cross-community content-ingest takeover, and the
  delete arm removes every post the feed created;
* an unknown `feed_id` was an `AttributeError`, not a 404 (D1011);
* `community_rss_feeds` fell off the end of the function for a non-moderator
  and for a name that does not resolve, which Flask reports as a 500 rather
  than as the refusal it meant (D1012);
* the RSS feed answered `304 Not Modified` for a PRIVATE community, because
  the conditional-request check ran before the access check (D1013);
* one event post with no `Event` row failed the whole iCal calendar (D1014).

`/community` is the blueprint's url_prefix, so `@bp.route('/community/<id>/
feed/<id>')` is served at `/community/community/<id>/feed/<id>`. That doubling
is real and the rows use the real paths.
"""
from datetime import timedelta
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_STATUS_REVIEWING, POST_TYPE_EVENT
from app.models import (Community, CommunityFlair, Event, Instance, Language,
                        Post, RssFeed, Site)
from app.utils import utcnow
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


@pytest.fixture
def env(app, db_session):
    """Two communities. The viewer moderates the first and has no standing in
    the second -- which is the whole point of D1010."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True)
    mod.verified = True
    mod.private_key = 'a private key'
    mine = make_community('mine')
    theirs = make_community('theirs')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    membership = make_community_member(mod, mine)
    membership.is_moderator = True
    db.session.commit()
    app.config['RSS_FEEDS'] = True
    client = app.test_client()
    login(client, mod)
    return client, mine, theirs, mod


def a_feed(community, title='a feed', url='https://example.com/feed.xml'):
    feed = RssFeed(title=title, url=url, community_id=community.id,
                   check_frequency=60)
    db.session.add(feed)
    db.session.commit()
    return feed


def a_post(community, author, number=1, **columns):
    post = make_post(community, author,
                     f'https://test.piefed.local/p/{number}',
                     title=f'post {number}')
    for column, value in columns.items():
        setattr(post, column, value)
    db.session.commit()
    return post


def feed_payload(token, **overrides):
    data = {'name': 'A feed', 'url': 'https://example.com/feed.xml',
            'flair': '-1', 'check_frequency': '60', 'submit': 'Save',
            'csrf_token': token}
    data.update(overrides)
    return data


# --------------------------------------------------------------------------
# RSS out
# --------------------------------------------------------------------------


def test_the_rss_feed_lists_the_communitys_posts(app, env):
    client, mine, theirs, mod = env
    a_post(mine, mod, 1)

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/rss+xml'
    assert b'post 1' in response.data


def test_a_remote_community_is_found_by_its_handle(app, env):
    """`if '@' in actor:` picks the `ap_id` lookup; without it a remote
    community's feed is a 404."""
    client, mine, theirs, mod = env
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    db.session.commit()
    a_post(remote, mod, 5)

    response = app.test_client().get('/community/remote@other.example/feed')

    assert response.status_code == 200


def test_an_unknown_community_has_no_feed(app, env):
    response = app.test_client().get('/community/nonexistent/feed')

    assert response.status_code == 404


def test_a_banned_communitys_feed_is_not_served(app, env):
    """`banned=False` is part of both lookups, so a banned community is a 404
    rather than a refusal."""
    client, mine, theirs, mod = env
    mine.banned = True
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert response.status_code == 404


def test_a_private_communitys_feed_is_refused(app, env):
    client, mine, theirs, mod = env
    mine.private = True
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert response.status_code == 403


def test_a_private_community_is_refused_even_with_a_matching_etag(app, env):
    """D1013. The 304 used to be answered BEFORE the access check, so a client
    holding an ETag from before the community was made private got `304 Not
    Modified` where a fresh request got 403. Measured:
    `PROBE r5 private feed with matching etag: 304`.

    The ETag is `{id}_{hash(last_active)}`, so the 304 also confirmed the
    community's current `last_active` to anyone who could guess it."""
    client, mine, theirs, mod = env
    a_post(mine, mod, 1)
    anonymous = app.test_client()
    first = anonymous.get(f'/community/{mine.name}/feed')
    etag = first.headers['ETag']

    mine.private = True
    db.session.commit()

    response = anonymous.get(f'/community/{mine.name}/feed',
                             headers={'If-None-Match': etag})

    assert response.status_code == 403


def test_an_unchanged_feed_is_a_304(app, env):
    """The conditional request still works for a community that allows it."""
    client, mine, theirs, mod = env
    a_post(mine, mod, 1)
    anonymous = app.test_client()
    first = anonymous.get(f'/community/{mine.name}/feed')

    response = anonymous.get(f'/community/{mine.name}/feed',
                             headers={'If-None-Match': first.headers['ETag']})

    assert response.status_code == 304


@pytest.mark.parametrize('columns', [
    {'deleted': True},
    {'status': POST_STATUS_REVIEWING},
    {'from_bot': True},
])
def test_the_feed_omits_what_the_page_omits(app, env, columns):
    """The three filters the RSS query carries, one row each."""
    client, mine, theirs, mod = env
    a_post(mine, mod, 1)
    a_post(mine, mod, 2, **columns)

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert b'post 1' in response.data
    assert b'post 2' not in response.data


def test_a_score_filter_narrows_the_feed(app, env):
    """`?score=` is the only query parameter the feed takes, and it is what
    `@cache.cached(query_string=True)` exists for."""
    client, mine, theirs, mod = env
    a_post(mine, mod, 1, score=10)
    a_post(mine, mod, 2, score=1)

    response = app.test_client().get(f'/community/{mine.name}/feed?score=5')

    assert b'post 1' in response.data
    assert b'post 2' not in response.data


def test_the_feed_carries_the_communitys_icon_and_description(app, env):
    from app.models import File

    client, mine, theirs, mod = env
    a_post(mine, mod, 1)
    icon = File(source_url='https://test.piefed.local/icon.png')
    db.session.add(icon)
    db.session.commit()
    mine.image_id = icon.id
    mine.description = 'What this community is for'
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert b'https://test.piefed.local/icon.png' in response.data
    assert b'What this community is for' in response.data


def test_a_community_without_an_icon_or_description_still_has_a_feed(app, env):
    """Both `else:` arms. feedgen raises "Required fields not set" for an
    empty subtitle, which is why the fallback is a space rather than ''."""
    client, mine, theirs, mod = env
    a_post(mine, mod, 1)
    mine.description = None
    mine.image_id = None
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert response.status_code == 200
    assert b'apple-touch-icon.png' in response.data


def test_a_post_with_a_media_url_carries_media_content(app, env):
    """RSSFeed attaches a post's url as media:content, typed by
    `mimetype_from_url` when it can be."""
    client, mine, theirs, mod = env
    a_post(mine, mod, 1, url='https://example.com/audio.mp3')

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert b'audio.mp3' in response.data
    assert b'<media:content' in response.data
    assert b'type="audio/mpeg"' in response.data


def test_a_post_with_a_page_url_is_media_content_typed_as_html(app, env):
    client, mine, theirs, mod = env
    a_post(mine, mod, 1, url='https://example.com/article.html')

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert b'<enclosure' not in response.data
    assert b'type="text/html"' in response.data


def test_a_post_with_a_slug_is_linked_by_it(app, env):
    """`if post.slug:` -- the factory leaves `slug` None, which is the fallback
    arm, so the ordinary case needs a row of its own."""
    client, mine, theirs, mod = env
    post = a_post(mine, mod, 1)
    post.slug = '/post/1/a-nice-title'
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert b'/post/1/a-nice-title' in response.data


def test_a_post_without_a_slug_falls_back_to_its_id(app, env):
    client, mine, theirs, mod = env
    post = a_post(mine, mod, 1)
    post.slug = None
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/feed')

    assert f'/post/{post.id}'.encode() in response.data


# --------------------------------------------------------------------------
# iCal out
# --------------------------------------------------------------------------


def an_event(community, author, number, start=None, end=None):
    post = a_post(community, author, number, type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=post.id,
                         start=start or utcnow() + timedelta(days=1),
                         end=end or utcnow() + timedelta(days=2)))
    db.session.commit()
    return post


def test_the_ical_feed_lists_the_communitys_events(app, env):
    client, mine, theirs, mod = env
    an_event(mine, mod, 1)

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert response.status_code == 200
    assert response.mimetype == 'text/calendar'
    assert b'BEGIN:VEVENT' in response.data
    assert b'post 1' in response.data


def test_the_ical_feed_names_the_community(app, env):
    """`X-WR-CALNAME` is spliced into the serialized calendar by hand, and the
    filename comes from the same name."""
    client, mine, theirs, mod = env
    an_event(mine, mod, 1)

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert b'X-WR-CALNAME:mine' in response.data
    assert 'filename="Events in mine.ics"' in response.headers['Content-Disposition']


def test_the_ical_feed_omits_ordinary_posts(app, env):
    client, mine, theirs, mod = env
    a_post(mine, mod, 1)
    an_event(mine, mod, 2)

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert b'post 2' in response.data
    assert b'post 1' not in response.data


@pytest.mark.parametrize('columns', [
    {'deleted': True},
    {'status': POST_STATUS_REVIEWING},
    {'from_bot': True},
])
def test_the_ical_feed_omits_what_the_page_omits(app, env, columns):
    client, mine, theirs, mod = env
    an_event(mine, mod, 1)
    hidden = an_event(mine, mod, 2)
    for column, value in columns.items():
        setattr(hidden, column, value)
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert b'post 1' in response.data
    assert b'post 2' not in response.data


def test_an_event_post_with_no_event_row_is_skipped(app, env):
    """D1014. `post.event` is a relationship, and a POST_TYPE_EVENT post whose
    Event row is missing made `post.event.start` an AttributeError -- which
    failed the WHOLE calendar, not that one entry. Measured:
    `PROBE r6 RAISED: AttributeError 'NoneType' object has no attribute
    'start'`."""
    client, mine, theirs, mod = env
    an_event(mine, mod, 1)
    a_post(mine, mod, 2, type=POST_TYPE_EVENT)

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert response.status_code == 200
    assert b'post 1' in response.data
    assert b'post 2' not in response.data


def test_a_private_communitys_calendar_is_refused(app, env):
    client, mine, theirs, mod = env
    mine.private = True
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert response.status_code == 403


def test_an_unknown_community_has_no_calendar(app, env):
    response = app.test_client().get('/community/nonexistent/ical')

    assert response.status_code == 404


def test_a_remote_communitys_calendar_is_found_by_handle(app, env):
    client, mine, theirs, mod = env
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    db.session.commit()
    an_event(remote, mod, 5)

    response = app.test_client().get('/community/remote@other.example/ical')

    assert response.status_code == 200


# --------------------------------------------------------------------------
# RSS in -- the feeds a community imports from
# --------------------------------------------------------------------------


def test_a_moderator_sees_the_communitys_feeds(app, env):
    client, mine, theirs, mod = env
    a_feed(mine, title='Mine')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/community/{mine.name}/rss_feeds')

    assert response.status_code == 200
    assert [feed.title for feed in render.call_args.kwargs['rss_feeds']] == ['Mine']


def test_only_this_communitys_feeds_are_listed(app, env):
    client, mine, theirs, mod = env
    a_feed(mine, title='Mine')
    a_feed(theirs, title='Theirs')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{mine.name}/rss_feeds')

    assert [feed.title for feed in render.call_args.kwargs['rss_feeds']] == ['Mine']


def test_a_non_moderator_is_refused_the_feed_list(app, env):
    """D1012. This used to fall off the end of the function and return None,
    which Flask reports as `TypeError: The view function ... did not return a
    valid response` -- a 500 where a refusal was meant. Measured."""
    client, mine, theirs, mod = env
    stranger = make_user(instance(), 'stranger', local=True)
    db.session.commit()
    other = app.test_client()
    login(other, stranger)

    response = other.get(f'/community/{mine.name}/rss_feeds')

    assert response.status_code == 403


def test_an_unknown_community_has_no_feed_list(app, env):
    """D1012's other arm, and the same 500 before the fix."""
    client, mine, theirs, mod = env

    response = client.get('/community/nonexistent/rss_feeds')

    assert response.status_code == 404


def test_an_admin_sees_any_communitys_feeds(app, env):
    """`or current_user.is_admin()` -- an admin moderates nothing and still
    reaches the page."""
    client, mine, theirs, mod = env
    a_feed(theirs, title='Theirs')
    admin = app.test_client()
    login(admin, db.session.get(type(mod), 1))

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = admin.get(f'/community/{theirs.name}/rss_feeds')

    assert response.status_code == 200
    assert [feed.title for feed in render.call_args.kwargs['rss_feeds']] == ['Theirs']


def test_a_banned_user_cannot_reach_the_feed_list(app, env):
    client, mine, theirs, mod = env
    mod.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{mine.name}/rss_feeds')

    assert render.call_args is None


# --------------------------------------------------------------------------
# D1010 -- whose feed is it
# --------------------------------------------------------------------------


def test_a_moderator_can_edit_their_own_communitys_feed(app, env):
    client, mine, theirs, mod = env
    feed = a_feed(mine, title='Before')
    token = csrf(app, client)

    response = client.post(f'/community/community/{mine.id}/feed/{feed.id}',
                           data=feed_payload(token, name='After',
                                             url='https://example.com/new.xml'))

    assert response.status_code == 302
    db.session.refresh(feed)
    assert feed.title == 'After'
    assert feed.url == 'https://example.com/new.xml'


def test_a_moderator_cannot_edit_another_communitys_feed(app, env):
    """D1010. `feed_id` and `community_id` both come from the URL and nothing
    tied them together, so a moderator of any community could rewrite another
    community's feed -- and the url is what the background fetcher polls to
    create posts in THAT community. Measured:
    `PROBE r1 their feed is now: Taken over https://attacker.example/feed.xml`."""
    client, mine, theirs, mod = env
    feed = a_feed(theirs, title='Theirs')
    token = csrf(app, client)

    response = client.post(f'/community/community/{mine.id}/feed/{feed.id}',
                           data=feed_payload(token, name='Taken over',
                                             url='https://attacker.example/feed.xml'))

    assert response.status_code == 404
    db.session.refresh(feed)
    assert feed.title == 'Theirs'
    assert feed.url == 'https://example.com/feed.xml'


def test_a_moderator_cannot_delete_another_communitys_feed(app, env):
    """D1010's second site, and the destructive one: `delete_dependencies()`
    deletes every post the feed created. Measured:
    `PROBE r2 their feed still exists? False`."""
    client, mine, theirs, mod = env
    feed = a_feed(theirs, title='Theirs')
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{mine.id}/feed/{feed.id}/delete',
        data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 404
    assert db.session.get(RssFeed, feed.id) is not None


def test_a_moderator_can_delete_their_own_communitys_feed(app, env):
    client, mine, theirs, mod = env
    feed = a_feed(mine, title='Mine')
    token = csrf(app, client)

    response = client.post(
        f'/community/community/{mine.id}/feed/{feed.id}/delete',
        data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 302
    assert db.session.get(RssFeed, feed.id) is None


def test_the_delete_form_asks_first(app, env):
    """A GET renders the confirmation rather than deleting."""
    client, mine, theirs, mod = env
    feed = a_feed(mine, title='Mine')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(
            f'/community/community/{mine.id}/feed/{feed.id}/delete')

    assert response.status_code == 200
    assert render.call_args.kwargs['title'] == 'Are you sure?'
    assert db.session.get(RssFeed, feed.id) is not None


def test_an_unknown_feed_id_is_a_404(app, env):
    """D1011. `rss_feed.title = form.name.data` on a None row was
    `AttributeError: 'NoneType' object has no attribute 'title'`."""
    client, mine, theirs, mod = env
    token = csrf(app, client)

    response = client.post(f'/community/community/{mine.id}/feed/9999',
                           data=feed_payload(token))

    assert response.status_code == 404


def test_an_unknown_community_id_is_a_404(app, env):
    client, mine, theirs, mod = env

    response = client.get('/community/community/9999/feed/new')

    assert response.status_code == 404


def test_a_new_feed_is_created(app, env):
    client, mine, theirs, mod = env
    token = csrf(app, client)

    response = client.post(f'/community/community/{mine.id}/feed/new',
                           data=feed_payload(token, name='A new feed',
                                             check_frequency='360'))

    assert response.status_code == 302
    created = RssFeed.query.filter_by(community_id=mine.id).one()
    assert created.title == 'A new feed'
    assert created.check_frequency == 360
    assert created.flair_id is None


def test_a_feed_can_carry_flair(app, env):
    """`flair_id` is `-1` for "none" and an id otherwise, and the two are
    handled by the same expression in both arms."""
    client, mine, theirs, mod = env
    flair = CommunityFlair(community_id=mine.id, flair='Imported',
                           text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{mine.id}/feed/new',
                data=feed_payload(token, flair=str(flair.id)))

    created = RssFeed.query.filter_by(community_id=mine.id).one()
    assert created.flair_id == flair.id


def test_editing_a_feed_resets_its_error_count(app, env):
    """`error_count = 0` on save is what revives a feed the fetcher has given
    up on, and it is the one field the form does not carry."""
    client, mine, theirs, mod = env
    feed = a_feed(mine)
    feed.error_count = 12
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{mine.id}/feed/{feed.id}',
                data=feed_payload(token))

    db.session.refresh(feed)
    assert feed.error_count == 0


def test_the_edit_form_opens_on_the_stored_feed(app, env):
    client, mine, theirs, mod = env
    flair = CommunityFlair(community_id=mine.id, flair='Imported',
                           text_color='#000000', background_color='#ffffff')
    db.session.add(flair)
    db.session.commit()
    feed = a_feed(mine, title='Stored', url='https://example.com/stored.xml')
    feed.flair_id = flair.id
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/community/{mine.id}/feed/{feed.id}')

    form = render.call_args.kwargs['form']
    assert form.name.data == 'Stored'
    assert form.url.data == 'https://example.com/stored.xml'
    assert form.check_frequency.data == '60'
    assert form.flair.data == str(flair.id)


def test_the_new_feed_form_opens_empty(app, env):
    """`if rss_feed:` -- the add form has nothing to pre-fill from."""
    client, mine, theirs, mod = env

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/community/{mine.id}/feed/new')

    assert render.call_args.kwargs['form'].name.data is None
    assert render.call_args.kwargs['title'] == 'Add RSS feed'


def test_a_non_moderator_cannot_reach_the_edit_form(app, env):
    client, mine, theirs, mod = env
    stranger = make_user(instance(), 'stranger', local=True)
    db.session.commit()
    other = app.test_client()
    login(other, stranger)

    response = other.get(f'/community/community/{mine.id}/feed/new')

    assert response.status_code == 403


def test_the_feature_can_be_turned_off(app, env):
    """`and current_app.config['RSS_FEEDS']` gates the edit form as well as
    the "add" button, so an instance with the feature off refuses the form
    rather than only hiding the link to it."""
    client, mine, theirs, mod = env
    app.config['RSS_FEEDS'] = False
    try:
        response = client.get(f'/community/community/{mine.id}/feed/new')
    finally:
        app.config['RSS_FEEDS'] = True

    assert response.status_code == 403


def test_a_banned_user_cannot_edit_a_feed(app, env):
    client, mine, theirs, mod = env
    feed = a_feed(mine, title='Before')
    mod.banned = True
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{mine.id}/feed/{feed.id}',
                data=feed_payload(token, name='After'))

    db.session.refresh(feed)
    assert feed.title == 'Before'


def test_a_banned_user_cannot_delete_a_feed(app, env):
    client, mine, theirs, mod = env
    feed = a_feed(mine, title='Mine')
    mod.banned = True
    db.session.commit()
    token = csrf(app, client)

    client.post(f'/community/community/{mine.id}/feed/{feed.id}/delete',
                data={'submit': 'Delete', 'csrf_token': token})

    assert db.session.get(RssFeed, feed.id) is not None


def test_a_non_event_post_that_has_an_event_row_is_still_omitted(app, env):
    """`Post.type == POST_TYPE_EVENT` and D1014's `if post.event is None:
    continue` overlap: an ordinary post has no Event row, so the nil guard
    hides it even when the type filter is gone. The discriminating case is a
    post that HAS an Event row and is no longer an event -- which is what a
    post whose type was changed after creation looks like."""
    client, mine, theirs, mod = env
    an_event(mine, mod, 1)
    changed = a_post(mine, mod, 2, type=POST_TYPE_EVENT)
    db.session.add(Event(post_id=changed.id,
                         start=utcnow() + timedelta(days=3),
                         end=utcnow() + timedelta(days=4)))
    db.session.commit()
    changed.type = 1  # POST_TYPE_LINK
    db.session.commit()

    response = app.test_client().get(f'/community/{mine.name}/ical')

    assert b'post 1' in response.data
    assert b'post 2' not in response.data
