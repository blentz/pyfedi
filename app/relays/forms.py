"""Admin forms for relays. Field names are unique across the page because app.utils.login_required validates a
bare `csrf_token`, which a prefixed form would rename (see app/discovery/forms.py)."""
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import HiddenField, IntegerField, StringField, SubmitField
from wtforms.validators import DataRequired, InputRequired, NumberRange, URL


class RelayAddForm(FlaskForm):
    relay_url = StringField(_l('Relay URL (inbox or actor)'), validators=[DataRequired(), URL(require_tld=True)])
    relay_add = SubmitField(_l('Subscribe'))


class RelayActionForm(FlaskForm):
    relay_id = HiddenField(validators=[DataRequired()])
    relay_retry = SubmitField(_l('Retry'))
    relay_remove = SubmitField(_l('Remove'))


class RelaySettingsForm(FlaskForm):
    relay_retention = IntegerField(_l('Days to keep relayed posts nobody here engaged with (0 keeps them)'),
                                   validators=[InputRequired(), NumberRange(min=0, max=365)])
    relay_settings_save = SubmitField(_l('Save'))
