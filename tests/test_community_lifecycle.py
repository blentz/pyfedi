"""The community lifecycle: create, edit, move, delete.

Sub-project 80, slice C. Two defects:

* `AddCommunityForm.validate` checked one string for uniqueness and
  `add_local` stored another, because the route slugified AFTER validation --
  so a name the form approved could collide on INSERT and answer 500 (D980);
* `community_edit`'s POST branch ended with `else:`, so a refused submission was
  overwritten from the database (D981) -- D907's shape for the fourth time in
  this campaign.

Two environment facts cost time here and are worth stating at the top:
`approval_required` (app/utils.py:1892) redirects to `/auth/please_wait` unless
the account has a `private_key`, because the `site` fixture leaves
`registration_mode` at 'Closed'; and `add_local` is behind
`site.community_creation_admin_only`, which the fixture leaves True.
"""
import io
from unittest.mock import patch

import pytest
from slugify import slugify

from app import db
from app.models import Community, CommunityMember, File, Language, Site, User
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    from app.models import Instance

    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def able_to_create(user):
    """Clear every gate stacked in front of `add_local`.

    The route carries four decorators beyond `login_required`, and each sends
    the caller somewhere different:

    * `validation_required` -> /auth/validation_required unless `verified`;
    * `approval_required` -> /auth/please_wait unless the account has a
      `private_key`, while `registration_mode` is 'RequireApplication' or
      'Closed' -- and the `site` fixture leaves it 'Closed';
    * `aged_account_required` -> /auth/not_trustworthy unless the account is in
      `g.admin_ids` or is not `created_very_recently()`;
    * the route's own `site.community_creation_admin_only` check.

    A row that misses any of them gets a 302 that looks like the refusal it was
    testing for.
    """
    from datetime import timedelta

    from app.models import utcnow

    user.verified = True
    user.private_key = 'a private key'
    user.created = utcnow() - timedelta(days=30)
    db.session.commit()
    assert not user.created_very_recently()
    return user


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


def url(app, endpoint, **values):
    from flask import url_for

    with app.test_request_context():
        return url_for(endpoint, **values)


@pytest.fixture
def creator(app, db_session):
    """An account that can actually reach `add_local`.

    Two gates stand in front of it and neither is obvious from the route:
    `approval_required` sends an account with no `private_key` to
    `/auth/please_wait` while `registration_mode` is 'Closed' (which the `site`
    fixture leaves it at), and `site.community_creation_admin_only` is True by
    default. User 1 clears the second by being an admin (fact 347).
    """
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    able_to_create(founder)
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.add(Language(code='en', name='English'))
    site = db.session.get(Site, 1)
    site.community_creation_admin_only = False
    db.session.commit()

    client = app.test_client()
    login(client, founder)
    return client, csrf(app, client), founder


def _add_payload(token, **overrides):
    data = {'community_name': 'A New Community', 'url': 'brandnew',
            'description': '', 'posting_warning': '', 'theme': 'disabled',
            'invitations': '0', 'submit': 'Create', 'csrf_token': token,
            'icon_file': (io.BytesIO(b''), ''),
            'banner_file': (io.BytesIO(b''), '')}
    data.update(overrides)
    return data


def _add(app, client, token, **overrides):
    """Multipart (fact 362) with render_template patched (fact 363).

    `languages` is left out rather than passed as `[]`: werkzeug's multipart
    builder produces a body the server cannot parse from an empty list, and the
    request then arrives with no form fields at all -- which since D958 is a
    400 for the missing CSRF token, not the validation failure it looks like.
    """
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        with patch('app.community.routes.task_selector'):
            response = client.post(url(app, 'community.add_local'),
                                   data=_add_payload(token, **overrides),
                                   content_type='multipart/form-data')
    return response, render


# --------------------------------------------------------------------------
# D980: the validator and the route must agree about the name
# --------------------------------------------------------------------------


@pytest.mark.parametrize('typed, stored', [
    ('__general__', 'general'),
    ('test__name', 'test_name'),
    ('Test_Name', 'test_name'),
    ('plain', 'plain'),
])
def test_slugify_changes_strings_the_validator_accepts(typed, stored):
    """The premise, asserted directly so the rows below cannot drift from it.

    Every one of these passes `AddCommunityForm`'s regex -- `^[a-zA-Z0-9_]+$`
    -- and only the last is a fixed point of `slugify(..., separator='_')`.
    That gap is D980: the form checked uniqueness against the left column and
    the route stored the right one.
    """
    assert slugify(typed.strip(), separator='_').lower() == stored


def test_a_name_that_slugifies_onto_an_existing_one_is_refused(app, creator):
    """D980's pin, inverted.

    `general` exists; `__general__` passes the regex, and the uniqueness check
    was asked about `__general__`, which is not taken. The route then slugified
    it to `general` and the INSERT died:

        psycopg2.errors.UniqueViolation: duplicate key value violates unique
        constraint "ix_community_ap_profile_id"
        DETAIL:  Key (ap_profile_id)=(https://test.piefed.local/c/general) already exists.

    An unhandled IntegrityError is a 500 on community creation, reachable by
    typing four extra underscores. The form now normalises first, so the
    duplicate is reported as a field error.
    """
    client, token, founder = creator
    make_community('general')
    db.session.commit()

    response, render = _add(app, client, token, url='__general__')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].url.errors == [
        'A community with this url already exists.']
    assert Community.query.count() == 1


def test_a_name_that_slugifies_to_nothing_is_refused(app, creator):
    """`___` slugifies to the empty string, which would be stored as a
    community named `''` with `ap_profile_id` ending in `/c/` -- a row nothing
    can route to. The emptiness check now runs on the normalised value, not
    the typed one."""
    client, token, founder = creator

    response, render = _add(app, client, token, url='___')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].url.errors == ['Url is required.']
    assert Community.query.count() == 0


def test_a_name_is_stored_exactly_as_the_validator_approved_it(app, creator):
    """The other half: normalising in the validator must not stop a legitimate
    name being created, and the stored name must be the normalised one rather
    than what was typed."""
    client, token, founder = creator

    response, _render = _add(app, client, token, url='Test__Name',
                             community_name='Test Name')

    assert response.status_code == 302
    community = Community.query.one()
    assert community.name == 'test_name'
    assert community.ap_profile_id.endswith('/c/test_name')
    assert community.title == 'Test Name'


def test_a_url_typed_with_a_c_prefix_is_accepted(app, creator):
    """`if form.url.data.strip().lower().startswith('/c/')` -- people paste the
    path rather than the name."""
    client, token, founder = creator

    response, _render = _add(app, client, token, url='/c/pasted')

    assert response.status_code == 302
    assert Community.query.one().name == 'pasted'


@pytest.mark.parametrize('typed, message', [
    ('has-a-hyphen', '- cannot be in Url. Use _ instead?'),
    ('has a space', 'Community urls can only contain letters, numbers, and underscores.'),
    ('', 'Url is required.'),
])
def test_the_url_rules_are_reported_as_field_errors(app, creator, typed,
                                                    message):
    """The three checks that run before normalisation. Each has its own
    message, so each is asserted by message rather than by "there was an
    error"."""
    client, token, founder = creator

    response, render = _add(app, client, token, url=typed)

    assert response.status_code == 200
    assert str(render.call_args.kwargs['form'].url.errors[0]) == message


def test_a_name_taken_by_a_user_is_refused(app, creator):
    """A community and a local user share a namespace -- `/u/name` and
    `/c/name` are both actors on this instance."""
    client, token, founder = creator
    make_user(instance(), 'takenname', local=True)
    db.session.commit()

    response, render = _add(app, client, token, url='takenname')

    assert response.status_code == 200
    assert str(render.call_args.kwargs['form'].url.errors[0]) == \
        'This name is in use already.'


def test_a_name_taken_by_a_deleted_user_says_so(app, creator):
    """The `if user.deleted:` arm -- a different message, because the name is
    permanently spent rather than currently occupied."""
    client, token, founder = creator
    gone = make_user(instance(), 'goneuser', local=True)
    gone.deleted = True
    db.session.commit()

    response, render = _add(app, client, token, url='goneuser')

    assert str(render.call_args.kwargs['form'].url.errors[0]) == \
        'This name was used in the past and cannot be reused.'


# --------------------------------------------------------------------------
# add_local: what it creates
# --------------------------------------------------------------------------


def test_creating_a_community_makes_the_creator_its_owner(app, creator):
    """A community with no owner cannot be administered by anyone but instance
    staff, so the membership row is as much a part of creation as the
    community itself."""
    client, token, founder = creator

    _add(app, client, token, url='mine')

    community = Community.query.one()
    membership = CommunityMember.query.filter_by(community_id=community.id,
                                                 user_id=founder.id).one()
    assert (membership.is_owner, membership.is_moderator) == (True, True)


def test_a_federating_community_gets_a_keypair(app, creator):
    """`if form.local_only.data: private_key = None` -- a community that
    federates signs its activities, and one that does not has nothing to sign
    with."""
    client, token, founder = creator

    _add(app, client, token, url='federated')

    community = Community.query.one()
    assert community.private_key and community.public_key


def test_a_local_only_community_gets_no_keypair(app, creator):
    """The true arm, which also makes `invitations` meaningful -- a local-only
    community is the only kind that can require them."""
    client, token, founder = creator

    _add(app, client, token, url='localonly', local_only='y', invitations='2')

    community = Community.query.one()
    assert (community.private_key, community.public_key) == (None, None)
    assert community.local_only is True
    assert community.invitations == 2


def test_a_private_community_is_local_only_and_hidden(app, creator):
    """`if form.private.data:` forces three other settings. A private community
    that appeared in the popular or all listings would defeat its own
    purpose."""
    client, token, founder = creator

    _add(app, client, token, url='secret', private='y')

    community = Community.query.one()
    assert (community.private, community.local_only) == (True, True)
    assert (community.show_popular, community.show_all) == (False, False)


def test_a_public_community_shows_in_the_listings(app, creator):
    """The false arm, and the `invitations` reset that goes with it: a public
    community cannot require an invitation."""
    client, token, founder = creator

    _add(app, client, token, url='public', invitations='4')

    community = Community.query.one()
    assert (community.private, community.show_popular, community.show_all) == \
        (False, True, True)
    assert community.invitations == 0


def test_creation_is_refused_when_restricted_to_admins(app, db_session):
    """`site.community_creation_admin_only` -- the setting exists so an
    instance can stop arbitrary users making communities, and it is checked
    against `is_admin()`."""
    local = instance()
    make_user(local, 'founder', local=True)
    ordinary = make_user(local, 'ordinary', local=True)
    able_to_create(ordinary)
    site = db.session.get(Site, 1)
    site.community_creation_admin_only = True
    db.session.commit()
    client = app.test_client()
    login(client, ordinary)
    token = csrf(app, client)

    with patch('app.community.routes.flash') as flashed:
        response = client.get(url(app, 'community.add_local'))

    assert response.status_code == 302
    assert 'restricted to admins' in flashed.call_args.args[0]
    assert Community.query.count() == 0


def test_a_banned_user_cannot_create_a_community(app, db_session):
    local = instance()
    make_user(local, 'founder', local=True)
    banned = make_user(local, 'banned', local=True)
    able_to_create(banned)
    banned.banned = True
    site = db.session.get(Site, 1)
    site.community_creation_admin_only = False
    db.session.commit()
    client = app.test_client()
    login(client, banned)
    token = csrf(app, client)

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.task_selector'):
            client.post(url(app, 'community.add_local'),
                        data=_add_payload(token, url='spam'),
                        content_type='multipart/form-data')

    assert Community.query.count() == 0


def test_the_add_form_renders_on_a_get(app, creator):
    client, token, founder = creator

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.add_local'))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


# --------------------------------------------------------------------------
# D981: a refused edit keeps what was typed
# --------------------------------------------------------------------------


@pytest.fixture
def owned_community(app, db_session):
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    owner = make_user(local, 'owner', local=True)
    ordinary = make_user(local, 'ordinary', local=True)
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    membership = make_community_member(owner, community, is_moderator=True)
    membership.is_owner = True
    db.session.commit()
    return community, owner, ordinary, english


@pytest.fixture
def owner_client(app, owned_community):
    community, owner, ordinary, english = owned_community
    client = app.test_client()
    login(client, owner)
    return client, csrf(app, client)


def _edit_payload(token, english, **overrides):
    # The SelectField choices are fixed lists in EditCommunityForm: layouts are
    # ('', 'masonry', 'masonry_wide'), post types are lowercase ('link', not
    # 'Link'), and `topic` is fed from topics_for_form(0) -- which is empty
    # unless a Topic exists, so '0' is not a valid choice and the field has to
    # be omitted rather than sent as zero.
    data = {'title': 'General', 'description': '', 'theme': 'disabled',
            'posting_warning': '', 'invitations': '0',
            'languages': str(english.id), 'default_layout': '',
            'default_post_type': 'link', 'downvote_accept_mode': '0',
            'post_url_type': 'friendly', 'submit': 'Save',
            'csrf_token': token, 'icon_file': (io.BytesIO(b''), ''),
            'banner_file': (io.BytesIO(b''), '')}
    data.update(overrides)
    return data


def _edit(app, client, token, community, english, **overrides):
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        with patch('app.community.routes.task_selector'):
            response = client.post(url(app, 'community.community_edit', community_id=community.id),
                                   data=_edit_payload(token, english, **overrides),
                                   content_type='multipart/form-data')
    return response, render


def test_a_refused_edit_keeps_what_the_owner_typed(app, owned_community,
                                                   owner_client):
    """D981's pin, inverted.

    The POST branch ended with `else:`, so a submission the form refused fell
    into the pre-fill arm and was overwritten from the database. Measured:

        PROBE g3 errors: {'theme': [...], 'topic': [...], 'default_layout': [...]}
        PROBE g3 title redisplayed as: 'Stored title'

    D907's shape for the fourth time in this campaign: sub-project 79 slice A,
    slice C's `admin_federation`, slice F's `admin_user_edit`, and here. Every
    instance was found by covering the function, never by reading it.
    """
    community, owner, ordinary, english = owned_community
    client, token = owner_client
    community.title = 'Stored title'
    db.session.commit()

    _response, render = _edit(app, client, token, community, english,
                              title='what the owner typed',
                              default_layout='not-a-real-choice')

    form = render.call_args.kwargs['form']
    assert 'default_layout' in form.errors
    assert form.title.data == 'what the owner typed'
    db.session.expire_all()
    assert db.session.get(Community, community.id).title == 'Stored title'


def test_the_edit_form_is_prefilled_on_a_get(app, owned_community,
                                             owner_client):
    community, owner, ordinary, english = owned_community
    client, token = owner_client
    community.title = 'Stored title'
    community.posting_warning = 'be nice'
    community.nsfw = True
    community.restricted_to_mods = True
    db.session.commit()

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_edit', community_id=community.id))

    assert response.status_code == 200
    form = render.call_args.kwargs['form']
    assert form.title.data == 'Stored title'
    assert form.posting_warning.data == 'be nice'
    assert (form.nsfw.data, form.restricted_to_mods.data) == (True, True)


def test_an_edit_saves_the_settings(app, owned_community, owner_client):
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    response, _render = _edit(app, client, token, community, english,
                              title='Renamed', description='# about us',
                              posting_warning='read the rules', nsfw='y',
                              restricted_to_mods='y', new_mods_wanted='y')

    if _render.call_args:
        assert _render.call_args.kwargs['form'].errors == {}
    assert response.status_code == 302
    db.session.expire_all()
    updated = db.session.get(Community, community.id)
    assert updated.title == 'Renamed'
    assert '<h1>about us</h1>' in updated.description_html
    assert updated.posting_warning == 'read the rules'
    assert (updated.nsfw, updated.restricted_to_mods, updated.new_mods_wanted) == \
        (True, True, True)


def test_making_a_community_private_forces_local_only(app, owned_community,
                                                      owner_client):
    """The same coupling as on creation, enforced again on edit -- a community
    made private after the fact must stop federating and stop appearing in the
    listings."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    _edit(app, client, token, community, english, private='y')

    db.session.expire_all()
    updated = db.session.get(Community, community.id)
    assert (updated.private, updated.local_only) == (True, True)
    assert (updated.show_popular, updated.show_all) == (False, False)


def test_an_edit_replaces_the_language_list(app, owned_community,
                                            owner_client):
    """`DELETE FROM community_language` then re-append. The undetermined
    language is always added back, because a post with no language would
    otherwise be refused by a community that accepts every language the owner
    listed."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    _edit(app, client, token, community, english)

    db.session.expire_all()
    codes = sorted(language.code
                   for language in db.session.get(Community, community.id).languages)
    assert codes == ['en', 'und']


def test_an_ordinary_member_cannot_edit_the_community(app, owned_community):
    community, owner, ordinary, english = owned_community
    client = app.test_client()
    login(client, ordinary)
    token = csrf(app, client)

    response, _render = _edit(app, client, token, community, english,
                              title='hijacked')

    assert response.status_code == 401
    db.session.expire_all()
    assert db.session.get(Community, community.id).title != 'hijacked'


def test_a_moderator_can_edit_the_community(app, owned_community):
    """`community.is_owner() or current_user.is_admin() or
    community.is_moderator()` -- the third arm. A plain moderator may change
    the settings, unlike the ownership routes, which require owner or staff."""
    community, owner, ordinary, english = owned_community
    moderator = make_user(instance(), 'moderator', local=True)
    db.session.commit()
    make_community_member(moderator, community, is_moderator=True)
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    response, _render = _edit(app, client, token, community, english,
                              title='moderator renamed it')

    assert response.status_code == 302
    db.session.expire_all()
    assert db.session.get(Community, community.id).title == 'moderator renamed it'


def test_editing_a_community_that_does_not_exist_is_a_404(app, owned_community,
                                                          owner_client):
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    assert client.get(url(app, 'community.community_edit', community_id=999999)).status_code == 404


# --------------------------------------------------------------------------
# add_local: uploads, languages and the announcement
# --------------------------------------------------------------------------


def test_creating_a_community_saves_its_icon_and_banner(app, creator):
    """`if file:` on both -- `save_icon_file` returns None for something it
    will not accept, and assigning that would break the community page."""
    client, token, founder = creator
    icon = File(source_url='https://example.com/i.png', file_path='i.png')
    banner = File(source_url='https://example.com/b.png', file_path='b.png')
    db.session.add_all([icon, banner])
    db.session.commit()

    with patch('app.community.routes.save_icon_file', return_value=icon) as save_icon:
        with patch('app.community.routes.save_banner_file', return_value=banner):
            _add(app, client, token, url='withimages',
                 icon_file=(io.BytesIO(b'icon bytes'), 'icon.png'),
                 banner_file=(io.BytesIO(b'banner bytes'), 'banner.png'))

    community = Community.query.one()
    assert save_icon.call_count == 1
    assert (community.icon_id, community.image_id) == (icon.id, banner.id)


def test_an_upload_the_saver_rejects_leaves_the_community_without_one(app,
                                                                      creator):
    client, token, founder = creator

    with patch('app.community.routes.save_icon_file', return_value=None):
        _add(app, client, token, url='noicon',
             icon_file=(io.BytesIO(b'not an image'), 'icon.png'))

    assert Community.query.one().icon_id is None


def test_creating_a_community_stores_the_chosen_languages(app, creator):
    """Plus the undetermined language, always -- a post with no language would
    otherwise be refused by a community that lists every language its creator
    picked."""
    client, token, founder = creator
    english = Language.query.filter_by(code='en').one()

    _add(app, client, token, url='multilingual', languages=str(english.id))

    codes = sorted(language.code for language in Community.query.one().languages)
    assert codes == ['en', 'und']


def test_announcing_a_new_community_is_optional(app, creator):
    """`publicize` posts the new community to an external feed community, so
    the false arm is what keeps a private-ish community off it."""
    client, token, founder = creator

    with patch('app.community.routes.publicize_community') as publicize:
        _add(app, client, token, url='quiet')
    assert publicize.call_args_list == []

    with patch('app.community.routes.publicize_community') as publicize:
        _add(app, client, token, url='loud', publicize='y')
    assert publicize.call_args.args[0].name == 'loud'


# --------------------------------------------------------------------------
# add_remote
# --------------------------------------------------------------------------


@pytest.fixture
def searcher(app, db_session):
    local = instance()
    founder = make_user(local, 'founder', local=True)
    able_to_create(founder)
    db.session.commit()
    client = app.test_client()
    login(client, founder)
    return client, csrf(app, client), founder


def _search(app, client, token, address, **patches):
    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.post(url(app, 'community.add_remote'),
                               data={'address': address, 'submit': 'Search',
                                     'csrf_token': token})
    return response, render


@pytest.mark.parametrize('address, expected_handle', [
    ('!books@remote.example', '!books@remote.example'),
    ('https://remote.example/c/books', '!books@remote.example'),
])
def test_each_reachable_address_format_resolves_to_a_handle(app, searcher,
                                                            address,
                                                            expected_handle):
    """The TWO reachable branches of the five.

    `SearchRemoteCommunity.validate` refuses anything that does not start with
    `!` or `http(s)://`, so the route's `elif address.startswith('@')`, its
    `elif '@' in address` and its `else` -- the one that flashes the accepted
    formats -- can never run. Registered as D983; the row below pins the form's
    refusals, which is where those messages actually come from.
    """
    client, token, founder = searcher
    found = make_community('books', host='remote.example')
    db.session.commit()

    with patch('app.community.routes.search_for_community',
               return_value=found) as search:
        _search(app, client, token, address)

    assert search.call_args.args == (expected_handle,)


@pytest.mark.parametrize('address, message', [
    ('books@remote.example', 'Address must start with !'),
    ('nonsense', 'Address must start with !'),
    ('!noatsign', 'Address must include @'),
    ('!has/slash@remote.example', '/ cannot be in address'),
])
def test_the_form_refuses_an_address_it_cannot_parse(app, searcher, address,
                                                     message):
    """`SearchRemoteCommunity.validate`, which is where these messages come
    from -- and the reason three of the route's own branches are unreachable
    (D983). Each refusal has its own message, so each is asserted by message."""
    client, token, founder = searcher

    with patch('app.community.routes.search_for_community') as search:
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            response = client.post(url(app, 'community.add_remote'),
                                   data={'address': address, 'submit': 'Search',
                                         'csrf_token': token})

    assert response.status_code == 200
    assert str(render.call_args.kwargs['form'].address.errors[0]) == message
    assert search.call_args_list == [], 'a refused address still reached the lookup'


def test_the_search_page_renders_on_a_get(app, searcher):
    client, token, founder = searcher

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.add_remote'))

    assert response.status_code == 200
    assert render.call_args.kwargs['new_community'] is None


def test_a_blocked_instance_says_so(app, searcher):
    """`if 'is blocked.' in str(e)` -- `search_for_community` raises for an
    instance this one has defederated, and the message points at Fediseer
    rather than looking like a lookup failure."""
    client, token, founder = searcher

    with patch('app.community.routes.search_for_community',
               side_effect=Exception('that instance is blocked.')):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client.post(url(app, 'community.add_remote'),
                            data={'address': '!x@blocked.example',
                                  'submit': 'Search', 'csrf_token': token})

    assert 'that instance is blocked' in str(flashed.call_args_list[0].args[0])


@pytest.mark.parametrize('enable_nsfw, expected', [
    (True, 'Community not found.'),
    (False, 'Community not found. If you are searching for a nsfw community '
            'it is blocked by this instance.'),
])
def test_a_community_that_cannot_be_found_says_why(app, searcher, enable_nsfw,
                                                   expected):
    """The two `if new_community is None` messages. On an instance with NSFW
    disabled, "not found" is often really "refused", and saying so is what
    stops the operator hunting a bug that is a setting.

    Asserted by EQUALITY, not `in`. The nsfw message begins with the plain one
    -- 'Community not found. If you are searching for...' -- so a substring
    check passes for both values of the setting, and the mutant forcing the
    else branch survived every run.
    """
    client, token, founder = searcher
    site = db.session.get(Site, 1)
    site.enable_nsfw = enable_nsfw
    db.session.commit()

    with patch('app.community.routes.search_for_community', return_value=None):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client.post(url(app, 'community.add_remote'),
                            data={'address': '!x@remote.example',
                                  'submit': 'Search', 'csrf_token': token})

    assert str(flashed.call_args_list[0].args[0]) == expected


def test_a_banned_community_is_flagged_when_found(app, searcher):
    """`if new_community.banned:` -- the search succeeds and the result is
    unusable, which is a different thing from not finding it."""
    client, token, founder = searcher
    found = make_community('banned_one', host='remote.example')
    found.banned = True
    db.session.commit()

    with patch('app.community.routes.search_for_community', return_value=found):
        with patch('app.community.routes.flash') as flashed:
            with patch('app.community.routes.render_template', return_value='rendered'):
                client.post(url(app, 'community.add_remote'),
                            data={'address': '!banned_one@remote.example',
                                  'submit': 'Search', 'csrf_token': token})

    assert 'is banned from' in str(flashed.call_args_list[0].args[0])


def test_adding_remote_communities_can_be_restricted(app, db_session):
    """`allow_default_user_add_remote_community` -- unlike `add_local`'s
    equivalent this one uses `is_admin_or_staff()`, so staff are included."""
    from app.utils import set_setting

    local = instance()
    make_user(local, 'founder', local=True)
    ordinary = make_user(local, 'ordinary', local=True)
    able_to_create(ordinary)
    set_setting('allow_default_user_add_remote_community', False)
    client = app.test_client()
    login(client, ordinary)

    with patch('app.community.routes.flash') as flashed:
        response = client.get(url(app, 'community.add_remote'))

    assert response.status_code == 302
    assert 'restricted to admin and staff' in flashed.call_args.args[0]


def test_a_banned_user_cannot_search_for_remote_communities(app, db_session):
    local = instance()
    make_user(local, 'founder', local=True)
    banned = make_user(local, 'banned', local=True)
    able_to_create(banned)
    banned.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, banned)

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        client.get(url(app, 'community.add_remote'))

    assert render.call_args_list == []


# --------------------------------------------------------------------------
# community_delete
# --------------------------------------------------------------------------


def test_deleting_a_local_community_bans_it_and_records_the_reason(
        app, owned_community, owner_client):
    """A local community is banned as well as deleted, and the modlog entry
    names who did it -- deleting a community destroys everyone's posts in it,
    so the trail is the point."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    with patch('app.community.routes.add_to_modlog') as modlog:
        response = client.post(
            url(app, 'community.community_delete', community_id=community.id),
            data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 302
    assert modlog.call_args.args == ('delete_community',)
    assert owner.user_name in modlog.call_args.kwargs['reason']
    assert db.session.get(Community, community.id) is None


def test_the_delete_form_renders_on_a_get(app, owned_community, owner_client):
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_delete',
                                  community_id=community.id))

    assert response.status_code == 200
    assert render.call_args.kwargs['community'].id == community.id
    assert db.session.get(Community, community.id) is not None


def test_an_ordinary_member_cannot_delete_the_community(app, owned_community):
    community, owner, ordinary, english = owned_community
    client = app.test_client()
    login(client, ordinary)
    token = csrf(app, client)

    response = client.post(
        url(app, 'community.community_delete', community_id=community.id),
        data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 401
    assert db.session.get(Community, community.id) is not None


def test_a_plain_moderator_cannot_delete_the_community(app, owned_community):
    """`community.is_owner() or current_user.is_admin()` -- deletion needs
    ownership, unlike editing, which any moderator may do."""
    community, owner, ordinary, english = owned_community
    moderator = make_user(instance(), 'moderator', local=True)
    db.session.commit()
    make_community_member(moderator, community, is_moderator=True)
    client = app.test_client()
    login(client, moderator)
    token = csrf(app, client)

    response = client.post(
        url(app, 'community.community_delete', community_id=community.id),
        data={'submit': 'Delete', 'csrf_token': token})

    assert response.status_code == 401
    assert db.session.get(Community, community.id) is not None


def test_a_banned_owner_cannot_delete_the_community(app, owned_community):
    community, owner, ordinary, english = owned_community
    owner.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, owner)
    token = csrf(app, client)

    client.post(url(app, 'community.community_delete', community_id=community.id),
                data={'submit': 'Delete', 'csrf_token': token})

    assert db.session.get(Community, community.id) is not None


def test_deleting_a_community_that_does_not_exist_is_a_404(app,
                                                           owned_community,
                                                           owner_client):
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    assert client.get(url(app, 'community.community_delete',
                          community_id=999999)).status_code == 404


# --------------------------------------------------------------------------
# community_edit: replacing the images, and the banned check
# --------------------------------------------------------------------------


def test_a_banned_owner_cannot_edit_the_community(app, owned_community):
    community, owner, ordinary, english = owned_community
    community.title = 'Stored title'
    owner.banned = True
    db.session.commit()
    client = app.test_client()
    login(client, owner)
    token = csrf(app, client)

    _edit(app, client, token, community, english, title='changed anyway')

    db.session.expire_all()
    assert db.session.get(Community, community.id).title == 'Stored title'


@pytest.mark.parametrize('field, column, saver', [
    ('icon_file', 'icon_id', 'save_icon_file'),
    ('banner_file', 'image_id', 'save_banner_file'),
])
def test_replacing_an_image_removes_the_old_one(app, owned_community,
                                                owner_client, field, column,
                                                saver):
    """The old row is deleted AND the file unlinked, and only after the new one
    has been saved -- the route's own comment says so, because failing halfway
    with the old one already gone would leave the community with no image at
    all."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client
    old = File(source_url='https://example.com/old.png', file_path='old.png')
    new = File(source_url='https://example.com/new.png', file_path='new.png')
    db.session.add_all([old, new])
    db.session.commit()
    setattr(community, column, old.id)
    db.session.commit()
    old_id = old.id

    with patch.object(File, 'delete_from_disk') as delete_from_disk:
        with patch(f'app.community.routes.{saver}', return_value=new):
            _edit(app, client, token, community, english,
                  **{field: (io.BytesIO(b'bytes'), 'new.png')})

    assert delete_from_disk.call_count == 1
    db.session.expire_all()
    assert getattr(db.session.get(Community, community.id), column) == new.id
    assert db.session.get(File, old_id) is None


@pytest.mark.parametrize('field, column, saver', [
    ('icon_file', 'icon_id', 'save_icon_file'),
    ('banner_file', 'image_id', 'save_banner_file'),
])
def test_an_upload_the_saver_rejects_leaves_the_old_image_alone(
        app, owned_community, owner_client, field, column, saver):
    """`if file:` -- the guard that makes the ordering safe. A rejected upload
    must not take the existing image with it."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client
    old = File(source_url='https://example.com/old.png', file_path='old.png')
    db.session.add(old)
    db.session.commit()
    setattr(community, column, old.id)
    db.session.commit()

    with patch.object(File, 'delete_from_disk') as delete_from_disk:
        with patch(f'app.community.routes.{saver}', return_value=None):
            _edit(app, client, token, community, english,
                  **{field: (io.BytesIO(b'not an image'), 'new.png')})

    assert delete_from_disk.call_args_list == []
    db.session.expire_all()
    assert getattr(db.session.get(Community, community.id), column) == old.id


@pytest.mark.parametrize('field, column, saver', [
    ('icon_file', 'icon_id', 'save_icon_file'),
    ('banner_file', 'image_id', 'save_banner_file'),
])
def test_adding_a_first_image_deletes_nothing(app, owned_community,
                                              owner_client, field, column,
                                              saver):
    """`if old_icon_id:` -- a community that had no image yet."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client
    new = File(source_url='https://example.com/new.png', file_path='new.png')
    db.session.add(new)
    db.session.commit()

    with patch.object(File, 'delete_from_disk') as delete_from_disk:
        with patch(f'app.community.routes.{saver}', return_value=new):
            _edit(app, client, token, community, english,
                  **{field: (io.BytesIO(b'bytes'), 'new.png')})

    assert delete_from_disk.call_args_list == []
    db.session.expire_all()
    assert getattr(db.session.get(Community, community.id), column) == new.id


def test_changing_the_topic_updates_both_counts(app, owned_community,
                                                owner_client):
    """`if community.topic_id != old_topic_id:` -- the community leaves one
    topic and joins another, and both `num_communities` have to be
    recomputed. The old topic's count is the one a naive fix forgets."""
    from app.models import Topic

    community, owner, ordinary, english = owned_community
    client, token = owner_client
    old_topic = Topic(name='Old', machine_name='old', num_communities=99)
    new_topic = Topic(name='New', machine_name='new', num_communities=99)
    db.session.add_all([old_topic, new_topic])
    db.session.commit()
    community.topic_id = old_topic.id
    db.session.commit()

    _edit(app, client, token, community, english, topic=str(new_topic.id))

    db.session.expire_all()
    assert db.session.get(Community, community.id).topic_id == new_topic.id
    assert db.session.get(Topic, new_topic.id).num_communities == 1
    assert db.session.get(Topic, old_topic.id).num_communities == 0


def test_clearing_the_topic_updates_the_old_count(app, owned_community,
                                                  owner_client):
    """`topics_for_form` offers `(-1, 'None')`, and the route stores None for
    anything not greater than zero -- so the old topic still has to be
    recounted."""
    from app.models import Topic

    community, owner, ordinary, english = owned_community
    client, token = owner_client
    old_topic = Topic(name='Old', machine_name='old', num_communities=99)
    db.session.add(old_topic)
    db.session.commit()
    community.topic_id = old_topic.id
    db.session.commit()

    _edit(app, client, token, community, english, topic='-1')

    db.session.expire_all()
    assert db.session.get(Community, community.id).topic_id is None
    assert db.session.get(Topic, old_topic.id).num_communities == 0


def test_an_edit_that_does_not_move_the_community_leaves_the_counts(
        app, owned_community, owner_client):
    """The false arm. Recomputing on every save would be harmless but the
    branch exists, and a row that never takes it cannot say the condition is
    right."""
    from app.models import Topic

    community, owner, ordinary, english = owned_community
    client, token = owner_client
    topic = Topic(name='Same', machine_name='same', num_communities=99)
    db.session.add(topic)
    db.session.commit()
    community.topic_id = topic.id
    db.session.commit()

    _edit(app, client, token, community, english, topic=str(topic.id))

    db.session.expire_all()
    assert db.session.get(Topic, topic.id).num_communities == 99


def test_an_edit_federates_and_clears_the_membership_caches(app,
                                                            owned_community,
                                                            owner_client):
    """`task_selector('edit_community', ...)` is how peers learn the settings
    changed, and the three memoized helpers decide what the owner's own
    sidebar shows."""
    community, owner, ordinary, english = owned_community
    client, token = owner_client

    with patch('app.community.routes.render_template', return_value='rendered'):
        with patch('app.community.routes.task_selector') as task:
            with patch('app.community.routes.cache.delete_memoized') as delete_memoized:
                client.post(url(app, 'community.community_edit',
                                community_id=community.id),
                            data=_edit_payload(token, english, title='Renamed'),
                            content_type='multipart/form-data')

    assert task.call_args.args == ('edit_community',)
    assert task.call_args.kwargs['community_id'] == community.id
    assert delete_memoized.call_count >= 3


# --------------------------------------------------------------------------
# community_move
# --------------------------------------------------------------------------


@pytest.fixture
def remote_community(app, db_session):
    """`community_move` only exists for REMOTE communities -- it asks this
    instance's admins to migrate one somebody else hosts, so a local community
    is a 404."""
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1
    requester = make_user(local, 'requester', local=True)
    requester.email = 'requester@example.com'
    remote = make_community('elsewhere', host='remote.example')
    # Community.is_local() is `ap_id is None or profile_id() startswith
    # SERVER_URL` (app/models.py:795), so an ap_id is what makes it remote.
    remote.ap_id = 'elsewhere@remote.example'
    remote.instance_id = instance('remote.example', 'lemmy').id
    site = db.session.get(Site, 1)
    site.contact_email = 'admin@test.piefed.local'
    db.session.commit()
    assert not remote.is_local()
    # actor_to_community resolves a REMOTE community by its qualified handle
    # (`name@domain`), which is what Community.link() returns for one -- the
    # bare name only resolves a local community.
    assert '@' in remote.link()

    client = app.test_client()
    login(client, requester)
    return client, csrf(app, client), remote, requester


def test_requesting_a_move_emails_the_admins_and_notifies_them(
        app, remote_community):
    """Both halves: the email carries the request, and the Notification plus
    user 1's unread counter are what make anybody look at it. The counter is
    bumped with raw SQL against id 1, so it is asserted rather than assumed."""
    from app.models import Notification

    client, token, remote, requester = remote_community
    before = db.session.get(User, 1).unread_notifications or 0

    with patch('app.community.routes.send_email') as send_email:
        with patch('app.community.routes.render_template', return_value='rendered'):
            response = client.post(
                url(app, 'community.community_move', actor=remote.link()),
                data={'post_link': 'https://remote.example/post/1',
                      # DataRequired() on a BooleanField means it must be
                      # TICKED, not merely present -- the request is a claim
                      # that the old community has already been locked.
                      'old_community_locked': 'y',
                      'submit': 'Request', 'csrf_token': token})

    assert response.status_code == 200
    assert send_email.call_count == 1
    assert send_email.call_args.args[2] == 'admin@test.piefed.local'
    assert send_email.call_args.args[5] == 'requester@example.com'
    notification = Notification.query.filter_by(user_id=1).one()
    assert notification.subtype == 'community_move_request'
    assert notification.url == f'/admin/community/{remote.id}/move/{requester.id}'
    db.session.expire_all()
    assert db.session.get(User, 1).unread_notifications == before + 1


def test_the_move_form_renders_on_a_get(app, remote_community):
    """The `return render_template(...)` reached without a submission -- it
    sits outside the `validate_on_submit` block, so a GET and a refused POST
    both land on it."""
    client, token, remote, requester = remote_community

    with patch('app.community.routes.render_template', return_value='rendered') as render:
        response = client.get(url(app, 'community.community_move',
                                  actor=remote.link()))

    assert response.status_code == 200
    assert render.call_args.kwargs['community'].id == remote.id
    from app.models import Notification
    assert Notification.query.count() == 0


def test_a_local_community_cannot_be_moved(app, remote_community):
    """`if community is not None and not community.is_local()` -- there is
    nothing to migrate, and the request would email the admins about their own
    instance."""
    client, token, remote, requester = remote_community
    local_community = make_community('ourown')
    db.session.commit()

    with patch('app.community.routes.send_email') as send_email:
        response = client.get(url(app, 'community.community_move',
                                  actor=local_community.name))

    assert response.status_code == 404
    assert send_email.call_args_list == []


def test_moving_a_community_that_does_not_exist_is_a_404(app,
                                                          remote_community):
    client, token, remote, requester = remote_community

    assert client.get(url(app, 'community.community_move',
                          actor='no-such-community')).status_code == 404


def test_a_banned_user_cannot_request_a_move(app, remote_community):
    client, token, remote, requester = remote_community
    requester.banned = True
    db.session.commit()

    with patch('app.community.routes.send_email') as send_email:
        client.post(url(app, 'community.community_move', actor=remote.link()),
                    data={'post_link': 'https://remote.example/post/1',
                          'old_community_locked': 'y',
                          'submit': 'Request', 'csrf_token': token})

    assert send_email.call_args_list == []


def test_add_local_falls_back_when_the_request_hook_has_not_set_g_site(
        app, creator):
    """`try: site = g.site / except: site = db.session.get(Site, 1)`.

    The fallback exists because `g.site` is populated by a before_request hook
    that does not run for every dispatch path. It could not previously rescue
    anything -- `g.site.enable_nsfw` was dereferenced three lines below it,
    unguarded -- so this row is only possible now that the route uses the local
    `site` throughout (D984).

    `g` is patched rather than emptied: deleting the attribute from the real
    request globals would leave it missing for the rest of the request,
    including the template layer.
    """
    from types import SimpleNamespace

    client, token, founder = creator

    with patch('app.community.routes.g', SimpleNamespace()):
        with patch('app.community.routes.render_template', return_value='rendered') as render:
            response = client.get(url(app, 'community.add_local'))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None
