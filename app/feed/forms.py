from flask_babel import lazy_gettext as _l
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, SelectField, FileField
from wtforms.validators import DataRequired, Length, Optional

from app.models import Community, Feed, User
# The module, not the names: app.shared.feed reaches this file through a blueprint
# package before they are defined (import cycle: app.shared.feed)
import app.shared.feed as shared_feed
from app.utils import apply_feed_url_rules


class AddCopyFeedForm(FlaskForm):
    title = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[Length(max=30)])
    description = TextAreaField(_l('Description'))
    parent_feed_id = SelectField(_l('Parent feed'), coerce=int, validators=[Optional()],
                                 render_kw={'class': 'form-select'})
    show_child_posts = BooleanField(_l('Show posts from child feeds'))
    communities = TextAreaField(_l('Communities'), validators=[DataRequired()], render_kw={'rows': 5})
    icon_file = FileField(_l('Icon image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Banner image'), render_kw={'accept': 'image/*'})
    nsfw = BooleanField('NSFW')
    nsfl = BooleanField('NSFL')
    public = BooleanField(_l('Public'), default=True)
    is_instance_feed = BooleanField(_l('Add to main menu'))
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        # url.data is None when the input is absent or disabled, not '' -- the
        # same case EditFeedForm.validate guards below, and calling .strip() on
        # it made a POST without the field a 500 rather than a form error.
        if self.url.data is None or self.url.data.strip() == '':
            self.url.errors.append(_l('Url is required.'))
            return False
        else:
            if not apply_feed_url_rules(self):
                return False
            # apply_feed_url_rules compares the url as typed, but make_feed stores it
            # slugified, so 'afeed_' passed there and collided with 'afeed' (D681)
            name = shared_feed.feed_machine_name(self.url.data, self.public.data, current_user)
            if Feed.query.filter(Feed.name == name).first() is not None:
                self.url.errors.append(_l('A Feed with this url already exists.'))
                return False
            community = Community.query.filter(Community.name == self.url.data.strip().lower(),
                                               Community.ap_id == None).first()
            if community is not None:
                self.url.errors.append(_l('A community with this url already exists.'))
                return False
            user = User.query.filter(func.lower(User.user_name) == func.lower(self.url.data.strip())).filter_by(
                ap_id=None).first()
            if user is not None:
                if user.deleted:
                    self.url.errors.append(_l('This name was used in the past and cannot be reused.'))
                else:
                    self.url.errors.append(_l('This name is in use already.'))
                return False

        input_communities = self.communities.data.strip().split('\n')
        for community_ap_id in input_communities:
            if not community_ap_id.strip():
                continue

            if not '@' in community_ap_id:
                self.communities.errors.append(
                    _l('Please make sure each community is formatted as "community_name@instance.tld"'))
                return False
        return True


class EditFeedForm(FlaskForm):
    feed_id = 0
    title = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[Length(max=50)])
    description = TextAreaField(_l('Description'))
    parent_feed_id = SelectField(_l('Parent feed'), coerce=int, validators=[Optional()],
                                 render_kw={'class': 'form-select'})
    show_child_posts = BooleanField(_l('Show posts from child feeds'))
    communities = TextAreaField(_l('Communities'), validators=[DataRequired()], render_kw={'rows': 5})
    icon_file = FileField(_l('Icon image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Banner image'), render_kw={'accept': 'image/*'})
    nsfw = BooleanField('NSFW')
    nsfl = BooleanField('NSFL')
    public = BooleanField(_l('Public'))
    is_instance_feed = BooleanField(_l('Add to main menu'))
    submit = SubmitField(_l('Save'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        if self.url.data is not None:  # when editing a feed with subscribers this field is disabled
            if self.url.data.strip() == '':
                self.url.errors.append(_l('Url is required.'))  # D740: the create form's message
                return False
            else:
                if not apply_feed_url_rules(self):
                    return False

        input_communities = self.communities.data.strip().split('\n')
        for community_ap_id in input_communities:
            if not community_ap_id.strip():
                continue

            if not '@' in community_ap_id:
                self.communities.errors.append(_l('Please make sure each community is formatted as "community_name@instance.tld"'))
                return False
        return True


class SearchRemoteFeed(FlaskForm):
    address = StringField(_l('Feed address'),
                          render_kw={'placeholder': 'e.g. https://server.name/f/feedname', 'autofocus': True},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Search'))
