"""The rest of `app/community/routes.py`: fragments, lookups and the queues.

Sub-project 80, slice I -- the last slice of this blueprint, which is why the
floor on `app/community/routes.py` is taken in this round. Seven defects, all
measured:

* `check_url_already_posted` was UNAUTHENTICATED and fetched a caller-supplied
  URL from the server (D1025);
* `community_changed` rendered a private community's flair list and side pane
  to anyone (D1026) and answered 500 for a non-numeric id (D1027);
* `remove_icon` and `remove_header` had NO authorization at all, so any
  logged-in account could delete any community's icon or banner, from disk as
  well as from the database (D1028);
* `flip_community_theme_allowed` took the user id from the URL, so any account
  could flip anyone else's per-community theme setting (D1029);
* the moderation queue's pagination links omitted `actor`, so building them
  raised `BuildError` -- a 500 on the page exactly when the queue overflowed
  (D1030);
* `community_moderate_comments` returned None for a non-moderator and for a
  name that does not resolve (D1031), D1012's shape for the third time.
"""
from unittest.mock import patch

import pytest

from app import db
from app.constants import POST_STATUS_REVIEWING
from app.models import (Community, CommunityFlair, CommunityWikiPage, File,
                        Instance, Language, ModLog, Post, PostReply, Report,
                        Site, User)
from app.utils import utcnow
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_post_reply,
                             make_user)

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


def as_user(app, user):
    client = app.test_client()
    login(client, user)
    return client


@pytest.fixture
def env(app, db_session):
    """`mod` moderates `community`; `member` merely belongs to it; `outsider`
    has no relationship to it at all -- which is the state D1028 and D1029 are
    about."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True)
    member = make_user(local, 'member', local=True)
    outsider = make_user(local, 'outsider', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    moderation = make_community_member(mod, community)
    moderation.is_moderator = True
    make_community_member(member, community)
    db.session.commit()
    return as_user(app, mod), community, mod, member, outsider


def an_image(name='icon.png'):
    image = File(source_url=f'https://test.piefed.local/{name}',
                 file_path=f'app/static/media/{name}')
    db.session.add(image)
    db.session.commit()
    return image


# --------------------------------------------------------------------------
# D1025 -- the outbound fetch
# --------------------------------------------------------------------------


def test_the_url_check_needs_a_login(app, env):
    """D1025. This route fetches a caller-supplied URL from the server, and it
    was reachable with no session at all -- an unauthenticated outbound-fetch
    primitive, the same family as D993's unbounded email. Measured:
    `PROBE i1 outbound fetch attempted: True ('https://example.com/x',)`."""
    with patch('app.community.routes.httpx_client.get') as fetch:
        response = app.test_client().get(
            '/community/check_url_already_posted?link_url=https://example.com/x')

    assert response.status_code == 302
    assert fetch.call_args is None


def test_a_logged_in_user_can_check_a_url(app, env):
    client, community, mod, member, outsider = env
    posted = make_post(community, mod, 'https://test.piefed.local/p/1',
                       title='already here')
    posted.url = 'https://example.com/x'
    db.session.commit()

    with patch('app.community.routes.httpx_client.get') as fetch:
        fetch.return_value.status_code = 200
        fetch.return_value.content = b'<html><head><title>A page</title></head></html>'
        response = client.get(
            '/community/check_url_already_posted?link_url=https://example.com/x')

    assert response.status_code == 200
    assert b'already here' in response.data


def test_the_url_check_needs_a_url(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/check_url_already_posted')

    assert response.status_code == 404


def test_the_url_check_normalises_the_link_first(app, env):
    """`remove_tracking_from_link` runs before the lookup. It is NOT a general
    tracking-parameter stripper -- it rewrites youtu.be links to their
    youtube.com form and returns everything else unchanged (app/utils.py:3298)
    -- so the case where it changes the answer is a share link for a video
    already posted in its canonical form."""
    client, community, mod, member, outsider = env
    posted = make_post(community, mod, 'https://test.piefed.local/p/1',
                       title='already here')
    posted.url = 'https://youtube.com/watch?v=abc123'
    db.session.commit()

    with patch('app.community.routes.httpx_client.get') as fetch:
        fetch.return_value.status_code = 200
        fetch.return_value.content = b'<html></html>'
        response = client.get('/community/check_url_already_posted'
                              '?link_url=https%3A%2F%2Fyoutu.be%2Fabc123')

    assert b'already here' in response.data


# --------------------------------------------------------------------------
# retrieve_metadata_of_url
# --------------------------------------------------------------------------


def metadata(app, body=b'', status=200, url='https://example.com/x'):
    from app.community.routes import retrieve_metadata_of_url

    with app.test_request_context():
        with patch('app.community.routes.httpx_client.get') as fetch:
            fetch.return_value.status_code = status
            fetch.return_value.content = body
            return retrieve_metadata_of_url(url)


def test_the_open_graph_title_is_preferred(app, env):
    title, description = metadata(app, (
        b'<html><head><meta property="og:title" content=" Social title ">'
        b'<title>HTML title</title></head></html>'))

    assert title == 'Social title'


def test_the_html_title_is_the_fallback(app, env):
    """`if title == '':` -- a page with no og:title, and one whose og:title is
    empty, both fall through to `<title>`."""
    title, description = metadata(app, (
        b'<html><head><title> HTML title </title></head></html>'))

    assert title == 'HTML title'


def test_an_empty_open_graph_title_falls_back_too(app, env):
    title, description = metadata(app, (
        b'<html><head><meta property="og:title" content="">'
        b'<title>HTML title</title></head></html>'))

    assert title == 'HTML title'


def test_a_page_with_no_title_gives_an_empty_one(app, env):
    title, description = metadata(app, b'<html><head></head></html>')

    assert (title, description) == ('', '')


def test_the_description_is_read_from_the_meta_tag(app, env):
    title, description = metadata(app, (
        b'<html><head><meta name="description" content=" What this is "></head></html>'))

    assert description == 'What this is'


def test_a_non_200_response_yields_nothing(app, env):
    title, description = metadata(app, b'<html><title>Ignored</title></html>',
                                  status=404)

    assert (title, description) == ('', '')


def test_a_refused_uri_is_not_fetched(app, env):
    """`is_invalid_get_request_uri` is what keeps this off private ranges and
    off `.local`, and it runs BEFORE the request is made."""
    from app.community.routes import retrieve_metadata_of_url

    with app.test_request_context():
        with patch('app.community.routes.httpx_client.get') as fetch:
            with patch('app.community.routes.is_invalid_get_request_uri',
                       return_value=True):
                result = retrieve_metadata_of_url('https://internal.example/x')

    assert result == ('', '')
    assert fetch.call_args is None


def test_a_transport_failure_yields_nothing(app, env):
    """The `except Exception:` arm. A metadata lookup must not be able to fail
    the page that asked for it."""
    from app.community.routes import retrieve_metadata_of_url

    with app.test_request_context():
        with patch('app.community.routes.httpx_client.get',
                   side_effect=RuntimeError('connection reset')):
            result = retrieve_metadata_of_url('https://example.com/x')

    assert result == ('', '')


# --------------------------------------------------------------------------
# D1026, D1027 -- the community_changed fragment
# --------------------------------------------------------------------------


def test_the_changed_fragment_renders_a_public_community(app, env):
    client, community, mod, member, outsider = env
    db.session.add(CommunityFlair(community_id=community.id, flair='Question',
                                  text_color='#000000',
                                  background_color='#ffffff'))
    db.session.commit()

    response = client.get(
        f'/community/community_changed?communities={community.id}')

    assert response.status_code == 200
    assert b'Question' in response.data


def test_the_changed_fragment_refuses_a_private_community(app, env):
    """D1026. This fragment renders the community's flair list and the whole
    side pane, and had none of `show_community`'s refusals -- D1017's shape at
    the second fragment endpoint. Measured anonymously:
    `PROBE i2 title leaked: True`."""
    client, community, mod, member, outsider = env
    community.private = True
    community.title = 'Secret Community'
    db.session.commit()

    response = app.test_client().get(
        f'/community/community_changed?communities={community.id}')

    assert response.status_code == 403
    assert b'Secret Community' not in response.data


def test_a_member_still_gets_the_changed_fragment(app, env):
    client, community, mod, member, outsider = env
    community.private = True
    db.session.commit()

    response = as_user(app, member).get(
        f'/community/community_changed?communities={community.id}')

    assert response.status_code == 200


def test_the_changed_fragment_refuses_a_banned_community(app, env):
    client, community, mod, member, outsider = env
    community.banned = True
    db.session.commit()

    response = client.get(
        f'/community/community_changed?communities={community.id}')

    assert response.status_code == 404


def test_the_changed_fragment_needs_a_community(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/community_changed')

    assert response.status_code == 200
    assert response.data == b''


def test_a_non_numeric_id_is_not_a_500(app, env):
    """D1027. `db.session.get(Community, 'abc')` reached the database as a
    string -- `DataError: invalid input syntax for type integer: "abc"`, an
    unauthenticated 500 from a query parameter. Measured."""
    client, community, mod, member, outsider = env

    response = client.get('/community/community_changed?communities=abc')

    assert response.status_code == 200
    assert response.data == b''


def test_an_unknown_community_is_not_found(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/community_changed?communities=9999')

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1028 -- the icon and banner buttons
# --------------------------------------------------------------------------


@pytest.mark.parametrize('what, column, message', [
    ('remove_icon', 'icon_id', b'Icon removed'),
    ('remove_header', 'image_id', b'Banner removed'),
])
def test_a_moderator_can_remove_the_communitys_images(app, env, what, column,
                                                      message):
    client, community, mod, member, outsider = env
    setattr(community, column, an_image().id)
    db.session.commit()
    token = csrf(app, client)

    with patch('app.models.File.delete_from_disk'):
        response = client.post(f'/community/community/{community.id}/{what}',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert message in response.data
    db.session.refresh(community)
    assert getattr(community, column) is None


@pytest.mark.parametrize('what, column', [
    ('remove_icon', 'icon_id'), ('remove_header', 'image_id'),
])
def test_a_stranger_cannot_remove_the_communitys_images(app, env, what,
                                                        column):
    """D1028. There was no authorization here AT ALL -- `@login_required` and
    nothing else -- so any account could delete any community's icon or
    banner, from disk as well as from the database. Measured, from an account
    with no relationship to the community: `PROBE i4 status: 200 / icon_id now:
    None`."""
    client, community, mod, member, outsider = env
    image = an_image()
    setattr(community, column, image.id)
    db.session.commit()
    attacker = as_user(app, outsider)
    token = csrf(app, attacker)

    with patch('app.models.File.delete_from_disk') as deleted:
        response = attacker.post(
            f'/community/community/{community.id}/{what}',
            data={'csrf_token': token})

    assert response.status_code == 403
    db.session.refresh(community)
    assert getattr(community, column) == image.id
    assert deleted.call_args is None


@pytest.mark.parametrize('what', ['remove_icon', 'remove_header'])
def test_an_admin_can_remove_any_communitys_images(app, env, what):
    """`or current_user.is_admin()` -- an admin moderates nothing and still
    has to be able to take down an image."""
    client, community, mod, member, outsider = env
    community.icon_id = an_image('icon.png').id
    community.image_id = an_image('header.png').id
    db.session.commit()
    admin = as_user(app, db.session.get(User, 1))
    token = csrf(app, admin)

    with patch('app.models.File.delete_from_disk'):
        response = admin.post(f'/community/community/{community.id}/{what}',
                              data={'csrf_token': token})

    assert response.status_code == 200


@pytest.mark.parametrize('what', ['remove_icon', 'remove_header'])
def test_removing_an_image_a_community_does_not_have(app, env, what):
    """`if community.icon_id:` -- the button is idempotent, and the second
    press must not be an AttributeError on a None relationship."""
    client, community, mod, member, outsider = env
    token = csrf(app, client)

    response = client.post(f'/community/community/{community.id}/{what}',
                           data={'csrf_token': token})

    assert response.status_code == 200


@pytest.mark.parametrize('what', ['remove_icon', 'remove_header'])
def test_removing_an_image_from_an_unknown_community(app, env, what):
    client, community, mod, member, outsider = env
    token = csrf(app, client)

    response = client.post(f'/community/community/9999/{what}',
                           data={'csrf_token': token})

    assert response.status_code == 404


# --------------------------------------------------------------------------
# D1029 -- the per-community theme toggle
# --------------------------------------------------------------------------


def test_a_user_can_flip_their_own_theme_setting(app, env):
    from app.community.util import get_community_theme_allowed

    client, community, mod, member, outsider = env
    viewer = as_user(app, member)
    token = csrf(app, viewer)

    response = viewer.post(
        f'/community/community/{community.id}/{member.id}/flip_community_theme_allowed',
        data={'csrf_token': token})

    assert response.status_code == 200
    assert response.headers['HX-Refresh'] == 'true'
    assert get_community_theme_allowed(community.id, member.id) is False


def test_flipping_it_back_says_so(app, env):
    """The two arms return different labels, because the button shows the
    action rather than the state."""
    from app.community.util import get_community_theme_allowed

    client, community, mod, member, outsider = env
    viewer = as_user(app, member)
    token = csrf(app, viewer)
    first = viewer.post(
        f'/community/community/{community.id}/{member.id}/flip_community_theme_allowed',
        data={'csrf_token': token})

    second = viewer.post(
        f'/community/community/{community.id}/{member.id}/flip_community_theme_allowed',
        data={'csrf_token': token})

    assert first.data != second.data
    assert get_community_theme_allowed(community.id, member.id) is True


def test_nobody_can_flip_somebody_elses_theme_setting(app, env):
    """D1029. `user_id` came from the URL and went straight to
    `set_community_theme_allowed`. Measured: `PROBE i5 victim theme setting
    before/after: True False`, from an unrelated account."""
    from app.community.util import get_community_theme_allowed

    client, community, mod, member, outsider = env
    attacker = as_user(app, outsider)
    token = csrf(app, attacker)

    response = attacker.post(
        f'/community/community/{community.id}/{member.id}/flip_community_theme_allowed',
        data={'csrf_token': token})

    assert response.status_code == 403
    assert get_community_theme_allowed(community.id, member.id) is True


# --------------------------------------------------------------------------
# D1030, D1031 -- the moderation queues
# --------------------------------------------------------------------------


def a_report(community, reporter, description='a report', source_instance=1):
    report = Report(reasons='1', description=description,
                    type=1, reporter_id=reporter.id,
                    in_community_id=community.id, status=0,
                    source_instance_id=source_instance)
    db.session.add(report)
    db.session.commit()
    return report


def test_a_moderator_sees_the_report_queue(app, env):
    client, community, mod, member, outsider = env
    a_report(community, member)

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/community/{community.name}/moderate')

    assert response.status_code == 200
    assert render.call_args.kwargs['reports'].total == 1


@pytest.mark.parametrize('filter_value, kept', [
    ('local', 'local report'), ('remote', 'remote report'),
])
def test_the_queue_can_be_narrowed_by_origin(app, env, filter_value, kept):
    """`local_remote` splits on `Report.source_instance_id == 1`, and the two
    arms are separate filters."""
    client, community, mod, member, outsider = env
    other = make_instance('other.example', software='piefed')
    a_report(community, member, 'local report', source_instance=1)
    a_report(community, member, 'remote report', source_instance=other.id)

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate'
                   f'?local_remote={filter_value}')

    descriptions = [r.description for r in render.call_args.kwargs['reports'].items]
    assert descriptions == [kept]


def test_the_queue_pagination_links_name_the_community(app, env):
    """D1030. Both links omitted `actor`, which this endpoint's rule requires,
    so building them raised `BuildError` -- and they are only built when the
    queue has more than one page, so the page answered 500 exactly when a
    community was being flooded with reports. Measured:
    `PROBE i6 RAISED: BuildError ... Did you forget to specify values
    ['actor']?`."""
    client, community, mod, member, outsider = env

    class Paginated:
        items = []
        total = 0
        has_next = True
        has_prev = True
        next_num = 3
        prev_num = 1

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        with patch('flask_sqlalchemy.query.Query.paginate',
                   return_value=Paginated()):
            response = client.get(
                f'/community/{community.name}/moderate?page=2')

    assert response.status_code == 200
    assert community.name in render.call_args.kwargs['next_url']
    assert community.name in render.call_args.kwargs['prev_url']


def test_a_non_moderator_is_refused_the_report_queue(app, env):
    client, community, mod, member, outsider = env

    response = as_user(app, outsider).get(
        f'/community/{community.name}/moderate')

    assert response.status_code == 401


def test_an_unknown_community_has_no_report_queue(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/nonexistent/moderate')

    assert response.status_code == 404


def test_a_banned_user_cannot_read_the_report_queue(app, env):
    client, community, mod, member, outsider = env
    mod.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate')

    assert render.call_args is None


def test_a_moderator_sees_the_comment_queue(app, env):
    client, community, mod, member, outsider = env
    post = make_post(community, mod, 'https://test.piefed.local/p/1',
                     title='a post')
    make_post_reply(post, member, body='a reply')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/community/{community.name}/moderate/comments')

    assert response.status_code == 200
    assert [r.body for r in render.call_args.kwargs['post_replies'].items] == ['a reply']


def test_the_comment_queue_omits_deleted_replies(app, env):
    client, community, mod, member, outsider = env
    post = make_post(community, mod, 'https://test.piefed.local/p/1',
                     title='a post')
    make_post_reply(post, member, body='a reply')
    gone = make_post_reply(post, member, body='a deleted reply')
    gone.deleted = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/comments')

    assert [r.body for r in render.call_args.kwargs['post_replies'].items] == ['a reply']


def test_a_non_moderator_is_refused_the_comment_queue(app, env):
    """D1031 -- D1012's shape for the THIRD time in this file. Neither arm
    returned anything, so this was `TypeError: The view function ... did not
    return a valid response` rather than a refusal."""
    client, community, mod, member, outsider = env

    response = as_user(app, outsider).get(
        f'/community/{community.name}/moderate/comments')

    assert response.status_code == 401


def test_an_unknown_community_has_no_comment_queue(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/nonexistent/moderate/comments')

    assert response.status_code == 404


def test_a_banned_user_cannot_read_the_comment_queue(app, env):
    client, community, mod, member, outsider = env
    mod.banned = True
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/comments')

    assert render.call_args is None


def test_the_comment_queue_pages(app, env):
    client, community, mod, member, outsider = env
    post = make_post(community, mod, 'https://test.piefed.local/p/1',
                     title='a post')
    for number in range(60):
        make_post_reply(post, member, body=f'reply {number}')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/comments')

    assert render.call_args.kwargs['replies_next_url'] is not None
    assert render.call_args.kwargs['replies_prev_url'] is None


def test_the_second_page_of_comments_links_back(app, env):
    client, community, mod, member, outsider = env
    post = make_post(community, mod, 'https://test.piefed.local/p/1',
                     title='a post')
    for number in range(60):
        make_post_reply(post, member, body=f'reply {number}')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/comments'
                   '?replies_page=2')

    assert render.call_args.kwargs['replies_prev_url'] is not None


# --------------------------------------------------------------------------
# The mod log
# --------------------------------------------------------------------------


def test_a_moderator_sees_the_mod_log(app, env):
    client, community, mod, member, outsider = env
    db.session.add(ModLog(community_id=community.id, user_id=mod.id,
                          action='ban_user', created_at=utcnow(),
                          public=True))
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        response = client.get(f'/community/{community.name}/moderate/modlog')

    assert response.status_code == 200
    assert render.call_args.kwargs['modlog_entries'].total == 1


def test_the_mod_log_shows_only_this_communitys_entries(app, env):
    client, community, mod, member, outsider = env
    elsewhere = make_community('elsewhere')
    db.session.commit()
    db.session.add(ModLog(community_id=elsewhere.id, user_id=mod.id,
                          action='ban_user', created_at=utcnow()))
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/modlog')

    assert render.call_args.kwargs['modlog_entries'].total == 0


def test_a_non_moderator_is_refused_the_mod_log(app, env):
    client, community, mod, member, outsider = env

    response = as_user(app, outsider).get(
        f'/community/{community.name}/moderate/modlog')

    assert response.status_code == 401


def test_an_unknown_community_has_no_mod_log(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/nonexistent/moderate/modlog')

    assert response.status_code == 404


def test_the_mod_log_pages_in_smaller_blocks_on_low_bandwidth(app, env):
    """`per_page=100 if not low_bandwidth else 50` -- the cookie halves the
    page."""
    client, community, mod, member, outsider = env
    client.set_cookie('low_bandwidth', '1', domain='test.piefed.local')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/modlog')

    assert render.call_args.kwargs['modlog_entries'].per_page == 50
    assert render.call_args.kwargs['low_bandwidth'] is True


def test_the_mod_logs_pagination_links_name_the_community(app, env):
    client, community, mod, member, outsider = env
    for number in range(120):
        db.session.add(ModLog(community_id=community.id, user_id=mod.id,
                              action='ban_user', created_at=utcnow()))
    db.session.commit()

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/modlog')

    assert community.name in render.call_args.kwargs['next_url']
    assert render.call_args.kwargs['prev_url'] is None


# --------------------------------------------------------------------------
# Looking a remote community up
# --------------------------------------------------------------------------


def test_looking_up_a_local_community_redirects_to_it(app, env):
    client, community, mod, member, outsider = env

    response = client.get(
        f'/community/lookup/{community.name}/test.piefed.local')

    assert response.status_code == 302
    assert response.headers['Location'] == f'/c/{community.name}'


def test_looking_up_a_known_remote_community_redirects_to_it(app, env):
    client, community, mod, member, outsider = env
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    db.session.commit()

    response = client.get('/community/lookup/Remote/Other.example')

    assert response.status_code == 302
    assert response.headers['Location'] == '/c/remote@other.example'


def test_looking_up_an_unknown_community_searches_for_it(app, env):
    """The handle must be one this instance has no row for, or the `exists`
    branch redirects and the search never runs."""
    client, community, mod, member, outsider = env
    found = make_community('found', host='other.example')
    found.ap_id = 'found@other.example'
    db.session.commit()

    with patch('app.community.routes.search_for_community',
               return_value=found) as search:
        with patch('app.community.routes.render_template',
                   return_value='rendered') as render:
            response = client.get('/community/lookup/seeking/other.example')

    assert response.status_code == 200
    assert search.call_args.args == ('!seeking@other.example',)
    assert render.call_args.kwargs['new_community'] is found


def test_a_search_that_finds_nothing_says_so(app, env):
    client, community, mod, member, outsider = env

    with patch('app.community.routes.search_for_community',
               return_value=None):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template',
                       return_value='rendered'):
                client.get('/community/lookup/missing/other.example')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'Community not found' in messages


def test_a_blocked_instance_is_named_in_the_message(app, env):
    """`if 'is blocked.' in str(e)` -- the only exception this function reads
    rather than letting through."""
    client, community, mod, member, outsider = env

    with patch('app.community.routes.search_for_community',
               side_effect=Exception('that instance is blocked.')):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template',
                       return_value='rendered'):
                client.get('/community/lookup/blocked/other.example')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'blocked' in messages


def test_a_banned_community_that_is_found_is_named_as_banned(app, env):
    client, community, mod, member, outsider = env
    found = make_community('found', host='other.example')
    found.ap_id = 'found@other.example'
    found.banned = True
    db.session.commit()

    with patch('app.community.routes.search_for_community',
               return_value=found):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template',
                       return_value='rendered'):
                client.get('/community/lookup/seeking/other.example')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'banned from' in messages


# --------------------------------------------------------------------------
# The community-name datalist
# --------------------------------------------------------------------------


def write_community_list(app, communities, sfw=False):
    import json
    import pathlib

    name = 'all_sfw_communities' if sfw else 'all_communities'
    path = pathlib.Path('app/static/tmp') / f'{name}.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({name: communities}))
    return path


def test_the_name_search_offers_matching_communities(app, env):
    client, community, mod, member, outsider = env
    db.session.get(Site, 1).enable_nsfw = True
    db.session.commit()
    path = write_community_list(app, ['news@example.com', 'chat@example.com'])
    try:
        response = client.get('/community/search-names?address=news')
    finally:
        path.unlink()

    assert response.data == b'<option value="news@example.com"></option>'


def test_a_safe_for_work_instance_reads_the_other_file(app, env):
    """The two files are separate, and which one is read is the instance's
    nsfw setting -- a row against one says nothing about the other."""
    client, community, mod, member, outsider = env
    db.session.get(Site, 1).enable_nsfw = False
    db.session.commit()
    path = write_community_list(app, ['safe@example.com'], sfw=True)
    try:
        response = client.get('/community/search-names?address=safe')
    finally:
        path.unlink()

    assert b'safe@example.com' in response.data


def test_a_missing_list_is_not_an_error(app, env):
    """The bare `except:` -- the file is written by a scheduled task, so it is
    absent on a new instance until that task first runs."""
    import pathlib

    client, community, mod, member, outsider = env
    db.session.get(Site, 1).enable_nsfw = True
    db.session.commit()
    path = pathlib.Path('app/static/tmp/all_communities.json')
    backup = path.read_bytes() if path.exists() else None
    if path.exists():
        path.unlink()
    try:
        response = client.get('/community/search-names?address=news')
    finally:
        if backup is not None:
            path.write_bytes(backup)

    assert response.status_code == 200
    assert response.data == b''


def test_the_name_search_needs_an_address(app, env):
    client, community, mod, member, outsider = env

    response = client.get('/community/search-names')

    assert response.data == b''


# --------------------------------------------------------------------------
# Favouriting, notifications and the remote refresh
# --------------------------------------------------------------------------


def test_favouriting_a_community(app, env):
    client, community, mod, member, outsider = env
    token = csrf(app, client)

    with patch('app.community.routes.favorite_community',
               return_value='done') as favourite:
        response = client.post(f'/community/{community.id}/fave',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert favourite.call_args.args[0] == community.id


def test_favouriting_an_unknown_community_is_a_404(app, env):
    from sqlalchemy.orm.exc import NoResultFound

    client, community, mod, member, outsider = env
    token = csrf(app, client)

    with patch('app.community.routes.favorite_community',
               side_effect=NoResultFound):
        response = client.post('/community/9999/fave',
                               data={'csrf_token': token})

    assert response.status_code == 404


def test_toggling_notifications_for_a_community(app, env):
    client, community, mod, member, outsider = env
    token = csrf(app, client)

    with patch('app.community.routes.subscribe_community',
               return_value='done') as subscribe:
        response = client.post(f'/community/{community.id}/notification',
                               data={'csrf_token': token})

    assert response.status_code == 200
    assert subscribe.call_args.args[0] == community.id


def test_toggling_notifications_for_an_unknown_community_is_a_404(app, env):
    from sqlalchemy.orm.exc import NoResultFound

    client, community, mod, member, outsider = env
    token = csrf(app, client)

    with patch('app.community.routes.subscribe_community',
               side_effect=NoResultFound):
        response = client.post('/community/9999/notification',
                               data={'csrf_token': token})

    assert response.status_code == 404


def test_an_admin_can_refresh_a_remote_community(app, env):
    client, community, mod, member, outsider = env
    remote = make_community('remote', host='other.example')
    remote.ap_id = 'remote@other.example'
    db.session.commit()
    admin = as_user(app, db.session.get(User, 1))

    with patch('app.community.routes.schedule_actor_refresh') as refresh:
        response = admin.get('/community/c/remote@other.example/fixup_from_remote')

    assert response.status_code == 302
    assert refresh.call_args.args[0].id == remote.id
    assert refresh.call_args.kwargs == {'override': True}


def test_a_local_community_has_nothing_to_refresh(app, env):
    """`if "@" not in actor:` -- a local community has no remote to fetch."""
    client, community, mod, member, outsider = env
    admin = as_user(app, db.session.get(User, 1))

    with patch('app.community.routes.schedule_actor_refresh') as refresh:
        with patch('app.community.routes.flash') as flashed:
            response = admin.get(
                f'/community/c/{community.name}/fixup_from_remote')

    assert response.status_code == 302
    assert refresh.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'local community' in messages


def test_an_unknown_remote_community_is_not_refreshed(app, env):
    """`if community:` -- a handle with no row is a redirect, not a crash."""
    client, community, mod, member, outsider = env
    admin = as_user(app, db.session.get(User, 1))

    with patch('app.community.routes.schedule_actor_refresh') as refresh:
        response = admin.get('/community/c/missing@other.example/fixup_from_remote')

    assert response.status_code == 302
    assert refresh.call_args is None


def test_a_non_admin_cannot_refresh_a_community(app, env):
    """The permission decorator admits staff; the body then asks for an admin
    specifically, and that second check is the one under test."""
    from tests.factories import grant_permission

    client, community, mod, member, outsider = env
    grant_permission(mod, 'change instance settings')
    db.session.commit()

    with patch('app.community.routes.schedule_actor_refresh') as refresh:
        with patch('app.community.routes.flash') as flashed:
            response = client.get(
                '/community/c/remote@other.example/fixup_from_remote')

    assert response.status_code == 302
    assert refresh.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'only for admins' in messages


def test_an_anonymous_lookup_is_sent_to_log_in(app, env):
    """The `else:` of `if current_user.is_authenticated:` -- searching for a
    remote community makes this instance fetch it, so it is not something an
    anonymous visitor can trigger."""
    client, community, mod, member, outsider = env

    with patch('app.community.routes.search_for_community') as search:
        with patch('app.community.routes.flash') as flashed:
            response = app.test_client().get(
                '/community/lookup/seeking/other.example')

    assert response.status_code == 302
    assert search.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'requires login' in messages


def test_a_nsfw_instance_does_not_blame_nsfw_for_a_missing_community(app, env):
    """The two "not found" messages differ: an instance with NSFW disabled
    says the community may have been filtered for that reason, and one with it
    enabled must not say so."""
    client, community, mod, member, outsider = env
    db.session.get(Site, 1).enable_nsfw = True
    db.session.commit()

    with patch('app.community.routes.search_for_community',
               return_value=None):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template',
                       return_value='rendered'):
                client.get('/community/lookup/seeking/other.example')

    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert messages.strip() == 'Community not found.'


# --------------------------------------------------------------------------
# Subscribing, and deleting a wiki page
# --------------------------------------------------------------------------


def able_to_subscribe(user):
    """`subscribe` carries validation_required and approval_required behind
    login_required -- an unverified account with no private_key is redirected
    before the route runs. Same trap as slice E's `able_to_post`."""
    user.verified = True
    user.private_key = 'a private key'
    db.session.commit()
    return user


def test_subscribing_by_post_returns_the_leave_button(app, env):
    """`admin_preload=request.method == 'POST'` -- htmx posts and gets a
    fragment back, and the no-JS path gets a redirect. D994 is why the GET arm
    is still there."""
    client, community, mod, member, outsider = env
    viewer = as_user(app, able_to_subscribe(member))
    token = csrf(app, viewer)

    with patch('app.community.routes.do_subscribe') as subscribe:
        with patch('app.community.routes.render_template',
                   return_value='rendered') as render:
            response = viewer.post(f'/community/{community.name}/subscribe',
                                   data={'csrf_token': token})

    assert response.status_code == 200
    assert subscribe.call_args.kwargs == {'admin_preload': True}
    assert render.call_args.args[0] == 'community/_leave_button.html'


def test_subscribing_by_get_sends_the_visitor_back(app, env):
    client, community, mod, member, outsider = env
    viewer = as_user(app, able_to_subscribe(member))

    with patch('app.community.routes.do_subscribe') as subscribe:
        response = viewer.get(f'/community/{community.name}/subscribe')

    assert response.status_code == 302
    assert subscribe.call_args.kwargs == {'admin_preload': False}


def a_wiki_page(community, author, title='A page'):
    page = CommunityWikiPage(community_id=community.id, title=title,
                             slug=title.lower().replace(' ', '-'),
                             body='the body', body_html='<p>the body</p>',
                             who_can_edit=0)
    db.session.add(page)
    db.session.commit()
    return page


def test_a_moderator_can_delete_a_wiki_page(app, env):
    client, community, mod, member, outsider = env
    page = a_wiki_page(community, mod)

    token = csrf(app, client)
    response = client.post(
        f'/community/{community.name}/moderate/wiki/{page.id}/delete',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert db.session.get(CommunityWikiPage, page.id) is None


def test_someone_who_may_not_edit_cannot_delete_a_wiki_page(app, env):
    """`page.can_edit(current_user, community)` is the whole check, and it
    answers False for a page belonging to another community as well as for a
    caller without the right -- the guard recorded in `can_edit` itself."""
    client, community, mod, member, outsider = env
    page = a_wiki_page(community, mod)

    attacker = as_user(app, outsider)
    token = csrf(app, attacker)
    response = attacker.post(
        f'/community/{community.name}/moderate/wiki/{page.id}/delete',
        data={'csrf_token': token})

    assert response.status_code == 302
    assert db.session.get(CommunityWikiPage, page.id) is not None


def test_deleting_a_wiki_page_of_an_unknown_community_is_a_404(app, env):
    client, community, mod, member, outsider = env
    page = a_wiki_page(community, mod)

    token = csrf(app, client)
    response = client.post(
        f'/community/nonexistent/moderate/wiki/{page.id}/delete',
        data={'csrf_token': token})

    assert response.status_code == 404


def test_deleting_an_unknown_wiki_page_is_a_404(app, env):
    client, community, mod, member, outsider = env

    token = csrf(app, client)
    response = client.post(
        f'/community/{community.name}/moderate/wiki/9999/delete',
        data={'csrf_token': token})

    assert response.status_code == 404


def test_an_empty_link_is_not_a_url_check(app, env):
    """`if url:` rather than `if url is not None:` -- the htmx trigger fires on
    every keystroke, including the one that empties the field, and an empty
    string must not become a lookup for every post with no url."""
    client, community, mod, member, outsider = env

    with patch('app.community.routes.httpx_client.get') as fetch:
        response = client.get('/community/check_url_already_posted?link_url=')

    assert response.status_code == 404
    assert fetch.call_args is None


@pytest.mark.parametrize('columns', [
    {'deleted': True},
    {'status': POST_STATUS_REVIEWING},
    {'microblog': True},
    {'from_bot': True},
])
def test_the_url_check_lists_only_posts_anyone_could_see(app, env, columns):
    """Four filters, four rows. A "this was already posted" warning that names
    a removed post tells the submitter something the site will not show them,
    and one that names a post still in review leaks the review queue."""
    client, community, mod, member, outsider = env
    visible = make_post(community, mod, 'https://test.piefed.local/p/1',
                        title='visible post')
    visible.url = 'https://example.com/x'
    hidden = make_post(community, mod, 'https://test.piefed.local/p/2',
                       title='hidden post')
    hidden.url = 'https://example.com/x'
    for column, value in columns.items():
        setattr(hidden, column, value)
    db.session.commit()

    with patch('app.community.routes.httpx_client.get') as fetch:
        fetch.return_value.status_code = 200
        fetch.return_value.content = b'<html></html>'
        response = client.get(
            '/community/check_url_already_posted?link_url=https://example.com/x')

    assert b'visible post' in response.data
    assert b'hidden post' not in response.data


def test_the_report_queue_shows_only_this_communitys_reports(app, env):
    """`in_community_id=community.id` -- a moderator of one community must not
    be handed another community's reports, which name the reporter and the
    reported content."""
    client, community, mod, member, outsider = env
    elsewhere = make_community('elsewhere')
    db.session.commit()
    a_report(community, member, 'ours')
    a_report(elsewhere, member, 'theirs')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate')

    descriptions = [r.description for r in render.call_args.kwargs['reports'].items]
    assert descriptions == ['ours']


def test_the_comment_queue_shows_only_this_communitys_replies(app, env):
    client, community, mod, member, outsider = env
    elsewhere = make_community('elsewhere')
    db.session.commit()
    ours = make_post(community, mod, 'https://test.piefed.local/p/1',
                     title='ours')
    theirs = make_post(elsewhere, mod, 'https://test.piefed.local/p/2',
                       title='theirs')
    make_post_reply(ours, member, body='our reply')
    make_post_reply(theirs, member, body='their reply')

    with patch('app.community.routes.render_template',
               return_value='rendered') as render:
        client.get(f'/community/{community.name}/moderate/comments')

    assert [r.body for r in render.call_args.kwargs['post_replies'].items] == ['our reply']


def test_a_dict_entry_in_the_community_list_is_skipped(app, env):
    """`if isinstance(c, str)` -- `search_term in c` on a dict asks about its
    KEYS, so an entry whose key happens to match would otherwise be rendered
    into the datalist as a Python dict."""
    client, community, mod, member, outsider = env
    db.session.get(Site, 1).enable_nsfw = True
    db.session.commit()
    path = write_community_list(app, [{'news': 'news@example.com'},
                                      'news@other.example'])
    try:
        response = client.get('/community/search-names?address=news')
    finally:
        path.unlink()

    assert response.data == b'<option value="news@other.example"></option>'
