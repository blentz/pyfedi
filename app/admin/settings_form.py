"""A settings page's fields for one domain (spec 2026-11-20-multitenant-domain-config-design.md, MT3 and MT4).

Each `Setting` names a form field and the `domain_config` column it edits. The first domain's page shows its row's
values as the form declares its fields. A sister domain's page shows the same form with each field able to say "the
first domain's": a yes/no box becomes a choice of the first domain's, yes or no; a choice gains the first domain's; a
number may be left blank; text left blank is the first domain's already (MT3). Saving a sister's "the first domain's"
stores NULL, so the first domain's value applies, and goes on applying when the first domain's changes.
"""
from __future__ import annotations

from typing import NamedTuple

from flask_babel import _
from wtforms import BooleanField, IntegerField, SelectField
from wtforms.validators import NumberRange, Optional

from app.form_choices import select_choices

INHERIT = ''   # a sister's choice of the first domain's value
YES, NO = 'y', 'n'


class Setting(NamedTuple):
    field: str               # the form field
    column: str = ''         # the domain_config column it edits: the field's name when blank
    scale: int = 1           # the column holds the page's value times this: megabytes on the page, bytes stored
    upper: bool = False      # country codes: shown in capitals, stored as typed with the ends trimmed

    @property
    def name(self) -> str:
        return self.column or self.field


def _kind(form_class, field: str) -> str:
    """What `field` is on the first domain's form: a sister's form remembers it (its yes/no boxes are choices there)."""
    kinds = getattr(form_class, 'setting_kinds', {})
    if field in kinds:
        return kinds[field]
    field_class = getattr(form_class, field).field_class
    if issubclass(field_class, BooleanField):
        return 'bool'
    if issubclass(field_class, SelectField):
        return 'choice'
    if issubclass(field_class, IntegerField):
        return 'number'
    return 'text'


def _blank_is_none(coerce):
    def coerced(value):
        return None if value in (INHERIT, None) else coerce(value)
    return coerced


def _keyword(unbound, name, default=None):
    return unbound.kwargs.get(name, default)


def _label(unbound):
    return unbound.args[0] if unbound.args else unbound.kwargs.get('label')


def sister_form(form_class, settings: list[Setting], shown_only=(), removed=(), added=None) -> type:
    """`form_class` as a sister domain's page has it: the unbound fields `added` (by name) put in; each of `settings`
    able to say "the first domain's"; each field of `shown_only` (one the page shows and does not save, such as an
    instance-wide one) without its validators, as a disabled field is not posted; `removed` taken away."""
    fields = dict(added or {})
    for setting in settings:
        unbound = getattr(form_class, setting.field)
        kind = _kind(form_class, setting.field)
        description = _keyword(unbound, 'description', '')
        if kind == 'bool':
            fields[setting.field] = SelectField(_label(unbound), choices=[], description=description, default=INHERIT,
                                                render_kw={'class': 'form-select'})
        elif kind == 'choice':
            fields[setting.field] = SelectField(_label(unbound), choices=_keyword(unbound, 'choices', []),
                                                description=description, default=INHERIT,
                                                coerce=_blank_is_none(_keyword(unbound, 'coerce', str)),
                                                validate_choice=_keyword(unbound, 'validate_choice', True),
                                                render_kw=_keyword(unbound, 'render_kw', {'class': 'form-select'}))
        elif kind == 'number':
            ranges = [validator for validator in _keyword(unbound, 'validators', []) if isinstance(validator, NumberRange)]
            fields[setting.field] = IntegerField(_label(unbound), validators=[Optional(), *ranges],
                                                 description=description)
    for name in shown_only:
        unbound = fields.get(name) or getattr(form_class, name)
        unchecked = {'validate_choice': False} if issubclass(unbound.field_class, SelectField) else {}
        fields[name] = unbound.field_class(*unbound.args, **{**unbound.kwargs, 'validators': [Optional()], **unchecked})
    for name in removed:
        fields[name] = None
    fields['setting_kinds'] = {setting.field: _kind(form_class, setting.field) for setting in settings}
    return type(f'Sister{form_class.__name__}', (form_class,), fields)


def _shown(setting: Setting, kind: str, value):
    """Column value `value` as the first domain's page shows it."""
    if kind == 'bool':
        return bool(value)
    if kind == 'number':
        return None if value is None else value // setting.scale
    if kind == 'text':
        return (value or '').upper() if setting.upper else (value or '')
    return value


def offer_inheritance(form, form_class, settings: list[Setting], first: dict) -> None:
    """A sister's choices: the first domain's value, named, then each choice the field has (a yes/no box: yes, no).
    `first` is the first domain's configuration. A number shows the first domain's as its placeholder."""
    for setting in settings:
        field, kind = getattr(form, setting.field), _kind(form_class, setting.field)
        value = _shown(setting, kind, first.get(setting.name))
        if kind == 'bool':
            field.choices = select_choices([(INHERIT, _("The first domain's: %(value)s", value=_('Yes') if value else _('No'))),
                                            (YES, _('Yes')), (NO, _('No'))])
        elif kind == 'choice':
            labels = dict((str(choice.value), choice.label) for choice in field.choices) if field.choices else {}
            label = labels.get(str(value), value)
            field.choices = select_choices([(INHERIT, _("The first domain's: %(value)s", value=label))]) + \
                list(field.choices or [])
        elif kind == 'number':
            field.render_kw = {**(field.render_kw or {}), 'placeholder': '' if value is None else str(value)}


def load(form, form_class, settings: list[Setting], stored, sister: bool, defaults: dict | None = None) -> None:
    """Fill `form` from `stored`, the domain's own row (None: a sister that has set nothing). On the first domain a
    NULL shows the value it stands for, from `defaults`: a box shown unticked for a setting in effect would turn it
    off on the next save (D1398)."""
    for setting in settings:
        kind = _kind(form_class, setting.field)
        value = getattr(stored, setting.name) if stored is not None else None
        field = getattr(form, setting.field)
        if not sister:
            field.data = _shown(setting, kind, (defaults or {}).get(setting.name) if value is None else value)
        elif value is None or value == '':
            field.data = INHERIT if kind in ('bool', 'text') else None   # a choice's INHERIT is coerced to None
        elif kind == 'bool':
            field.data = YES if value else NO
        else:
            field.data = _shown(setting, kind, value)


def save(form, form_class, settings: list[Setting], target, sister: bool) -> None:
    """Write `form`'s values for `settings` to `target`, the domain's row. A sister's "the first domain's" is NULL. A
    number left blank keeps the first domain's own (as its page always has), and is the first domain's on a sister."""
    for setting in settings:
        kind = _kind(form_class, setting.field)
        data = getattr(form, setting.field).data
        if kind == 'bool':
            value = {YES: True, NO: False}.get(data) if sister else bool(data)
        elif kind == 'number':
            if data is None and not sister:
                continue
            value = None if data is None else data * setting.scale
        elif kind == 'choice':
            value = None if data in (INHERIT, None) and sister else data
        else:
            value = (data or '').strip() if setting.upper else (data or '')
        setattr(target, setting.name, value)
