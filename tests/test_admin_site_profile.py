"""`admin_site` -- the site profile page and its icon upload.

Sub-project 79, slice B. The function is 151 of `app/admin/routes.py`'s gaps
and is almost entirely one thing: an upload that writes to a SERVED directory,
deletes files, and shells out to Pillow and to the SVG sanitizer, reachable by
anyone holding `change instance settings`.

Two defects were found and fixed here, and both are the same mistake -- the
route changed disk state before it knew the upload was usable:

* a corrupt upload unlinked the existing logo files and then raised, leaving
  the row pointing at files that no longer existed (D912);
* `.SVG` was sanitized as an SVG by a case-INSENSITIVE guard and then handed to
  `Image.open` by a case-SENSITIVE one, raising and leaving the bytes in the
  media root (D913).

Every row that writes to `app/static/media` cleans up after itself in a
`finally`, because that directory is the real one -- there is no tmpdir
indirection in this route and patching `directory` would test a different
function.
"""
import glob
import io
import os

import pytest
from unittest.mock import patch

from PIL import Image
from werkzeug.exceptions import HTTPException

from app import db
from app.models import Site
from tests.factories import grant_permission, make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

MEDIA = 'app/static/media'

SVG = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
       b'<rect width="10" height="10"/></svg>')

# An SVG py-svg-hush refuses. sanitize_svg() destroys the file before returning
# False, which is the invariant its docstring states.
HOSTILE_SVG = (b'<?xml version="1.0"?><!DOCTYPE svg [<!ENTITY a "'
               + b'x' * 64 + b'"><!ENTITY b "&a;&a;&a;">]>'
               b'<svg xmlns="http://www.w3.org/2000/svg">&b;&b;&b;&b;&b;&b;'
               b'&b;&b;&b;&b;</svg>')


def _png(width, height):
    """A real PNG, not a stub: the route decodes it and builds six thumbnails,
    so a placeholder would test the error path instead."""
    buffer = io.BytesIO()
    Image.new('RGB', (width, height), (10, 20, 30)).save(buffer, format='PNG')
    return buffer.getvalue()


def _seed():
    instance = make_instance('test.piefed.local', software='piefed')
    founder = make_user(instance, 'founder', local=True)
    assert founder.id == 1  # fact 347: user 1 passes every user_access check
    ordinary = make_user(instance, 'ordinary', local=True)
    ordinary.verified = True
    db.session.commit()
    return instance, ordinary


def _settings_admin(instance, name='settingsadmin'):
    """Holds ONLY 'change instance settings' and is not user 1 -- fact 348."""
    user = make_user(instance, name, local=True)
    user.verified = True
    db.session.commit()
    assert user.id != 1
    grant_permission(user, 'change instance settings')
    return user


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(app, client):
    """Fact 355: `login_required(csrf=True)` validates CSRF itself, regardless
    of `WTF_CSRF_ENABLED`, so every POST here needs a real token."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def _payload(token, **overrides):
    """A SiteProfileForm submission that validates. contact_email carries
    DataRequired() and Length(min=5); the rest are free text."""
    data = {'name': 'Test Instance', 'description': 'a tagline', 'about': '',
            'sidebar': '', 'legal_information': '', 'tos_url': '',
            'announcement': '', 'contact_email': 'admin@example.com',
            'submit': 'Save', 'csrf_token': token}
    data.update(overrides)
    return data


def _post(client, token, **overrides):
    """Every POST here is multipart, and every one patches render_template.

    Multipart because `request.files['icon']` raises BadRequestKeyError on a
    request with no file part at all -- a plain form-encoded POST to this route
    is a bare 400, which is R2. Patched because the real admin/site.html
    renders `form.csrf_token`, and tests/conftest.py sets WTF_CSRF_ENABLED
    False, so FlaskForm does not declare that field and Jinja raises
    UndefinedError -- a template-rendering failure that has nothing to do with
    the behaviour under test.
    """
    icon = overrides.pop('icon', (io.BytesIO(b''), ''))
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        response = client.post('/admin/site',
                               data=_payload(token, icon=icon, **overrides),
                               content_type='multipart/form-data')
    return response, render


def _media_files():
    return set(glob.glob(f'{MEDIA}/logo_*')) | set(glob.glob(f'{MEDIA}/existing*'))


@pytest.fixture
def media_is_clean():
    """Remove only what the row created. The media root is the real one and may
    hold files this suite did not put there."""
    before = _media_files()
    yield
    for path in _media_files() - before:
        os.unlink(path)


@pytest.fixture
def admin_client(app, db_session):
    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    client = app.test_client()
    login(client, admin)
    return client, csrf(app, client)


def _existing_logo():
    """A site that already has a logo: five files on disk, five columns and two
    settings pointing at them. This is the state P1 destroyed."""
    from app.utils import set_setting

    os.makedirs(MEDIA, exist_ok=True)
    names = {'logo': '_100', 'logo_180': '_180', 'logo_152': '_152',
             'logo_32': '_32', 'logo_16': '_16'}
    site = db.session.get(Site, 1)
    for column, suffix in names.items():
        with open(f'{MEDIA}/existing{suffix}.png', 'wb') as handle:
            handle.write(b'the bytes that were already there')
        setattr(site, column, f'/static/media/existing{suffix}.png')
    for setting, suffix in (('logo_512', '_512'), ('logo_192', '_192')):
        with open(f'{MEDIA}/existing{suffix}.png', 'wb') as handle:
            handle.write(b'the bytes that were already there')
        set_setting(setting, f'/static/media/existing{suffix}.png')
    db.session.commit()
    return site


# --------------------------------------------------------------------------
# Authorization
# --------------------------------------------------------------------------


def test_the_site_profile_needs_the_settings_permission(app, db_session):
    instance, ordinary = _seed()
    client = app.test_client()
    login(client, ordinary)

    response = client.get('/admin/site')

    assert response.status_code == 302
    assert '/permission_denied' in response.headers['Location']


# --------------------------------------------------------------------------
# P1: a corrupt upload must not destroy the existing logo
# --------------------------------------------------------------------------


def test_an_undecodable_upload_leaves_the_existing_logo_alone(admin_client,
                                                              media_is_clean):
    """P1's pin, inverted -- the round's durable artefact.

    The route used to unlink all seven superseded files BEFORE Pillow had seen
    the upload. An upload it could not decode then raised
    `UnidentifiedImageError`, `db.session.commit()` was never reached, and the
    site served a broken image on every page while the row insisted the logo
    was fine. Measured before the fix:

        PROBE s5 old logo on disk before: True
        PROBE s5 RAISED: UnidentifiedImageError cannot identify image file ...
        PROBE s5 row still points at: /static/media/existing_100.png
        PROBE s5 old logo on disk after: False

    Both halves are asserted: the files and the row. Either alone would pass
    against a fix that kept the files but cleared the columns.
    """
    client, token = admin_client
    site = _existing_logo()
    previous = {column: getattr(site, column)
                for column in ('logo', 'logo_180', 'logo_152', 'logo_32', 'logo_16')}

    response, _ = _post(client, token,
                        icon=(io.BytesIO(b'this is not a PNG at all'), 'evil.png'))

    assert response.status_code == 400

    for suffix in ('_100', '_180', '_152', '_32', '_16', '_512', '_192'):
        assert os.path.isfile(f'{MEDIA}/existing{suffix}.png'), (
            f'existing{suffix}.png was deleted by an upload that failed')

    db.session.expire_all()
    site = db.session.get(Site, 1)
    for column, value in previous.items():
        assert getattr(site, column) == value


def test_an_undecodable_upload_does_not_leave_its_bytes_in_the_media_root(
        admin_client, media_is_clean):
    """`app/static/media` is SERVED. An upload the route refuses must not stay
    there -- nothing else ever removes it, so every attempt would accumulate."""
    client, token = admin_client

    response, _ = _post(client, token,
                        icon=(io.BytesIO(b'not an image'), 'evil.png'))

    assert response.status_code == 400
    assert glob.glob(f'{MEDIA}/logo_*') == []


def test_a_good_upload_does_remove_the_superseded_files(admin_client,
                                                        media_is_clean):
    """The other half of P1, and the one that keeps the fix honest: deferring
    the deletion must not turn into never deleting. A row asserting only that
    the old files survive a FAILED upload is satisfied by deleting nothing at
    all, which would leak a set of files on every logo change.
    """
    client, token = admin_client
    _existing_logo()

    response, _ = _post(client, token, icon=(io.BytesIO(_png(200, 200)), 'new.png'))

    assert response.status_code == 200
    for suffix in ('_100', '_180', '_152', '_32', '_16', '_512', '_192'):
        assert not os.path.isfile(f'{MEDIA}/existing{suffix}.png'), (
            f'existing{suffix}.png survived a successful replacement')


# --------------------------------------------------------------------------
# P2: '.SVG' is an SVG
# --------------------------------------------------------------------------


def test_an_uppercase_svg_is_stored_as_an_svg(admin_client, media_is_clean):
    """P2's pin, inverted.

    `allowed_extensions` is checked with `.lower()` and the form's FileAllowed
    lowercases too, so `.SVG` is accepted; the sanitize guard is
    case-insensitive; the branch predicate was not. Measured before the fix:

        PROBE s1 RAISED: UnidentifiedImageError cannot identify image file
                         'app/static/media/logo_Vyucu.SVG'
        PROBE s1 orphans left: ['logo_Vyucu.SVG']

    Asserted the same way as the lowercase control below, so the two cases are
    demonstrably one behaviour rather than two similar ones.
    """
    client, token = admin_client
    _existing_logo()

    response, _ = _post(client, token, icon=(io.BytesIO(SVG), 'LOGO.SVG'))

    assert response.status_code == 200
    db.session.expire_all()
    site = db.session.get(Site, 1)
    assert site.logo.endswith('.SVG')
    assert os.path.isfile(f'app{site.logo}')
    # The SVG branch clears the raster fields rather than deriving them, and
    # _existing_logo() set them first -- fact 350: on a fresh Site they are
    # already '' and asserting '' would prove nothing.
    assert (site.logo_180, site.logo_152, site.logo_32, site.logo_16) == ('', '', '', '')


def test_a_lowercase_svg_is_stored_the_same_way(admin_client, media_is_clean):
    """The control for the row above."""
    client, token = admin_client
    _existing_logo()

    response, _ = _post(client, token, icon=(io.BytesIO(SVG), 'logo.svg'))

    assert response.status_code == 200
    db.session.expire_all()
    site = db.session.get(Site, 1)
    assert site.logo.endswith('.svg')
    assert os.path.isfile(f'app{site.logo}')
    assert (site.logo_180, site.logo_152, site.logo_32, site.logo_16) == ('', '', '', '')


def test_an_svg_upload_clears_the_pwa_logo_settings(admin_client, media_is_clean):
    """The SVG branch sets logo_512 and logo_192 to '' rather than leaving the
    previous PNG paths, which would have the manifest pointing at files the
    same request deleted."""
    from app.utils import get_setting

    client, token = admin_client
    _existing_logo()

    _post(client, token, icon=(io.BytesIO(SVG), 'logo.svg'))

    assert get_setting('logo_512', 'unset') == ''
    assert get_setting('logo_192', 'unset') == ''


def test_an_svg_that_cannot_be_sanitized_is_refused(admin_client, media_is_clean):
    """sanitize_svg() destroys the file before returning False, and the route
    turns that into a 400. The SVG branch stores the file AS UPLOADED -- no
    Pillow re-encode -- and serves it from this origin on every page, so the
    refusal is the only thing between a hostile document and every visitor."""
    client, token = admin_client

    response, _ = _post(client, token, icon=(io.BytesIO(HOSTILE_SVG), 'hostile.svg'))

    assert response.status_code == 400
    assert glob.glob(f'{MEDIA}/logo_*') == []
    db.session.expire_all()
    assert db.session.get(Site, 1).logo in (None, '')


# --------------------------------------------------------------------------
# The raster path
# --------------------------------------------------------------------------


@pytest.mark.parametrize('filename', ['hostile.svg', 'HOSTILE.SVG'])
def test_an_svg_that_cannot_be_sanitized_is_refused_in_either_case(
        admin_client, media_is_clean, filename):
    """The sanitize guard is case-INSENSITIVE and must stay that way.

    Slice B made the branch predicate case-insensitive so that `.SVG` is stored
    as an SVG. That makes the case of the SANITIZE guard load-bearing in a way
    it was not before: if it reverted to `file_ext == '.svg'`, a `.SVG` upload
    would skip sanitizing and then be stored verbatim by the branch below and
    served from this origin on every page. The mutation pass found exactly that
    -- the case-sensitive sanitize guard survived every row until this one.
    """
    client, token = admin_client

    response, _ = _post(client, token,
                        icon=(io.BytesIO(HOSTILE_SVG), filename))

    assert response.status_code == 400
    assert glob.glob(f'{MEDIA}/logo_*') == []
    db.session.expire_all()
    assert db.session.get(Site, 1).logo in (None, '')


def test_a_large_png_is_thumbnailed_to_every_size(admin_client, media_is_clean):
    """`if img.width > 100` -- the large arm. Six derivatives are written and
    the original is deleted; each size is asserted, because a single
    `site.logo` assertion passes while five thumbnails are missing."""
    from app.utils import get_setting

    client, token = admin_client

    response, _ = _post(client, token, icon=(io.BytesIO(_png(400, 400)), 'big.png'))

    assert response.status_code == 200
    db.session.expire_all()
    site = db.session.get(Site, 1)

    assert site.logo.endswith('_100.png')
    for column, edge in ((site.logo, 100), (site.logo_180, 180),
                         (site.logo_152, 152), (site.logo_32, 32),
                         (site.logo_16, 16)):
        assert os.path.isfile(f'app{column}')
        with Image.open(f'app{column}') as image:
            assert max(image.size) == edge

    for setting, edge in (('logo_512', 400), ('logo_192', 192)):
        path = get_setting(setting, '')
        assert os.path.isfile(f'app{path}')
        with Image.open(f'app{path}') as image:
            # thumbnail() never enlarges, so a 400px source stays 400 at 512.
            assert max(image.size) == edge

    # delete_original is True on this arm.
    assert glob.glob(f'{MEDIA}/big.png') == []


def test_a_small_png_is_stored_without_being_scaled_up(admin_client, media_is_clean):
    """`else:` -- the small arm, and P3's pin.

    A source at or under 100px is saved as-is under `<base>.png` rather than
    `<base>_100.png`. For a PNG upload that path IS the uploaded file, so the
    unconditional `delete_original = True` that used to follow unlinked the
    logo the route had just stored: `site.logo` named a file that did not
    exist, and the site icon 404'd on every page. Measured as
    `FileNotFoundError: [Errno 2] No such file or directory:
    'app/static/media/logo_qHkJy.png'` -- raised by this row opening the path
    the route had just committed.

    The existence assertion is the point; the size assertion only says the
    image was not scaled up.
    """
    client, token = admin_client

    response, _ = _post(client, token, icon=(io.BytesIO(_png(64, 64)), 'small.png'))

    assert response.status_code == 200
    db.session.expire_all()
    site = db.session.get(Site, 1)

    assert site.logo.endswith('.png')
    assert not site.logo.endswith('_100.png')
    assert os.path.isfile(f'app{site.logo}'), (
        f'{site.logo} is committed as the site logo but is not on disk')
    with Image.open(f'app{site.logo}') as image:
        assert image.size == (64, 64)


def test_a_small_upload_in_another_format_still_removes_the_original(
        admin_client, media_is_clean):
    """The other half of P3: `delete_original` must not become "never delete".

    A small WEBP is converted to `<base>.png`, so `<base>.webp` is a different
    file and is still removed -- otherwise every non-PNG logo upload would
    leave its source in the served media root forever.
    """
    client, token = admin_client
    buffer = io.BytesIO()
    Image.new('RGB', (64, 64), (1, 2, 3)).save(buffer, format='WEBP')

    response, _ = _post(client, token, icon=(io.BytesIO(buffer.getvalue()),
                                             'small.webp'))

    assert response.status_code == 200
    db.session.expire_all()
    site = db.session.get(Site, 1)
    assert os.path.isfile(f'app{site.logo}')
    assert glob.glob(f'{MEDIA}/logo_*.webp') == []


def test_a_png_of_exactly_100_pixels_takes_the_small_arm(admin_client,
                                                         media_is_clean):
    """`> 100`, not `>= 100`. The boundary, because the two arms write
    different filenames and nothing else would tell the operators apart."""
    client, token = admin_client

    _post(client, token, icon=(io.BytesIO(_png(100, 100)), 'edge.png'))

    db.session.expire_all()
    assert not db.session.get(Site, 1).logo.endswith('_100.png')


def test_a_disallowed_extension_is_refused_by_the_form(admin_client,
                                                       media_is_clean):
    """The FIRST of the two layers. `SiteProfileForm.icon` carries
    `FileAllowed(['jpg', 'jpeg', 'png', 'webp', 'svg'])`, so a disallowed
    extension never reaches the route at all -- the form refuses it and the
    page comes back with a field error, not a 400."""
    client, token = admin_client

    response, render = _post(client, token,
                             icon=(io.BytesIO(_png(50, 50)), 'payload.php'))

    assert response.status_code == 200
    assert render.call_args.kwargs['form'].errors == {'icon': ['Images only!']}
    assert glob.glob(f'{MEDIA}/logo_*') == []


def test_the_routes_own_extension_check_still_refuses_what_the_form_would_let_by(
        app, db_session, media_is_clean):
    """The SECOND layer, `if file_ext.lower() not in allowed_extensions:
    abort(400)`.

    It is unreachable through the form today, because FileAllowed's list is a
    strict subset of `allowed_extensions` -- registered as D916. That makes it
    defence in depth for a route that writes into a SERVED directory, and the
    only honest way to exercise it is to relax the first layer, which is what
    this row does: it clears the form validator, states that it is doing so,
    and checks the route refuses on its own. If the route check is ever deleted
    as dead, this row is what fails.
    """
    from flask import g, session as flask_session
    from flask_login import login_user
    from flask_wtf.csrf import generate_csrf

    from app.admin.forms import SiteProfileForm
    from app.admin.routes import admin_site

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    site_row = db.session.get(Site, 1)

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']

    payload = _payload(token, icon=(io.BytesIO(_png(50, 50)), 'payload.php'))
    with app.test_request_context('/admin/site', method='POST', data=payload,
                                  content_type='multipart/form-data'):
        flask_session['csrf_token'] = raw
        g.site = site_row
        login_user(admin)
        # SiteProfileForm.icon is an UnboundField until the form is
        # instantiated, so the validators live in its kwargs, not on it.
        with patch.dict(SiteProfileForm.icon.kwargs, {'validators': []}):
            with pytest.raises(HTTPException) as refused:
                admin_site()

    assert refused.value.code == 400
    assert glob.glob(f'{MEDIA}/logo_*') == []


def test_an_empty_icon_field_leaves_the_logo_untouched(admin_client,
                                                       media_is_clean):
    """`if uploaded_icon and uploaded_icon.filename != ''` -- saving the page
    without choosing a file is the common case, and it must not touch the
    logo."""
    client, token = admin_client
    _existing_logo()

    response, _ = _post(client, token)

    assert response.status_code == 200
    db.session.expire_all()
    assert db.session.get(Site, 1).logo == '/static/media/existing_100.png'
    assert os.path.isfile(f'{MEDIA}/existing_100.png')


# --------------------------------------------------------------------------
# The text fields
# --------------------------------------------------------------------------


def test_the_profile_text_is_saved_and_rendered_to_html(admin_client):
    """about, sidebar and legal_information are each stored twice: raw for the
    edit form and rendered for the page. Both are asserted for all three --
    storing only the raw leaves the page blank, and only the HTML makes the
    field uneditable."""
    client, token = admin_client

    response, _ = _post(client, token, name='PieFed Test',
                        description='the tagline', about='# About us',
                        sidebar='## Sidebar', legal_information='**Legal**',
                        tos_url='https://example.com/tos',
                        contact_email='contact@example.com')

    assert response.status_code == 200
    db.session.expire_all()
    site = db.session.get(Site, 1)

    assert site.name == 'PieFed Test'
    assert site.description == 'the tagline'
    assert site.tos_url == 'https://example.com/tos'
    assert site.contact_email == 'contact@example.com'
    assert site.about == '# About us'
    assert '<h1>About us</h1>' in site.about_html
    assert site.sidebar == '## Sidebar'
    assert '<h2>Sidebar</h2>' in site.sidebar_html
    assert site.legal_information == '**Legal**'
    assert '<strong>Legal</strong>' in site.legal_information_html


@pytest.mark.parametrize('field, html_field', [
    ('about', 'about_html'),
    ('sidebar', 'sidebar_html'),
    ('legal_information', 'legal_information_html'),
])
def test_clearing_a_text_field_clears_its_html(admin_client, field, html_field):
    """The false arm of each `if form.<field>.data:`. Clearing the source must
    clear the rendered copy: leaving stale HTML behind would keep publishing
    text the admin deleted."""
    client, token = admin_client

    _post(client, token, **{field: 'something'})
    db.session.expire_all()
    assert getattr(db.session.get(Site, 1), html_field) != ''

    _post(client, token, **{field: ''})
    db.session.expire_all()
    assert getattr(db.session.get(Site, 1), html_field) == ''


def test_the_announcement_is_stored_raw_and_rendered(admin_client):
    """The announcement is a setting rather than a column, and it is written
    unconditionally -- unlike the three fields above, which are columns."""
    from app.utils import get_setting

    client, token = admin_client

    _post(client, token, announcement='# Heads up')

    assert get_setting('announcement', '') == '# Heads up'
    assert '<h1>Heads up</h1>' in get_setting('announcement_html', '')


def test_saving_the_profile_invalidates_the_cached_site(admin_client):
    """`cache.delete_memoized(get_site_as_dict)`. Every request reads the site
    through that memoized helper, so without the invalidation the new name
    would not appear until the cache expired."""
    from app.utils import get_site_as_dict

    client, token = admin_client

    with patch('app.admin.routes.cache.delete_memoized') as delete_memoized:
        _post(client, token, name='Renamed')

    # set_setting invalidates its own memoized readers too, so the assertion is
    # that get_site_as_dict is among them -- not that it is the only one.
    assert (get_site_as_dict,) in [call.args for call in delete_memoized.call_args_list]


def test_an_invalid_submission_is_redisplayed_with_its_errors(admin_client):
    """contact_email carries DataRequired(). A refused submission must come
    back showing what was typed, not the stored settings -- the same shape as
    slice A's D907."""
    client, token = admin_client

    _response, render = _post(client, token, name='typed but not saved',
                              contact_email='')

    form = render.call_args.kwargs['form']
    assert 'contact_email' in form.errors
    assert form.name.data == 'typed but not saved'
    db.session.expire_all()
    assert db.session.get(Site, 1).name != 'typed but not saved'


def test_the_form_is_prefilled_from_the_stored_settings(admin_client):
    """`elif request.method == 'GET':` -- the pre-fill arm, including the three
    `is not None` defaults that keep a never-set column out of the textarea as
    the string 'None'."""
    from app.utils import set_setting

    client, token = admin_client
    site = db.session.get(Site, 1)
    site.name = 'Stored Name'
    site.description = 'stored tagline'
    site.about = None
    site.sidebar = None
    site.legal_information = None
    site.tos_url = 'https://example.com/stored'
    site.contact_email = 'stored@example.com'
    db.session.commit()
    set_setting('announcement', 'stored announcement')

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/site')

    form = render.call_args.kwargs['form']
    assert form.name.data == 'Stored Name'
    assert form.description.data == 'stored tagline'
    assert form.tos_url.data == 'https://example.com/stored'
    assert form.contact_email.data == 'stored@example.com'
    assert form.announcement.data == 'stored announcement'
    assert (form.about.data, form.sidebar.data, form.legal_information.data) == ('', '', '')


def test_a_set_text_column_is_prefilled_as_itself(admin_client):
    """The true arm of the three `is not None` defaults above."""
    client, token = admin_client
    site = db.session.get(Site, 1)
    site.about = 'the about text'
    site.sidebar = 'the sidebar text'
    site.legal_information = 'the legal text'
    db.session.commit()

    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/site')

    form = render.call_args.kwargs['form']
    assert form.about.data == 'the about text'
    assert form.sidebar.data == 'the sidebar text'
    assert form.legal_information.data == 'the legal text'


def test_the_profile_creates_a_site_row_when_there_is_none(app, db_session):
    """`if site is None: site = Site()` and `if site.id is None:
    db.session.add(site)`.

    Called directly rather than over HTTP, for the reason registered as D911:
    with no Site row the request lifecycle cannot run at all, because
    `get_site_as_dict` dereferences `site.__table__` with no nil check before
    any view is reached. Fact 354 is why `g.site` is supplied to a context that
    is entered once rather than nested inside another.
    """
    from flask import g, session as flask_session
    from flask_login import login_user
    from flask_wtf.csrf import generate_csrf

    from app.admin.routes import admin_site

    instance, ordinary = _seed()
    admin = _settings_admin(instance)
    site_row = db.session.get(Site, 1)

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']

    # Multipart, because request.files['icon'] raises BadRequestKeyError on a
    # request with no file part at all.
    payload = _payload(token, icon=(io.BytesIO(b''), ''))
    with app.test_request_context('/admin/site', method='POST', data=payload,
                                  content_type='multipart/form-data'):
        flask_session['csrf_token'] = raw
        g.site = site_row
        login_user(admin)
        db.session.delete(site_row)
        db.session.commit()
        assert Site.query.count() == 0

        with patch('app.admin.routes.render_template', return_value='rendered') as render:
            admin_site()
        # Fact 356: without this the branch under test may never have run.
        assert render.call_args.kwargs['form'].errors == {}

    assert Site.query.count() == 1
    assert db.session.execute(db.select(Site)).scalar_one().name == 'Test Instance'
