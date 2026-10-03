"""Admin forms for discovery (interop D24). Field names are distinct across the page because
app.utils.login_required validates a bare `csrf_token`, which a prefixed form would rename."""
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import IntegerField, PasswordField, SelectMultipleField, SubmitField
from wtforms.validators import Length, NumberRange, Optional
from wtforms.widgets import CheckboxInput, ListWidget


class PodcastIndexCredentialsForm(FlaskForm):
    # PasswordField never renders its value back, which is what makes these write-only
    podcastindex_api_key = PasswordField(_l('Podcast Index API key'), validators=[Optional(), Length(max=128)])
    podcastindex_api_secret = PasswordField(_l('Podcast Index API secret'), validators=[Optional(), Length(max=128)])
    podcastindex_save = SubmitField(_l('Save credentials'))
    podcastindex_remove = SubmitField(_l('Remove credentials'))


class MultiCheckboxField(SelectMultipleField):
    widget = ListWidget(prefix_label=False)
    option_widget = CheckboxInput()


class DiscoveryPreloadForm(FlaskForm):
    preload_count = IntegerField(_l('How many to subscribe to'), default=25,
                                 validators=[NumberRange(min=1, max=200)])
    preload_platforms = MultiCheckboxField(_l('Platforms'), default=['peertube', 'castopod'],
                                           choices=[('peertube', _l('PeerTube channels')),
                                                    ('castopod', _l('Castopod podcasts'))])
    preload_preview = SubmitField(_l('Preview'))
    preload_subscribe = SubmitField(_l('Subscribe'))
