"""app.form_choices.select_choices: WTForms 3.3 deprecates (value, label) tuples and dicts of option groups as choices,
and 3.4 removes them. The helper turns each shape the app declares into SelectChoice, and a field given its result
renders exactly what the same field rendered from the tuples."""
import warnings
from collections import OrderedDict

import pytest
from wtforms import Form, SelectField
from wtforms.fields.choices import SelectChoice

from app.form_choices import select_choices


def test_a_pair_becomes_its_value_and_label():
    assert select_choices([(1, 'One'), ('b', 'Bee')]) == [SelectChoice(1, 'One'), SelectChoice('b', 'Bee')]


def test_a_third_item_is_the_options_render_kw():
    assert select_choices([('x', 'X', {'disabled': True})]) == [SelectChoice('x', 'X', {'disabled': True})]


def test_a_bare_value_is_its_own_label_and_a_selectchoice_is_kept():
    kept = SelectChoice('k', 'Kay')
    assert select_choices(['plain', kept]) == [SelectChoice('plain', 'plain'), kept]
    assert select_choices(['plain', kept])[1] is kept


def test_a_dict_of_groups_becomes_options_in_their_optgroups():
    zones = OrderedDict([('Africa', [('Africa/Abidjan', 'Abidjan')]), ('Europe', [('Europe/London', 'London')])])
    assert select_choices(zones) == [SelectChoice('Africa/Abidjan', 'Abidjan', optgroup='Africa'),
                                     SelectChoice('Europe/London', 'London', optgroup='Europe')]


@pytest.mark.parametrize('choices', [
    [(1, 'One'), (2, 'Two')],
    OrderedDict([('Africa', [('Africa/Abidjan', 'Abidjan')]), ('Europe', [('Europe/London', 'London')])]),
])
def test_a_field_renders_and_validates_as_it_did_from_the_tuples(choices):
    coerce = int if isinstance(choices, list) else str
    value = '2' if coerce is int else 'Europe/London'

    class Legacy(Form):
        pick = SelectField(coerce=coerce)

    class Current(Form):
        pick = SelectField(coerce=coerce)

    with warnings.catch_warnings():
        warnings.simplefilter('ignore', DeprecationWarning)   # the legacy shapes are what is compared against
        legacy = Legacy(data={'pick': coerce(value)})
        legacy.pick.choices = choices
        legacy_html = str(legacy.pick)
    current = Current(data={'pick': coerce(value)})
    current.pick.choices = select_choices(choices)

    assert str(current.pick) == legacy_html
    assert current.validate() and current.pick.data == coerce(value)
