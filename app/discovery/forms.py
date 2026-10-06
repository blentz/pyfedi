"""Admin forms for discovery (interop D24). Field names are distinct across the page because
app.utils.login_required validates a bare `csrf_token`, which a prefixed form would rename."""
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import BooleanField, IntegerField, SelectMultipleField, SubmitField
from wtforms.validators import InputRequired, NumberRange
from wtforms.widgets import CheckboxInput, ListWidget


class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


class DiscoverySyncForm(FlaskForm):
    sync_per_host = IntegerField(_l('Channels and podcasts to keep synced per server'), default=0,
                                 validators=[InputRequired(), NumberRange(min=0, max=50)])
    sync_platforms = MultiCheckboxField(_l('Platforms'), default=['peertube', 'castopod'],
                                        choices=[('peertube', _l('PeerTube channels')),
                                                 ('castopod', _l('Castopod podcasts'))])
    sync_external_search = BooleanField(_l('Let people include PeerTube videos from the wider network in search '
                                           '(their search text is sent to sepiasearch.org)'), default=True)
    sync_save = SubmitField(_l('Save'))


class DiscoverySyncNowForm(FlaskForm):
    sync_now = SubmitField(_l('Sync now'))
