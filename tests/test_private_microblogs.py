"""Private microblog posts (spec docs/superpowers/specs/2026-11-22-private-microblogs-design.md).

A user's setting, `microblogs_private`, decides whether their new microblog posts are public and federated or
private: stamped local_only, never federated, seen only by signed-in users here. The web compose form, the alpha
API and the Mastodon API choose per post; saying nothing means the setting. It is fixed at creation.
"""
# ruff: noqa: F811 -- the fixtures imported below are named again as test parameters, which is how pytest uses them
import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import inspect

from app import db
from app.constants import COMMUNITY_KIND_MICROBLOGS, POST_TYPE_ARTICLE, SRC_API, SRC_WEB
from app.local_only import outbound_refusal
from app.models import Language, Post, User
from app.shared.post import make_post, requested_local_only
from app.visibility import can_view
from tests.factories import make_community, make_instance, make_user
from tests.test_main_add_post import env, poster, quick_post  # noqa: F401 -- fixtures
from tests.test_mastodon_compose import _made, _send, world  # noqa: F401 -- fixture
from tests.test_s7_followers_only_create import _api_seed, api_client, two_communities, web  # noqa: F401 -- fixtures
from tests.test_community_add_post import _post_payload, url
from tests.test_shared_post_edit import _api_input
from tests.test_user_settings import as_user, instance, save_settings
from tests.factories import bearer

MIGRATION = Path(__file__).resolve().parent.parent / 'migrations' / 'versions' / '2daa520cdaa7_user_microblogs_private.py'


def _form(value=None, submitted=True):
    """A web form's `private_microblog` select: `raw_data` is empty when the field was not submitted."""
    return SimpleNamespace(private_microblog=SimpleNamespace(data=value or 'public',
                                                             raw_data=[value] if submitted else []))


# --------------------------------------------------------------------------
# requested_local_only: the one rule every producer shares
# --------------------------------------------------------------------------


@pytest.fixture
def author(two_communities):
    return User.query.filter_by(user_name='founder').one()


@pytest.mark.parametrize('setting', [False, True])
@pytest.mark.parametrize('src, input_, expected', [
    (SRC_API, {'local_only': True}, True),
    (SRC_API, {'local_only': False}, False),
    (SRC_WEB, _form('private'), True),
    (SRC_WEB, _form('public'), False),
], ids=['api-true', 'api-false', 'web-private', 'web-public'])
def test_in_microblogs_a_choice_wins_over_the_setting(two_communities, author, setting, src, input_, expected):
    author.microblogs_private = setting
    assert requested_local_only(input_, src, author, two_communities.microblogs) is expected


@pytest.mark.parametrize('setting', [False, True])
@pytest.mark.parametrize('src, input_', [
    (SRC_API, {}),
    (SRC_API, {'local_only': None}),
    (SRC_WEB, SimpleNamespace()),                          # a form object without the field
    (SRC_WEB, _form('public', submitted=False)),           # the field, not submitted (the form opened elsewhere)
], ids=['api-absent', 'api-null', 'web-no-field', 'web-not-submitted'])
def test_in_microblogs_no_choice_is_the_setting(two_communities, author, setting, src, input_):
    author.microblogs_private = setting
    assert requested_local_only(input_, src, author, two_communities.microblogs) is setting


@pytest.mark.parametrize('src, input_, expected', [
    (SRC_API, {'local_only': True}, True),                 # PeerTube's Internal: only more private
    (SRC_API, {'local_only': False}, False),
    (SRC_API, {}, False),
    (SRC_WEB, _form('private'), False),                    # the web field is a microblogs choice only
    (SRC_WEB, SimpleNamespace(), False),
], ids=['api-true', 'api-false', 'api-absent', 'web-private', 'web-no-field'])
def test_elsewhere_the_setting_does_not_apply(two_communities, author, src, input_, expected):
    author.microblogs_private = True
    assert requested_local_only(input_, src, author, two_communities.general) is expected


@pytest.mark.parametrize('community', ['microblogs', 'general'])
def test_a_local_only_author_is_always_private(two_communities, author, community):
    with patch('app.shared.post.content_local_only', return_value=True):
        assert requested_local_only({'local_only': False}, SRC_API, author, getattr(two_communities, community)) \
            is True
        assert requested_local_only(_form('public'), SRC_WEB, author, getattr(two_communities, community)) is True


# --------------------------------------------------------------------------
# The column and its migration
# --------------------------------------------------------------------------


def _migration():
    spec = importlib.util.spec_from_file_location('user_microblogs_private_migration', MIGRATION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_revision_chain():
    migration = _migration()
    assert (migration.revision, migration.down_revision) == ('2daa520cdaa7', '9c1e42dfcd53')


def test_the_column_is_required_and_defaults_to_public(app, db_session):
    with db.engine.connect() as connection:
        column = next(found for found in inspect(connection).get_columns('user')
                      if found['name'] == 'microblogs_private')
    assert column['nullable'] is False
    assert 'false' in str(column['default']).lower()
    assert make_user(make_instance('cols.example'), 'fresh', local=True).microblogs_private is False


@pytest.mark.serial   # it alters the user table every worker reads (run_tests.sh runs these alone)
def test_the_migration_reverses_and_reapplies(app, db_session):
    from alembic.migration import MigrationContext
    from alembic.operations import Operations
    from sqlalchemy import text

    migration = _migration()
    instance_id = make_instance('mpmig.example').id   # committed: the migration's own connection sees it

    def columns(connection):
        return {found['name'] for found in inspect(connection).get_columns('user')}

    with db.engine.connect() as connection:
        connection.execute(text("INSERT INTO \"user\" (user_name, email, instance_id) VALUES ('mpmig', "
                                "'mpmig@example.com', :instance)"), {'instance': instance_id})
        with Operations.context(MigrationContext.configure(connection)):
            migration.downgrade()
            assert 'microblogs_private' not in columns(connection)
            migration.upgrade()
        assert 'microblogs_private' in columns(connection)
        assert connection.execute(text("SELECT microblogs_private FROM \"user\" WHERE user_name = 'mpmig'")) \
            .scalar() is False   # a row from before the column is public
        connection.rollback()


# --------------------------------------------------------------------------
# make_post stores it; a private post is not federated and not public
# --------------------------------------------------------------------------


@pytest.mark.parametrize('setting, given, expected', [
    (False, {}, False), (True, {}, True), (True, {'local_only': False}, False), (False, {'local_only': True}, True),
])
def test_make_post_stamps_microblog_posts(db_session, monkeypatch, setting, given, expected):
    s = _api_seed(monkeypatch, 'microblogs')
    s.author.microblogs_private = setting
    db.session.commit()
    _, post = make_post(_api_input(**given), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    assert db.session.get(Post, post.id).local_only is expected


def test_make_post_leaves_community_posts_public_whatever_the_setting(db_session, monkeypatch):
    s = _api_seed(monkeypatch, 'general')
    s.author.microblogs_private = True
    db.session.commit()
    _, post = make_post(_api_input(), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    assert db.session.get(Post, post.id).local_only is False


def test_a_private_post_never_leaves_and_is_hidden_from_logged_out_readers(db_session, monkeypatch):
    s = _api_seed(monkeypatch, 'microblogs')
    _, private = make_post(_api_input(local_only=True), s.community, POST_TYPE_ARTICLE, SRC_API, auth='t')
    _, public = make_post(_api_input(local_only=False, title='open'), s.community, POST_TYPE_ARTICLE, SRC_API,
                          auth='t')
    private, public = db.session.get(Post, private.id), db.session.get(Post, public.id)

    create = {'type': 'Create', 'actor': s.author.public_url(), 'object': {'id': private.ap_id}}
    announce = {'type': 'Announce', 'object': private.public_url()}
    assert outbound_refusal(create) == 'it names a local-only post'
    assert outbound_refusal(announce) == 'it names a local-only post'
    assert outbound_refusal({'type': 'Create', 'object': {'id': public.ap_id}}) is None

    assert can_view(private, None) is False
    assert can_view(private, s.author.id) is True
    assert can_view(public, None) is True


# --------------------------------------------------------------------------
# The alpha API
# --------------------------------------------------------------------------


@pytest.mark.parametrize('setting, extra, expected', [
    (True, {}, True), (False, {}, False), (True, {'local_only': False}, False), (False, {'local_only': True}, True),
], ids=['setting-private', 'setting-public', 'override-public', 'override-private'])
def test_the_alpha_api_chooses_per_post(api_client, setting, extra, expected):
    a = api_client
    a.author.microblogs_private = setting
    db.session.commit()
    response = a.client.post('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                             json={'title': 'via the api', 'community_id': a.microblogs.id, **extra})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert Post.query.filter_by(title='via the api').one().local_only is expected


def test_the_alpha_api_refuses_a_non_boolean(api_client):
    a = api_client
    response = a.client.post('/api/alpha/post', headers={'Authorization': bearer(a.author)},
                             json={'title': 'nope', 'community_id': a.microblogs.id, 'local_only': 'maybe'})
    assert response.status_code == 400
    assert Post.query.filter_by(title='nope').first() is None


# --------------------------------------------------------------------------
# The Mastodon API
# --------------------------------------------------------------------------


@pytest.mark.parametrize('setting, extra, expected', [
    (True, {}, True), (False, {}, False), (True, {'local_only': False}, False), (False, {'local_only': True}, True),
    (False, {'local_only': 'true'}, True), (True, {'local_only': None}, True),
], ids=['setting-private', 'setting-public', 'override-public', 'override-private', 'form-string', 'null'])
def test_a_mastodon_status_chooses_per_post(world, setting, extra, expected):
    world['me'].microblogs_private = setting
    db.session.commit()
    row = _made(_send(world, json={'status': 'Hello there.', **extra}))
    assert row.community_id == world['microblogs'].id
    assert row.local_only is expected


def test_a_mastodon_status_in_a_community_ignores_the_setting(world):
    world['me'].microblogs_private = True
    db.session.commit()
    row = _made(_send(world, json={'status': 'Tips? @cooking@test.piefed.local'}))
    assert row.community_id == world['cooking'].id and row.local_only is False


# --------------------------------------------------------------------------
# The web compose form, the quick post box and the edit form
# --------------------------------------------------------------------------


@pytest.mark.parametrize('setting, selected', [(False, 'public'), (True, 'private')])
def test_the_form_offers_privacy_in_microblogs_preset_from_the_setting(app, web, setting, selected):
    web.author.microblogs_private = setting
    db.session.commit()
    body = web.client.get(url(app, 'community.add_post', actor='microblogs', type='discussion')).get_data(as_text=True)
    assert 'name="private_microblog"' in body
    assert f'<option selected value="{selected}">' in body


def test_the_form_does_not_offer_privacy_elsewhere(app, web):
    body = web.client.get(url(app, 'community.add_post', actor='general', type='discussion')).get_data(as_text=True)
    assert 'name="private_microblog"' not in body


def _web_post(app, web, title, community=None, **fields):
    with patch('app.community.routes.render_template', return_value='rendered'):
        response = web.client.post(url(app, 'community.add_post', actor='microblogs', type='discussion'),
                                   data=_post_payload(web.token, web.english, community or web.microblogs,
                                                      title=title, **fields),
                                   content_type='multipart/form-data')
    assert response.status_code == 302
    return Post.query.filter_by(title=title).one()


@pytest.mark.parametrize('setting', [False, True])
@pytest.mark.parametrize('choice, expected', [('private', True), ('public', False)])
def test_a_web_choice_is_stored(app, web, setting, choice, expected):
    web.author.microblogs_private = setting
    db.session.commit()
    assert _web_post(app, web, 'Chosen', private_microblog=choice).local_only is expected


@pytest.mark.parametrize('setting', [False, True])
def test_a_web_post_that_did_not_send_the_field_takes_the_setting(app, web, setting):
    web.author.microblogs_private = setting
    db.session.commit()
    assert _web_post(app, web, 'Unsaid').local_only is setting


def test_a_web_post_elsewhere_stays_public(app, web):
    web.author.microblogs_private = True
    db.session.commit()
    assert _web_post(app, web, 'Community post', community=web.general, private_microblog='private').local_only \
        is False


def test_an_unknown_web_choice_is_refused(app, web):
    with patch('app.community.routes.render_template', return_value='rendered'):
        web.client.post(url(app, 'community.add_post', actor='microblogs', type='discussion'),
                        data=_post_payload(web.token, web.english, web.microblogs, title='Odd',
                                           private_microblog='secret'),
                        content_type='multipart/form-data')
    assert Post.query.filter_by(title='Odd').first() is None


@pytest.mark.parametrize('setting', [False, True])
def test_a_quick_post_takes_the_setting(app, site, poster, setting):   # `site` before `poster`, as its module's mark
    client, _joined, microblogs = poster
    alice = User.query.filter_by(user_name='alice').one()
    alice.microblogs_private = setting
    db.session.commit()
    assert quick_post(app, client, 'Quick and quiet.').status_code == 302
    post = Post.query.filter_by(user_id=alice.id).one()
    assert post.community_id == microblogs.id and post.local_only is setting


@pytest.mark.parametrize('choice, shown', [('private', 'Public, private: this server only'), ('public', 'Public')])
def test_the_edit_form_shows_it_and_does_not_offer_it(app, web, choice, shown):
    post = _web_post(app, web, 'Edit me', private_microblog=choice)
    with patch('app.post.routes.render_template', return_value='rendered') as render:
        web.client.get(url(app, 'post.post_edit', post_id=post.id))
    kwargs = render.call_args.kwargs
    assert 'private_microblog' not in kwargs['form']
    assert kwargs['visibility_text'] == shown


def test_an_edit_keeps_it(app, web):
    post = _web_post(app, web, 'Keep me', private_microblog='private')
    with patch('app.post.routes.render_template', return_value='rendered'):
        web.client.post(url(app, 'post.post_edit', post_id=post.id),
                        data=_post_payload(web.token, web.english, web.microblogs, title='Kept',
                                           private_microblog='public'),
                        content_type='multipart/form-data')
    edited = db.session.get(Post, post.id)
    assert edited.title == 'Kept'   # the edit was saved
    assert edited.local_only is True


# --------------------------------------------------------------------------
# Cross-posting never republishes a private post where it would federate
# --------------------------------------------------------------------------


def test_a_private_post_is_not_prefilled_into_a_federating_community(app, web):
    post = _web_post(app, web, 'Quiet thoughts', private_microblog='private')
    response = web.client.get(url(app, 'community.add_post', actor='general', type='link', source=post.id))
    assert response.status_code == 404
    assert 'Quiet thoughts' not in response.get_data(as_text=True)


def test_a_private_post_is_prefilled_where_the_copy_stays_here(app, web):
    post = _web_post(app, web, 'Quiet thoughts', private_microblog='private')
    with patch('app.community.routes.content_local_only', return_value=True):
        response = web.client.get(url(app, 'community.add_post', actor='general', type='link', source=post.id))
    assert response.status_code == 200
    assert 'Quiet thoughts' in response.get_data(as_text=True)


def test_a_public_post_is_still_prefilled(app, web):
    post = _web_post(app, web, 'Loud thoughts', private_microblog='public')
    response = web.client.get(url(app, 'community.add_post', actor='general', type='link', source=post.id))
    assert response.status_code == 200


# --------------------------------------------------------------------------
# The settings page
# --------------------------------------------------------------------------


@pytest.fixture
def settings_env(app, db_session, site):
    """tests/test_user_settings.py's `env`, which shares its name with tests/test_main_add_post.py's."""
    site.private_instance = False
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    viewer = make_user(local, 'viewer', local=True)
    db.session.add(Language(code='en', name='English'))
    db.session.commit()
    return as_user(app, viewer), viewer, None


@pytest.mark.parametrize('stored, shown', [(False, 'public'), (True, 'private')])
def test_the_settings_form_opens_on_the_stored_value(app, settings_env, stored, shown):
    client, viewer, _other = settings_env
    viewer.microblogs_private = stored
    db.session.commit()
    with patch('app.user.routes.render_template', return_value='rendered') as render:
        client.get('/user/settings')
    assert render.call_args.kwargs['form'].microblog_privacy.data == shown


@pytest.mark.parametrize('value, stored', [('private', True), ('public', False)])
def test_saving_the_setting(app, settings_env, value, stored):
    client, viewer, _other = settings_env
    viewer.microblogs_private = not stored
    db.session.commit()
    response, _render = save_settings(app, client, microblog_privacy=value)
    assert response.status_code == 302
    db.session.refresh(viewer)
    assert viewer.microblogs_private is stored


def test_the_settings_form_refuses_another_value(app, settings_env):
    client, viewer, _other = settings_env
    response, _render = save_settings(app, client, microblog_privacy='secret')
    assert response.status_code != 302
    db.session.refresh(viewer)
    assert viewer.microblogs_private is False


def test_the_settings_page_renders_it(app, settings_env, monkeypatch):
    monkeypatch.setitem(app.config, 'WTF_CSRF_ENABLED', True)   # the template calls form.csrf_token()
    client, _viewer, _other = settings_env
    body = client.get('/user/settings').get_data(as_text=True)
    assert 'name="microblog_privacy"' in body
    assert 'Private: this server only, not federated' in body


def test_a_microblogs_community_kind_is_what_counts(db_session):
    """A remote community called microblogs is not the local one: the setting does not apply there."""
    founder = make_user(make_instance('local.example', software='piefed'), 'founder', local=True)
    founder.microblogs_private = True
    remote = make_community('microblogs', host='peer.example', kind=COMMUNITY_KIND_MICROBLOGS)
    remote.ap_id = 'microblogs@peer.example'
    db.session.commit()
    assert requested_local_only({}, SRC_API, founder, remote) is False
