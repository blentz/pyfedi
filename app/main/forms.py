from flask_babel import lazy_gettext as _l
from flask_wtf import FlaskForm
from wtforms import SubmitField, TextAreaField
from wtforms.fields.choices import SelectField
from wtforms.validators import DataRequired, Length

# The quick post box on the "Create post" chooser: a short microblog post, with no title or other options.
QUICK_POST_MAX = 250


class ShareLinkForm(FlaskForm):
    which_community = SelectField(_l('Community to post this link to'), validators=[DataRequired()], coerce=int,
                                  render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Next'))


class QuickPostForm(FlaskForm):
    body = TextAreaField(_l('Quick post'), validators=[DataRequired(), Length(max=QUICK_POST_MAX)],
                         render_kw={'rows': 3, 'maxlength': QUICK_POST_MAX, 'class': 'form-control'})
    submit = SubmitField(_l('Post'))
