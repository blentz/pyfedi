"""A text field a submitted form leaves out reads as empty text (fuzz-routes finding, 2026-11-13). WTForms 3 leaves
its data None there, where 2 made it '', and PieFed's views read text fields as text: form.urls.data.strip() was a 500
from POST /user/files/upload. Validation is as it was: DataRequired refuses '' as it refused None."""
from werkzeug.datastructures import ImmutableMultiDict
from wtforms import PasswordField, StringField, TextAreaField
from wtforms.validators import DataRequired

from app.auth.forms import LoginForm


def test_a_text_field_left_out_of_a_post_is_empty_text(app):
    with app.test_request_context('/auth/login', method='POST', data={'password': 'x' * 10}):
        form = LoginForm()
        assert form.user_name.data == ''


def test_a_posted_value_is_still_the_value(app):
    with app.test_request_context('/auth/login', method='POST', data={'user_name': 'someone'}):
        assert LoginForm().user_name.data == 'someone'


def test_a_form_shown_without_a_post_is_as_it_was(app):
    with app.test_request_context('/auth/login', method='GET'):
        assert LoginForm().user_name.data is None


def test_data_required_still_refuses_a_text_field_left_out(app):
    with app.test_request_context('/auth/login', method='POST', data={'password': 'x' * 10}):
        form = LoginForm(meta={'csrf': False})
        assert not form.validate() and 'user_name' in form.errors


def test_every_kind_of_text_field_is_covered():
    for kind in (StringField, TextAreaField, PasswordField):
        field = kind(validators=[DataRequired()]).bind(form=None, name='f', _meta=LoginForm.Meta())
        field.process(ImmutableMultiDict())
        assert field.data == '', kind
