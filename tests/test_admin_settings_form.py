"""app.admin.settings_form: one form, shown and saved for the first domain or for a sister, whose "the first domain's"
is NULL (spec 2026-11-20-multitenant-domain-config MT3, MT4)."""
from types import SimpleNamespace

import pytest
from flask_wtf import FlaskForm
from werkzeug.datastructures import MultiDict
from wtforms import BooleanField, IntegerField, SelectField, StringField, TextAreaField
from wtforms.validators import DataRequired, InputRequired, NumberRange

from app.admin.settings_form import INHERIT, NO, YES, Setting, load, offer_inheritance, save, sister_form
from app.form_choices import select_choices


class Toy(FlaskForm):
    flag = BooleanField('A flag', description='Whether')
    mode = SelectField('A mode', choices=select_choices([('a', 'Mode A'), ('b', 'Mode B')]), validators=[DataRequired()])
    language = SelectField('A language', coerce=int, validators=[DataRequired()])
    size_mb = IntegerField('A size', validators=[InputRequired(), NumberRange(min=1, max=10)])
    countries = TextAreaField('Countries')
    text = StringField('Some text')
    wide = IntegerField('Instance-wide', validators=[InputRequired()])
    gone = BooleanField('First domain only')


SETTINGS = [Setting('flag'), Setting('mode'), Setting('language', 'language_id'), Setting('size_mb', 'size_bytes', 1024),
            Setting('countries', upper=True), Setting('text')]


def form_of(form_class, data=None):
    return form_class(formdata=MultiDict(data) if data is not None else None, meta={'csrf': False})


def row(**values):
    names = ('flag', 'mode', 'language_id', 'size_bytes', 'countries', 'text')
    return SimpleNamespace(**{**dict.fromkeys(names), **values})


@pytest.fixture
def ctx(app):
    with app.test_request_context():
        yield


def test_the_first_domains_page_shows_its_row(ctx):
    form = form_of(Toy)
    load(form, Toy, SETTINGS, row(flag=True, mode='b', language_id=2, size_bytes=4096, countries='gb\nfr', text=None),
         sister=False)
    assert (form.flag.data, form.mode.data, form.language.data, form.size_mb.data, form.countries.data, form.text.data) == \
        (True, 'b', 2, 4, 'GB\nFR', '')


def test_a_sister_shows_its_own_values_and_inherits_the_rest(ctx):
    form_class = sister_form(Toy, SETTINGS)
    form = form_of(form_class)
    load(form, form_class, SETTINGS, row(flag=False, mode='a', size_bytes=2048), sister=True)
    assert (form.flag.data, form.mode.data, form.language.data, form.size_mb.data, form.countries.data) == \
        (NO, 'a', None, 2, INHERIT)
    load(form, form_class, SETTINGS, row(flag=True), sister=True)
    assert form.flag.data == YES
    load(form, form_class, SETTINGS, None, sister=True)   # a sister that has set nothing
    assert (form.flag.data, form.size_mb.data, form.text.data) == (INHERIT, None, INHERIT)


def test_a_sisters_choices_name_the_first_domains_value(ctx):
    form_class = sister_form(Toy, SETTINGS)
    form = form_of(form_class)
    form.language.choices = select_choices([(1, 'English'), (2, 'French')])
    offer_inheritance(form, form_class, SETTINGS, {'flag': True, 'mode': 'b', 'language_id': 2, 'size_bytes': 3072})
    assert [(choice.value, choice.label) for choice in form.flag.choices] == \
        [(INHERIT, "The first domain's: Yes"), (YES, 'Yes'), (NO, 'No')]
    assert [(choice.value, choice.label) for choice in form.mode.choices] == \
        [(INHERIT, "The first domain's: Mode B"), ('a', 'Mode A'), ('b', 'Mode B')]
    assert form.language.choices[0].label == "The first domain's: French"
    assert form.size_mb.render_kw['placeholder'] == '3'
    offer_inheritance(form, form_class, SETTINGS, {'flag': False, 'size_bytes': None})
    assert form.flag.choices[0].label == "The first domain's: No" and form.size_mb.render_kw['placeholder'] == ''


def test_a_sisters_choice_of_the_first_domains_saves_null(ctx):
    form_class = sister_form(Toy, SETTINGS)
    form = form_of(form_class, {'flag': INHERIT, 'mode': INHERIT, 'language': INHERIT, 'size_mb': '',
                                'countries': '', 'text': '', 'wide': '1'})
    form.flag.choices = select_choices([(INHERIT, 'first'), (YES, 'Yes'), (NO, 'No')])
    form.mode.choices = select_choices([(INHERIT, 'first'), ('a', 'A'), ('b', 'B')])
    form.language.choices = select_choices([(INHERIT, 'first'), (1, 'English')])
    assert form.validate(), form.errors
    target = row(flag=True, mode='a', language_id=1, size_bytes=1024, countries='GB', text='mine')
    save(form, form_class, SETTINGS, target, sister=True)
    assert (target.flag, target.mode, target.language_id, target.size_bytes, target.countries, target.text) == \
        (None, None, None, None, '', '')


def test_a_sisters_own_values_save(ctx):
    form_class = sister_form(Toy, SETTINGS)
    form = form_of(form_class, {'flag': NO, 'mode': 'b', 'language': '1', 'size_mb': '3', 'countries': ' gb ',
                                'text': 'its own', 'wide': '1'})
    form.flag.choices = select_choices([(INHERIT, 'first'), (YES, 'Yes'), (NO, 'No')])
    form.mode.choices = select_choices([(INHERIT, 'first'), ('a', 'A'), ('b', 'B')])
    form.language.choices = select_choices([(INHERIT, 'first'), (1, 'English')])
    assert form.validate(), form.errors
    target = row()
    save(form, form_class, SETTINGS, target, sister=True)
    assert (target.flag, target.mode, target.language_id, target.size_bytes, target.countries, target.text) == \
        (False, 'b', 1, 3072, 'gb', 'its own')


def test_a_sisters_number_keeps_its_range(ctx):
    form_class = sister_form(Toy, SETTINGS)
    form = form_of(form_class, {'size_mb': '11'})
    form.validate()
    assert 'size_mb' in form.errors


def test_the_first_domains_save(ctx):
    form = form_of(Toy, {'flag': 'y', 'mode': 'a', 'language': '1', 'size_mb': '2', 'countries': 'fr\n', 'text': 'x'})
    form.language.choices = select_choices([(1, 'English')])
    target = row(size_bytes=99)
    save(form, Toy, SETTINGS, target, sister=False)
    assert (target.flag, target.mode, target.language_id, target.size_bytes, target.countries, target.text) == \
        (True, 'a', 1, 2048, 'fr', 'x')
    form = form_of(Toy, {'mode': 'a'})   # a box not posted is off; a number left out keeps the first domain's own
    save(form, Toy, SETTINGS, target, sister=False)
    assert (target.flag, target.size_bytes) == (False, 2048)


def test_instance_wide_fields_are_shown_on_a_sister_and_need_nothing_posted(ctx):
    """And a sister's choice left out of a post is the first domain's, as a blank field is."""
    form_class = sister_form(Toy, SETTINGS, shown_only=('wide',))
    form = form_of(form_class, {'text': 'posted'})
    form.flag.choices = select_choices([(INHERIT, 'first'), (YES, 'Yes'), (NO, 'No')])
    form.mode.choices = select_choices([(INHERIT, 'first'), ('a', 'A')])
    form.language.choices = select_choices([(INHERIT, 'first')])
    assert form.validate(), form.errors
    assert 'wide' not in form.errors and (form.flag.data, form.mode.data, form.language.data) == (INHERIT, None, None)


def test_fields_can_be_removed_and_added(ctx):
    form_class = sister_form(Toy, SETTINGS, removed=('gone',), added={'extra': BooleanField('Extra')})
    form = form_of(form_class)
    assert 'gone' not in form._fields and getattr(form, 'gone', None) is None and form.extra.label.text == 'Extra'
    assert form.flag.description == 'Whether' and form.flag.label.text == 'A flag'
