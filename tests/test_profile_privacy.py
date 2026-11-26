"""Profile privacy (app.profile_privacy, spec 2026-11-26-profile-privacy): a local user's profile shows other people
their microblog posts, and their community posts, comments and followers only as they choose.

The rules first, then each surface: the web profile, its RSS feed, the alpha API, the Mastodon account, the
ActivityPub followers collection, the counts, the settings page and the migration."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g
from sqlalchemy import inspect

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS
import app.profile_privacy as profile_privacy
from app.models import Language, Site
from tests.factories import a_keypair, make_admin, make_community, make_follow, make_instance, make_post, \
    make_post_reply, make_user
from tests.test_ap_content_objects import ap_get
from tests.test_user_settings import csrf, settings_payload

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / 'f2b7c9d4e1a6_user_profile_privacy.py'
HOST = 'test.piefed.local'

pytestmark = pytest.mark.usefixtures('site')


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    """Subject has one post in the microblogs community, one in another community and a comment; a remote account
    follows them. Viewer is another local account, staff an admin, remote a remote author with the same activity."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance(HOST, software='piefed')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    viewer = make_user(local, 'viewer', local=True)
    subject = make_user(local, 'subject', local=True)
    staff = make_admin(make_user(local, 'staff', local=True))
    peer = make_instance('peer.example')
    remote = make_user(peer, 'remote')
    follower = make_user(peer, 'follower')
    for user in (viewer, subject, staff):
        user.ap_profile_id = f'https://{HOST}/u/{user.user_name}'
    microblogs = make_community('microblogs', kind=COMMUNITY_KIND_MICROBLOGS)
    books = make_community('books')
    db.session.commit()
    rows = SimpleNamespace(viewer=viewer, subject=subject, staff=staff, remote=remote, follower=follower,
                           microblogs=microblogs, books=books)
    for author in (subject, remote):
        name = author.user_name
        setattr(rows, f'{name}_microblog', make_post(microblogs, author, f'https://{HOST}/post/{name}-mb',
                                                     title=f'{name} microblog'))
        community_post = make_post(books, author, f'https://{HOST}/post/{name}-books', title=f'{name} books')
        setattr(rows, f'{name}_community', community_post)
        setattr(rows, f'{name}_comment', make_post_reply(community_post, author, body=f'{name} comment'))
    subject.post_count, subject.post_reply_count = 2, 1
    make_follow(subject, follower, is_inward=True)
    subject.num_followers = 1
    db.session.commit()
    return rows


def profile(app, client, actor='subject'):
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        response = client.get(f'/u/{actor}')
    assert response.status_code == 200
    return render.call_args.kwargs


def titles(kwargs):
    return sorted(post.title for post in kwargs['posts'].items)


def overview(kwargs):
    return sorted(getattr(item, 'title', None) or item.body for item in kwargs['overview_items'])


# --------------------------------------------------------------------------
# The rules
# --------------------------------------------------------------------------


def test_every_viewer_but_the_person_and_their_staff_is_bound(app, env):
    subject = env.subject
    assert profile_privacy.exempt(subject, None) is False
    assert profile_privacy.exempt(subject, env.viewer.id) is False
    assert profile_privacy.exempt(subject, subject.id) is True
    assert profile_privacy.exempt(subject, env.staff.id) is True


def test_a_remote_person_has_no_settings(app, env):
    assert profile_privacy.exempt(env.remote, None) is True
    assert profile_privacy.hidden_posts_clause(env.remote, None) is None
    assert profile_privacy.hidden_replies_clause(env.remote, None) is None
    assert profile_privacy.followers_shown(env.remote, None) == 'list'


def test_someone_who_may_view_user_details_is_exempt(app, env):
    with patch('app.profile_privacy.user_access', side_effect=lambda permission, *_a, **_k:
               permission == 'view user details'):
        assert profile_privacy.exempt(env.subject, env.viewer.id) is True


def test_the_defaults_show_microblog_posts_and_the_followers_list(app, env):
    subject = env.subject
    assert (subject.profile_community_posts, subject.profile_comments, subject.profile_followers) == \
        (False, False, 'list')
    assert profile_privacy.shows_community_posts(subject, None) is False
    assert profile_privacy.shows_comments(subject, None) is False
    assert profile_privacy.followers_shown(subject, None) == 'list'


@pytest.mark.parametrize('stored', [None, '', 'everyone'])
def test_a_followers_value_outside_the_choices_reads_as_the_default(stored):
    assert profile_privacy.followers_of(SimpleNamespace(profile_followers=stored)) == 'list'


@pytest.mark.parametrize('setting, count', [('list', 7), ('count', 7), ('none', 0)])
def test_an_api_count_of_followers(app, env, setting, count):
    env.subject.profile_followers = setting
    assert profile_privacy.follower_count_shown(env.subject, None, 7) == count
    assert profile_privacy.follower_count_shown(env.subject, env.subject.id, 7) == 7


def test_the_filters_pass_other_authors(app, env):
    from app.models import Post, PostReply
    posts = Post.query.filter(profile_privacy.hidden_posts_clause(env.subject, None)).all()
    assert env.remote_community in posts and env.subject_microblog in posts
    assert env.subject_community not in posts
    replies = PostReply.query.filter(profile_privacy.hidden_replies_clause(env.subject, None)).all()
    assert replies == [env.remote_comment]


# --------------------------------------------------------------------------
# The web profile
# --------------------------------------------------------------------------


@pytest.mark.parametrize('signed_in', [False, True])
def test_by_default_only_microblog_posts_are_shown(app, env, signed_in):
    client = as_user(app, env.viewer) if signed_in else app.test_client()

    kwargs = profile(app, client)

    assert titles(kwargs) == ['subject microblog']
    assert kwargs['post_replies'].items == []
    assert overview(kwargs) == ['subject microblog']
    assert (kwargs['profile_post_count'], kwargs['profile_comment_count']) == (1, 0)
    assert kwargs['shows_comments'] is False


def test_community_posts_are_shown_when_opted_in(app, env):
    env.subject.profile_community_posts = True
    db.session.commit()

    kwargs = profile(app, app.test_client())

    assert titles(kwargs) == ['subject books', 'subject microblog']
    assert kwargs['post_replies'].items == []
    assert overview(kwargs) == ['subject books', 'subject microblog']
    assert (kwargs['profile_post_count'], kwargs['profile_comment_count']) == (2, 0)


def test_comments_are_shown_when_opted_in(app, env):
    env.subject.profile_comments = True
    db.session.commit()

    kwargs = profile(app, app.test_client())

    assert titles(kwargs) == ['subject microblog']
    assert [reply.body for reply in kwargs['post_replies'].items] == ['subject comment']
    assert overview(kwargs) == ['subject comment', 'subject microblog']
    assert kwargs['shows_comments'] is True
    assert (kwargs['profile_post_count'], kwargs['profile_comment_count']) == (1, 1)


def test_with_both_opted_in_the_stored_counts_are_shown(app, env):
    env.subject.profile_community_posts = env.subject.profile_comments = True
    env.subject.post_count = 5
    db.session.commit()

    kwargs = profile(app, app.test_client())

    assert (kwargs['profile_post_count'], kwargs['profile_comment_count']) == (5, 1)


@pytest.mark.parametrize('who', ['subject', 'staff'])
def test_the_person_and_their_staff_see_everything(app, env, who):
    kwargs = profile(app, as_user(app, getattr(env, who)))

    assert titles(kwargs) == ['subject books', 'subject microblog']
    assert [reply.body for reply in kwargs['post_replies'].items] == ['subject comment']
    assert overview(kwargs) == ['subject books', 'subject comment', 'subject microblog']
    assert kwargs['shows_comments'] is True
    assert [user.id for user in kwargs['followers']] == [env.follower.id]


def test_a_remote_profile_is_unchanged(app, env):
    kwargs = profile(app, app.test_client(), actor='remote@peer.example')

    assert titles(kwargs) == ['remote books', 'remote microblog']
    assert [reply.body for reply in kwargs['post_replies'].items] == ['remote comment']


@pytest.mark.parametrize('setting, listed, count', [('list', True, None), ('count', False, 1), ('none', False, None)])
def test_the_followers(app, env, setting, listed, count):
    env.subject.profile_followers = setting
    db.session.commit()

    kwargs = profile(app, app.test_client())

    assert [user.id for user in kwargs['followers']] == ([env.follower.id] if listed else [])
    assert kwargs['followers_count'] == count


def test_the_page_leaves_out_what_is_hidden(app, env):
    """Rendered for real: no Comments tab or count, and the followers line as the setting says."""
    env.subject.profile_followers = 'count'
    db.session.commit()

    page = app.test_client().get('/u/subject').get_data(as_text=True)

    assert 'subject microblog' in page
    assert 'subject books' not in page and 'subject comment' not in page
    assert 'comments-pill' not in page and 'comments_tab' not in page
    assert 'followers-pill' not in page
    assert 'Followers: 1' in page and 'Posts: 1' in page and 'Comments:' not in page


def test_the_page_shows_what_is_opted_in(app, env):
    env.subject.profile_comments = True
    db.session.commit()

    page = app.test_client().get('/u/subject').get_data(as_text=True)

    assert 'comments-pill' in page and 'subject comment' in page and 'Comments: 1' in page
    assert 'followers-pill' in page and 'Followers: 1' not in page


# --------------------------------------------------------------------------
# RSS
# --------------------------------------------------------------------------


def _feed(app):
    from app import cache
    cache.clear()   # the route is cached for ten minutes
    response = app.test_client().get('/u/subject/feed')
    assert response.status_code == 200
    return response.get_data(as_text=True)


def test_the_feed_carries_microblog_posts_by_default(app, env):
    feed = _feed(app)
    assert 'subject microblog' in feed and 'subject books' not in feed


def test_the_feed_carries_community_posts_when_opted_in(app, env):
    env.subject.profile_community_posts = True
    db.session.commit()

    feed = _feed(app)

    assert 'subject microblog' in feed and 'subject books' in feed


# --------------------------------------------------------------------------
# The alpha API
# --------------------------------------------------------------------------


def auth(user):
    return {'Authorization': f'Bearer {user.encode_jwt_token()}'}


@pytest.fixture
def api(app, env, monkeypatch):
    monkeypatch.setitem(current_app.config, 'ENABLE_ALPHA_API', 'true')
    site = db.session.get(Site, 1)
    g.site = site
    undetermined = Language.query.filter_by(code='und').first()
    if undetermined is None:
        undetermined = Language(code='und', name='Undetermined')
        db.session.add(undetermined)
        db.session.flush()
    site.language_id = undetermined.id
    for user in (env.viewer, env.subject):
        user.private_key, user.public_key = a_keypair()
    db.session.commit()
    return app.test_client()


def api_titles(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    return sorted(post['post']['title'] for post in response.get_json()['posts'])


def api_comments(response):
    assert response.status_code == 200, response.get_data(as_text=True)
    return [comment['comment']['body'] for comment in response.get_json()['comments']]


@pytest.mark.parametrize('endpoint', ['list', 'list2'])
def test_the_api_lists_a_persons_microblog_posts_by_default(app, env, api, endpoint):
    response = api.get(f'/api/alpha/post/{endpoint}?person_id={env.subject.id}', headers=auth(env.viewer))
    assert api_titles(response) == ['subject microblog']


@pytest.mark.parametrize('endpoint', ['list', 'list2'])
def test_the_api_lists_community_posts_when_opted_in(app, env, api, endpoint):
    env.subject.profile_community_posts = True
    db.session.commit()

    response = api.get(f'/api/alpha/post/{endpoint}?person_id={env.subject.id}')

    assert api_titles(response) == ['subject books', 'subject microblog']


def test_the_api_lists_everything_to_the_person(app, env, api):
    response = api.get(f'/api/alpha/post/list?person_id={env.subject.id}', headers=auth(env.subject))
    assert api_titles(response) == ['subject books', 'subject microblog']
    response = api.get(f'/api/alpha/comment/list?person_id={env.subject.id}', headers=auth(env.subject))
    assert api_comments(response) == ['subject comment']


def test_a_post_listing_narrowed_otherwise_still_hides_the_persons_community_posts(app, env, api):
    """The filter is on the author alone, so it holds when a community narrows the listing first."""
    response = api.get(f'/api/alpha/post/list?person_id={env.subject.id}&community_id={env.books.id}')
    assert 'subject books' not in api_titles(response)


def test_the_raw_sql_path_hides_them_too(app, env, api):
    """A scope that keeps the raw-SQL fast path (Local) and a person: the filter has an SQL twin."""
    response = api.get(f'/api/alpha/post/list?type_=Local&person_id={env.subject.id}')
    assert 'subject books' not in api_titles(response)


def test_a_listing_of_no_person_is_not_a_profile(app, env, api):
    """The settings govern a person's profile, not the communities their posts are in."""
    response = api.get(f'/api/alpha/post/list?community_id={env.books.id}')
    assert api_titles(response) == ['remote books', 'subject books']


def test_an_unknown_person_lists_as_before(app, env, api):
    response = api.get('/api/alpha/post/list?person_id=999999')
    assert api_titles(response) == []


def test_the_api_hides_comments_by_default(app, env, api):
    response = api.get(f'/api/alpha/comment/list?person_id={env.subject.id}')
    assert api_comments(response) == []


def test_the_api_lists_comments_when_opted_in(app, env, api):
    env.subject.profile_comments = True
    db.session.commit()

    response = api.get(f'/api/alpha/comment/list?person_id={env.subject.id}')

    assert api_comments(response) == ['subject comment']


def test_a_comment_listing_by_an_unknown_person_lists_as_before(app, env, api):
    assert api_comments(api.get('/api/alpha/comment/list?person_id=999999')) == []


def test_the_api_person_counts_and_content(app, env, api):
    env.subject.profile_followers = 'none'
    db.session.commit()

    response = api.get(f'/api/alpha/user?person_id={env.subject.id}&include_content=true', headers=auth(env.viewer))

    assert response.status_code == 200, response.get_data(as_text=True)
    body = response.get_json()
    assert body['person_view']['counts']['post_count'] == 1
    assert body['person_view']['counts']['comment_count'] == 0
    assert body['person_view']['counts']['follower_count'] == 0
    assert [post['post']['title'] for post in body['posts']] == ['subject microblog']
    assert body['comments'] == []


# --------------------------------------------------------------------------
# Counts, the Mastodon account and ActivityPub
# --------------------------------------------------------------------------


def test_the_counts_follow_the_settings(app, env):
    from app.reads.users import listable_counts
    remote, subject = env.remote, env.subject
    assert listable_counts([subject, remote], None) == {subject.id: (1, 0), remote.id: (2, 1)}
    subject.profile_community_posts = subject.profile_comments = True
    db.session.commit()
    assert listable_counts([subject], None) == {subject.id: (2, 1)}


def test_staff_who_may_view_user_details_get_every_count(app, env):
    from app.reads.users import listable_counts
    with patch('app.profile_privacy.user_access', side_effect=lambda permission, *_a, **_k:
               permission == 'view user details'):
        assert listable_counts([env.subject], env.viewer.id) == {env.subject.id: (2, 1)}


@pytest.mark.parametrize('setting, count, hidden', [('list', 1, False), ('count', 1, True), ('none', 0, True)])
def test_the_mastodon_account(app, env, setting, count, hidden):
    from app.mastodon.entities import account
    env.subject.profile_followers = setting
    db.session.commit()

    with app.test_request_context('/'):
        entity = account(env.subject, None)
        own = account(env.subject, env.subject.id, with_source=True)

    assert (entity['followers_count'], entity['hide_collections']) == (count, hidden)
    assert entity['statuses_count'] == 1
    assert (own['followers_count'], own['statuses_count']) == (1, 3)
    assert own['hide_collections'] is hidden and own['source']['hide_collections'] is hidden


@pytest.mark.parametrize('setting, total', [('list', 1), ('count', 1), ('none', None)])
def test_the_activitypub_followers_collection(app, env, setting, total):
    env.subject.profile_followers = setting
    db.session.commit()

    collection = ap_get(app, '/u/subject/followers').json

    assert collection.get('totalItems') == total
    assert collection['orderedItems'] == []


# --------------------------------------------------------------------------
# The settings page
# --------------------------------------------------------------------------


def test_the_settings_open_on_the_stored_values_and_save(app, env):
    client = as_user(app, env.subject)
    env.subject.profile_followers = 'count'
    db.session.commit()

    with patch('app.user.routes.render_template', return_value='rendered') as render:
        client.get('/user/settings')
        form = render.call_args.kwargs['form']
        assert (form.profile_community_posts.data, form.profile_comments.data, form.profile_followers.data) == \
            (False, False, 'count')

        token = csrf(app, client)
        response = client.post('/user/settings', data=settings_payload(
            token, profile_community_posts='y', profile_comments='y', profile_followers='none'))

    assert response.status_code == 302
    db.session.refresh(env.subject)
    assert (env.subject.profile_community_posts, env.subject.profile_comments, env.subject.profile_followers) == \
        (True, True, 'none')


def test_the_settings_refuse_an_unknown_followers_choice(app, env):
    client = as_user(app, env.subject)
    token = csrf(app, client)

    with patch('app.user.routes.render_template', return_value='rendered'):
        response = client.post('/user/settings', data=settings_payload(token, profile_followers='everyone'))

    assert response.status_code == 200   # the form again, unsaved
    db.session.refresh(env.subject)
    assert env.subject.profile_followers == 'list'


def test_the_settings_page_renders_the_fields(app, env, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the template renders the form's token
    page = as_user(app, env.subject).get('/user/settings').get_data(as_text=True)
    for field in ('profile_community_posts', 'profile_comments', 'profile_followers'):
        assert f'name="{field}"' in page


# --------------------------------------------------------------------------
# The migration
# --------------------------------------------------------------------------


def _migration():
    spec = importlib.util.spec_from_file_location('user_profile_privacy_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_chain():
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ('f2b7c9d4e1a6', 'e7c3a9d5b214')


def test_the_columns_are_required_with_their_defaults(app, db_session):
    with db.engine.connect() as connection:
        columns = {found['name']: found for found in inspect(connection).get_columns('user')}
    for name, default in (('profile_community_posts', 'false'), ('profile_comments', 'false'),
                          ('profile_followers', "'list'")):
        assert columns[name]['nullable'] is False
        assert default in str(columns[name]['default'])


@pytest.mark.serial   # it alters the user table every worker reads (run_tests.sh runs these alone)
def test_the_migration_reverses_and_reapplies(app, db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    migration = _migration()
    instance_id = make_instance('ppmig.example').id   # committed: the migration's own connection sees it
    names = {'profile_community_posts', 'profile_comments', 'profile_followers'}

    def columns(connection):
        return {found['name'] for found in inspect(connection).get_columns('user')}

    with db.engine.connect() as connection:
        connection.execute(text("INSERT INTO \"user\" (user_name, email, instance_id) VALUES ('ppmig', 'ppmig@example.com', :instance)"),
                           {'instance': instance_id})
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert not names & columns(connection)
            migration.upgrade()
        assert names <= columns(connection)
        row = connection.execute(text("SELECT profile_community_posts, profile_comments, profile_followers "
                                      "FROM \"user\" WHERE user_name = 'ppmig'")).one()
        assert tuple(row) == (False, False, 'list')   # a row from before the columns reads the defaults
        connection.rollback()
