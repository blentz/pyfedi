"""How many images a local album post may hold: an admin setting, default 4 (Pixelfed interop R7).

Mastodon shows 4 attachments and Pixelfed's MAX_ALBUM_LENGTH defaults to 4; an admin may raise it, accepting that
Mastodon drops the rest. The ceiling is 9: each image may be 10 MB (R207) and a request may be MAX_CONTENT_LENGTH,
100 MB by default, so ten images could not be posted in one form.
"""
from unittest.mock import patch

import pytest
from werkzeug.datastructures import MultiDict

from app.admin.forms import SiteMiscForm
from app.constants import MAX_ALBUM_IMAGES_CEILING, MAX_ALBUM_IMAGES_DEFAULT
from app.utils import max_album_images, set_setting
from tests.test_admin_routes_entry import _seed, _settings_admin, login


def test_the_default_is_four(app, db_session):
    assert (MAX_ALBUM_IMAGES_DEFAULT, MAX_ALBUM_IMAGES_CEILING) == (4, 9)
    assert max_album_images() == 4


@pytest.mark.parametrize('stored,expected', [(6, 6), (1, 1), (50, 9), (0, 1), (-3, 1), ('7', 7), ('x', 4), (None, 4)])
def test_a_stored_value_is_held_to_the_range(app, db_session, stored, expected):
    """A row written by hand, or by an older form, cannot make albums empty or larger than one request can carry."""
    set_setting('max_album_images', stored)
    assert max_album_images() == expected


def test_the_admin_form_shows_the_default(app, db_session):
    client = app.test_client()
    login(client, _settings_admin(_seed()[0]))
    with patch('app.admin.routes.render_template', return_value='rendered') as render:
        client.get('/admin/misc')
    assert render.call_args.kwargs['form'].max_album_images.data == 4


@pytest.mark.parametrize('value,valid', [('1', True), ('9', True), ('0', False), ('10', False), ('', False)])
def test_the_admin_form_takes_one_to_nine(app, db_session, value, valid):
    with app.test_request_context():
        form = SiteMiscForm(formdata=MultiDict({'max_album_images': value}), meta={'csrf': False})
        assert form.max_album_images.validate(form) is valid
