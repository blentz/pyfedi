"""Admin forms for discovery (interop D24). Field names are distinct across the page because
app.utils.login_required validates a bare `csrf_token`, which a prefixed form would rename."""
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import PasswordField, SubmitField
from wtforms.validators import Length, Optional


class PodcastIndexCredentialsForm(FlaskForm):
    # PasswordField never renders its value back, which is what makes these write-only
    podcastindex_api_key = PasswordField(_l('Podcast Index API key'), validators=[Optional(), Length(max=128)])
    podcastindex_api_secret = PasswordField(_l('Podcast Index API secret'), validators=[Optional(), Length(max=128)])
    podcastindex_save = SubmitField(_l('Save credentials'))
    podcastindex_remove = SubmitField(_l('Remove credentials'))
