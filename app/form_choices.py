"""Choices for WTForms select fields. WTForms 3.3 deprecates (value, label) tuples and dicts of option groups, and 3.4
removes them, so every choice list a form declares or assigns goes through select_choices. The lists themselves stay
(value, label) pairs: other code reads them as pairs."""
from wtforms.fields.choices import SelectChoice


def select_choices(choices) -> list[SelectChoice]:
    """`choices` as SelectChoice: each a (value, label[, render_kw]) pair, a bare value, or a SelectChoice already;
    a dict maps an option group's name to such a list."""
    if isinstance(choices, dict):
        return [_choice(choice, optgroup) for optgroup, group in choices.items() for choice in group]
    return [_choice(choice) for choice in choices]


def _choice(choice, optgroup=None) -> SelectChoice:
    if isinstance(choice, SelectChoice):
        return choice
    if isinstance(choice, (tuple, list)):
        value, label, *rest = choice
        return SelectChoice(value, label, *rest, optgroup=optgroup)
    return SelectChoice(choice, optgroup=optgroup)
