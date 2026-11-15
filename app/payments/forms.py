"""Forms on the members' payment pages."""
from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import StringField, SubmitField
from wtforms.validators import DataRequired, Length


class RedeemCodeForm(FlaskForm):
    """A gift code a member adds (paid membership G4)."""
    code = StringField(_l('Gift code'), validators=[DataRequired(), Length(max=64)], render_kw={'autocomplete': 'off'})
    redeem = SubmitField(_l('Redeem'))
