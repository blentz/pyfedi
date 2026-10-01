import re
from io import BytesIO
from zoneinfo import ZoneInfo

import pytesseract
from PIL import Image, UnidentifiedImageError
from flask import request, g
from flask_babel import _, lazy_gettext as _l
from slugify import slugify
from flask_login import current_user
from flask_wtf import FlaskForm
from sqlalchemy import func
from wtforms import StringField, SubmitField, TextAreaField, BooleanField, HiddenField, SelectField, FileField, \
    DateField, IntegerField, DateTimeLocalField, RadioField

from wtforms.validators import ValidationError, DataRequired, Length, Regexp, Optional, URL

from app import db
from app.constants import DOWNVOTE_ACCEPT_ALL, DOWNVOTE_ACCEPT_MEMBERS, DOWNVOTE_ACCEPT_INSTANCE, \
    DOWNVOTE_ACCEPT_TRUSTED, DOWNVOTE_ACCEPT_NONE
from app.models import Community, Site, utcnow, User, Feed
from app.utils import domain_from_url, MultiCheckboxField, get_timezones, url_is_parseable

MAX_IMAGE_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB


def image_upload_too_large(field, uploaded_file) -> bool:
    # R207: the one size check every image upload form shares. Records the error on `field`.
    uploaded_file.seek(0)
    too_large = len(uploaded_file.read()) > MAX_IMAGE_UPLOAD_BYTES
    uploaded_file.seek(0)
    if too_large:
        error_message = "This image filesize is too large."
        if not isinstance(field.errors, list):
            field.errors = [error_message]
        else:
            field.errors.append(error_message)
    return too_large


class AddCommunityForm(FlaskForm):
    community_name = StringField(_l('Name'), validators=[DataRequired()])
    url = StringField(_l('Url'), validators=[Length(max=50)])
    description = TextAreaField(_l('Description'), validators=[Length(max=10000)])
    posting_warning = StringField(_l('Posting warning'), validators=[Length(max=512)])
    icon_file = FileField(_l('Icon image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Banner image'), render_kw={'accept': 'image/*'})
    theme = SelectField(_l('Community theme'), coerce=str, render_kw={'class': 'form-select'})
    nsfw = BooleanField(_l('NSFW'))
    nsfl = BooleanField(_l('NSFL'))  # R203
    ai_generated = BooleanField(_l('Only AI-generated content'))
    local_only = BooleanField(_l('Local only'))
    private = BooleanField(_l('Private'))
    joining_options = [
        (0, _l('Anyone can join')),
        #(1, _l('Apply to join')),
        (2, _l('Invitation by a member required')),
        (3, _l('Invitation by moderator required')),
        (4, _l('Invitation by owner required')),
    ]
    invitations = SelectField(_l('Joining process'), coerce=int, choices=joining_options)
    publicize = BooleanField(_l('Announce this community to newcommunities@lemmy.world'))
    question_answer = BooleanField(_l('Question & answer community'))
    languages = MultiCheckboxField(_l('Languages'), coerce=int, validators=[Optional()],
                                   render_kw={'class': 'form-multicheck-columns'})
    submit = SubmitField(_l('Create'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        # The '/c/' prefix is stripped HERE, not in add_local. People paste the
        # path rather than the name, and the strip used to run in the route
        # after validation -- so every check below saw '/c/whatever' while the
        # stored value was 'whatever'.
        if self.url.data.strip().lower().startswith('/c/'):
            self.url.data = self.url.data.strip()[3:]

        if self.url.data.strip() == '':
            self.url.errors.append(_l('Url is required.'))
            return False
        else:
            if '-' in self.url.data.strip():
                self.url.errors.append(_l('- cannot be in Url. Use _ instead?'))
                return False

            # Allow alphanumeric characters and underscores (a-z, A-Z, 0-9, _)
            # D1402, the third site of the same one character: `$` matches before a
            # trailing newline, so 'books\n' satisfied this. The normalisation below
            # slugifies it away before storage, so nothing was stored wrong -- but the
            # guard is what the message claims it is only with `fullmatch`.
            if not re.fullmatch(r'^[a-zA-Z0-9_]+$', self.url.data):
                self.url.errors.append(_l('Community urls can only contain letters, numbers, and underscores.'))
                return False

            # Normalise to the exact value add_local will store, AFTER the
            # character rules (which must see what was typed, or a hyphen would
            # be silently turned into an underscore rather than reported) and
            # BEFORE the uniqueness checks below.
            #
            # add_local used to slugify after validation, and slugify is not
            # the identity on strings this validator accepts: '__general__'
            # became 'general' and '___' became ''. So a submission that passed
            # the uniqueness check could collide on INSERT --
            # `UniqueViolation ... ix_community_ap_profile_id`, an unhandled
            # 500 -- and the friendly "already exists" error never fired,
            # because it had been asked about a different string.
            self.url.data = slugify(self.url.data.strip(), separator='_').lower()
            if self.url.data == '':
                self.url.errors.append(_l('Url is required.'))
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
            feed = Feed.query.filter(Feed.name == self.url.data.strip().lower(), Feed.ap_id == None).first()
            if feed is not None:
                self.url.errors.append(_('This name is in use already.'))
                return False
        return True


class EditCommunityForm(FlaskForm):
    title = StringField(_l('Title'), validators=[DataRequired()])
    description = TextAreaField(_l('Description'), validators=[Length(max=10000)])
    posting_warning = StringField(_l('Posting warning'), validators=[Length(max=512)])
    icon_file = FileField(_l('Icon image'), render_kw={'accept': 'image/*'})
    banner_file = FileField(_l('Banner image'), render_kw={'accept': 'image/*'})
    theme = SelectField(_l('Community theme'), coerce=str, render_kw={'class': 'form-select'})
    nsfw = BooleanField(_l('NSFW community'))
    nsfl = BooleanField(_l('NSFL community'))  # R203
    ai_generated = BooleanField('Only AI-generated content')
    local_only = BooleanField(_l('Only accept posts from current instance'))
    private = BooleanField(_l('Private'))
    joining_options = [
        (0, _l('Anyone can join')),
        #(1, _l('Apply to join')),
        (2, _l('Invitation by a member required')),
        (3, _l('Invitation by moderator required')),
        (4, _l('Invitation by owner required')),
    ]
    invitations = SelectField(_l('Joining process'), coerce=int, choices=joining_options)
    question_answer = BooleanField(_l('Question & answer community'))
    restricted_to_mods = BooleanField(_l('Only moderators can post'))
    new_mods_wanted = BooleanField(_l('New moderators wanted'))
    downvote_accept_modes = [(DOWNVOTE_ACCEPT_ALL, _l('Everyone')),
                             (DOWNVOTE_ACCEPT_NONE, _l('Nobody')),
                             (DOWNVOTE_ACCEPT_MEMBERS, _l('Community members')),
                             (DOWNVOTE_ACCEPT_INSTANCE, _l('This instance')),
                             (DOWNVOTE_ACCEPT_TRUSTED, _l('Trusted instances')),

                             ]
    downvote_accept_mode = SelectField(_l('Accept downvotes from'), coerce=int, choices=downvote_accept_modes,
                                       validators=[Optional()], render_kw={'class': 'form-select'})
    topic = SelectField(_l('Topic'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    languages = MultiCheckboxField(_l('Languages'), coerce=int, validators=[Optional()],
                                   render_kw={'class': 'form-multicheck-columns'})
    layouts = [('', _l('List')),
               ('masonry', _l('Masonry')),
               ('masonry_wide', _l('Wide masonry'))]
    default_layout = SelectField(_l('Layout'), coerce=str, choices=layouts, validators=[Optional()],
                                 render_kw={'class': 'form-select'})
    post_types = [('link', _l('Link')),
                  ('discussion', _l('Discussion')),
                  ('image', _l('Image')),
                  ('video', _l('Video')),
                  ('poll', _l('Poll')),
                  ('event', _l('Event')),
                 ]
    default_post_type = SelectField(_l('Default post type'), coerce=str, choices=post_types, validators=[Optional()],
                                 render_kw={'class': 'form-select'})
    url_types = [('friendly', _l('Friendly urls')),
                 ('post_id', _l('Post ids only'))]
    post_url_type = SelectField(_l('Post url structure to use in this community'), choices=url_types, coerce=str,
                                validators=[Optional()], render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))


class EditCommunityWikiPageForm(FlaskForm):
    title = StringField(_l('Title'), validators=[DataRequired()])
    slug = StringField(_l('Slug'), validators=[DataRequired()])
    body = TextAreaField(_l('Body'), render_kw={'rows': '10'})
    edit_options = [(0, _l('Mods and admins')),
                    (1, _l('Trusted accounts')),
                    (2, _l('Community members')),
                    (3, _l('Any account'))
                    ]
    who_can_edit = SelectField(_l('Who can edit'), coerce=int, choices=edit_options, validators=[Optional()],
                               render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))


class AddModeratorForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()])
    submit = SubmitField(_l('Find'))


class EscalateReportForm(FlaskForm):
    reason = StringField(_l('Amend the report description if necessary'), validators=[DataRequired()])
    submit = SubmitField(_l('Escalate report'))


class ResolveReportForm(FlaskForm):
    note = StringField(_l('Note for mod log'), validators=[Optional()])
    also_resolve_others = BooleanField(_l('Also resolve all other reports about the same thing.'), default=True)
    submit = SubmitField(_l('Resolve report'))


class SearchRemoteCommunity(FlaskForm):
    address = StringField(_l('Community address'),
                          render_kw={'placeholder': 'e.g. !name@server', 'autofocus': True, 'autocomplete': 'off'},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Search'))

    def validate(self, extra_validators=None):
        if not super().validate():
            return False
        # `if self.address.data.strip() == '':` used to stand here. The field
        # carries DataRequired(), which fails on whitespace-only input inside
        # super().validate() above, so that arm could never run.
        if self.address.data.strip().startswith('https://') or self.address.data.strip().startswith('http://'):
            return True
        else:
            if not self.address.data.strip().startswith('!'):
                self.address.errors.append(_l('Address must start with !'))
                return False
            elif '@' not in self.address.data.strip():
                self.address.errors.append(_l('Address must include @'))
                return False
            elif '/' in self.address.data.strip():
                self.address.errors.append(_l('/ cannot be in address'))
                return False
        
        return True


class BanUserCommunityForm(FlaskForm):
    reason = StringField(_l('Reason'), render_kw={'autofocus': True}, validators=[DataRequired()])
    ban_until = DateField(_l('Ban until'), validators=[Optional()])
    delete_posts = BooleanField(_l('Also delete all their posts'))
    delete_post_replies = BooleanField(_l('Also delete all their comments'))
    submit = SubmitField(_l('Ban'))


class FindAndBanUserCommunityForm(FlaskForm):
    user_name = StringField(_l('User name'), validators=[DataRequired()])
    submit = SubmitField(_l('Find'))


class CreatePostForm(FlaskForm):
    communities = SelectField(_l('Community'), validators=[DataRequired()], coerce=int,
                              render_kw={'class': 'form-select',
                                         'hx-get': '/community/community_changed',
                                         'hx-params': '*',
                                         'hx-target': '#communityFlair'})
    title = StringField(_l('Title'), validators=[DataRequired(), Length(min=3, max=255)])
    body = TextAreaField(_l('Body'), validators=[Optional(), Length(min=3, max=50000)], render_kw={'rows': 5, 'class': 'autoresize'})
    tags = StringField(_l('Tags'), validators=[Optional(), Length(min=2, max=5000)])
    flair = MultiCheckboxField(_l('Flair'), coerce=int, validators=[Optional()],
                               render_kw={'class': 'form-multicheck-columns'})
    sticky = BooleanField(_l('Sticky'))
    nsfw = BooleanField(_l('NSFW'))
    nsfl = BooleanField(_l('Gore/gross'))
    ai_generated = BooleanField(_l('AI generated'))
    notify_author = BooleanField(_l('Notify about replies'))
    language_id = SelectField(_l('Language'), validators=[DataRequired()], coerce=int,
                              render_kw={'class': 'form-select'})
    scheduled_for = DateTimeLocalField(_l('Publish at'), validators=[Optional()], format="%Y-%m-%dT%H:%M")
    repeat = SelectField(_l('Repeat'), validators=[Optional()],
                         choices=[('none', _l('None')), ('daily', _l('Daily')),
                                  ('weekly', _l('Weekly')), ('monthly', _l('Monthly'))],
                         render_kw={'class': 'form-select'})
    timezone = SelectField(_l('Timezone'), validators=[DataRequired()], render_kw={'id': 'timezone', "class": "form-control"})
    submit = SubmitField(_l('Publish'))

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.timezone.choices = get_timezones()

    def validate_nsfw(self, field):
        if g.site.enable_nsfw is False:
            if field.data:
                self.nsfw.errors.append(_l('NSFW posts are not allowed.'))
                return False
        return True

    def validate_nsfl(self, field):
        if g.site.enable_nsfl is False:
            if field.data:
                self.nsfl.errors.append(_l('NSFL posts are not allowed.'))
                return False
        return True

    def validate_scheduled_for(self, field):
        if field.data:
            date_with_tz = field.data.replace(tzinfo=ZoneInfo(self.timezone.data))
            if date_with_tz.astimezone(ZoneInfo('UTC')) < utcnow(naive=False):
                self.scheduled_for.errors.append(_l('Choose a time in the future.'))
                return False
        return True

    def filter_title(self, title):
        if isinstance(title, str):
            title = title.strip()

        return title

class CreateDiscussionForm(CreatePostForm):
    pass


class SubmittedUrlMixin:
    """The one place a submitted URL is checked before it can be stored.

    Two checks, in this order, and the order is the whole point:

    1. Can urlparse() read it at all? `^https?://` says nothing about the
       authority, so 'https://youtube.com[abc' passes it and then makes
       urlparse raise ValueError('Invalid IPv6 URL').
    2. Is its domain banned? -- the check this hook has always performed.

    The parseability check has to come FIRST, and cannot be folded into the
    domain lookup, because domain_from_url(create=False) returns None for two
    different reasons: "this URL has no host urlparse could find" and "this
    host has no Domain row yet". Before the ValueError guards landed, the
    unparseable case RAISED out of domain_from_url -- a 500 at submission, but
    the URL never reached the database. With the guard it returns None,
    `if domain and domain.banned` is False, and the URL is accepted and stored.
    This restores the refusal without restoring the crash.

    Extracted rather than fixed twice: this body was duplicated verbatim in
    CreateLinkForm and CreateEventForm, and a duplicated fix is how one copy
    drifts later.

    `banned_message` is a callable taking the domain name, because the two
    call sites word that message differently ("Links to X" / "Videos from X")
    and lazy_gettext's interpolation has to happen at the call site to stay
    translatable.
    """

    def url_field_is_acceptable(self, field, banned_message) -> bool:
        if field.data and not url_is_parseable(field.data):
            field.errors.append(_l('This URL could not be understood. Please check it and try again.'))
            return False
        domain = domain_from_url(field.data, create=False)
        if domain and domain.banned:
            field.errors.append(banned_message(domain.name))
            return False
        return True


class CreateLinkForm(SubmittedUrlMixin, CreatePostForm):
    link_url = StringField(_l('URL'), validators=[DataRequired(), Regexp(r'^https?://', message='Submitted links need to start with "http://"" or "https://"')],
                           render_kw={'placeholder': 'https://...',
                                      'hx-get': '/community/check_url_already_posted',
                                      'hx-params': '*',
                                      'hx-target': '#urlUsed'})
    image_alt_text = StringField(_l('Alt text (for links to images)'), validators=[Optional(), Length(min=3, max=1500)])

    def validate_link_url(self, field):
        return self.url_field_is_acceptable(
            field, lambda name: _l("Links to %(domain)s are not allowed.", domain=name))


class CreateVideoForm(SubmittedUrlMixin, CreatePostForm):
    video_url = StringField(_l('URL'), validators=[Regexp(r'^https?://', message='Submitted links need to start with "http://"" or "https://"')],
                            render_kw={'placeholder': 'https://...'})
    image_file = FileField(_l('Video file (mp4, webm or mov)'), render_kw={'accept': 'video/mp4,video/webm,video/quicktime'})    # do not change from image_file even though this is a video

    def validate(self, extra_validators=None) -> bool:
        if not super().validate(extra_validators):
            return False

        # video_url has no validate_video_url hook of its own; its domain-ban
        # check lives here inline. It used to have to: the guard above was
        # missing, super()'s result was discarded, and an inline validator's
        # rejection would have been discarded with it. That is fixed, so this
        # check could now move into a validate_video_url hook and let the
        # override go -- left in place because moving it changes when the
        # error is reported (a hook runs even when another field fails; this
        # does not), and that is a separate decision.
        return self.url_field_is_acceptable(
            self.video_url, lambda name: _l("Videos from %(domain)s are not allowed.", domain=name))


class CreateImageForm(CreatePostForm):
    image_alt_text = StringField(_l('Alt text'), validators=[Optional(), Length(min=3, max=1500)])
    image_file = FileField(_l('Image'), validators=[DataRequired()], render_kw={'accept': 'image/*'})

    def validate(self, extra_validators=None) -> bool:
        if not super().validate(extra_validators):
            return False

        # `.get`, not `['image_file']`. This form's own field carries
        # DataRequired(), so the key is always there on a create -- but
        # EditImageForm overrides the field to Optional() and inherits this
        # method, so an edit that keeps the existing image reached
        # `request.files['image_file']` and was
        # `werkzeug.exceptions.BadRequestKeyError: 400`.
        uploaded_file = request.files.get('image_file')
        if uploaded_file and uploaded_file.filename != '' and not uploaded_file.filename.endswith('.svg') and not uploaded_file.filename.endswith('.gif'):
            site = db.session.get(Site, 1)
            if site is None:
                site = Site()

            if site.enable_chan_image_filter:
                # Do not allow fascist meme content
                try:
                    if '.avif' in uploaded_file.filename:
                        import pillow_avif  # NOQA  # lazy: registers Pillow's AVIF plugin only on the AVIF path
                    image_text = pytesseract.image_to_string(Image.open(BytesIO(uploaded_file.read())).convert('L'))
                except FileNotFoundError:
                    image_text = ''
                except UnidentifiedImageError:
                    image_text = ''
                except pytesseract.TesseractNotFoundError:
                    # D1415. `TesseractNotFoundError` is an `OSError` but NOT a
                    # `FileNotFoundError` (its MRO is TesseractNotFoundError -> OSError),
                    # so neither arm above caught it: pytesseract raises it when the
                    # tesseract BINARY is absent, which is the ordinary state of a machine
                    # that installed this application's Python dependencies and nothing
                    # else. An admin turning `enable_chan_image_filter` on there made every
                    # image upload raise out of form validation -- a 500 on the post form,
                    # for every image, until the setting was turned off again.
                    #
                    # '' is what the other two arms already answer for "the OCR could not
                    # run", and the check below treats an empty string as "no chan markers
                    # found", so the filter degrades to off rather than to broken.
                    image_text = ''

                if 'Anonymous' in image_text and ('No.' in image_text or ' N0' in image_text):  # chan posts usually contain the text 'Anonymous' and ' No.12345'
                    self.image_file.errors.append("This image is from 4chan.")
                    db.session.commit()
                    return False
        if uploaded_file and image_upload_too_large(self.image_file, uploaded_file):
            return False
        if self.communities:
            community = db.session.get(Community, self.communities.data)
            if community.is_local() and g.site.allow_local_image_posts is False:
                # D1001. This appended the error and then returned True, so
                # `allow_local_image_posts = False` recorded a complaint and
                # accepted the image anyway. Measured: with the setting off,
                # an image post to a local community still reached make_post.
                self.communities.errors.append(_l('Images cannot be posted to local communities.'))
                return False

        return True


class EditImageForm(CreateImageForm):
    # There were two `image_file` declarations here, the first carrying
    # DataRequired() and immediately shadowed by this one -- so the field has
    # always been optional on an edit, and the line above it only read as
    # though it were required.
    image_file = FileField(_l('Image'), validators=[Optional()], render_kw={'accept': 'image/*'})

    # `validate` used to be overridden here, to repeat the
    # `allow_local_image_posts` check that CreateImageForm.validate -- the
    # super() this called first -- has already made. The parent refuses before
    # the copy is ever reached, so no input could tell them apart. Removed;
    # the inherited method is the one that runs.


class CreateEventForm(SubmittedUrlMixin, CreatePostForm):
    start_datetime = DateTimeLocalField(_l('Start'), validators=[DataRequired()], format="%Y-%m-%dT%H:%M")
    end_datetime = DateTimeLocalField(_l('End'), validators=[DataRequired()], format="%Y-%m-%dT%H:%M")
    image_file = FileField(_l('Banner'), validators=[Optional()], render_kw={'accept': 'image/*'})
    more_info_url = StringField(_l('More information link'), validators=[Optional(), Regexp(r'^https?://', message='URLs need to start with "http://"" or "https://"')])
    event_timezone = SelectField(_l('Timezone'), validators=[Optional()],
                           render_kw={'id': 'timezone', "class": "form-control tom-select"})
    join_mode = SelectField(_l('Cost'), validators=[Optional()])
    max_attendees = IntegerField(_l('Maximum number of attendees'))
    online = BooleanField(_l('Online'))
    online_link = StringField(_l('Online link'), validators=[Optional(), Regexp(r'^https?://', message='URLs need to start with "http://"" or "https://"')])
    irl_address = StringField(_l('Address'))
    irl_city = StringField(_l('City'))
    irl_country = StringField(_l('Country'))


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.event_timezone.choices = get_timezones()
        self.join_mode.choices = [('free', _('Free')), ('donation', _('Donation')), ('paid', _('Paid'))]

    # These two replace a `validate_link_url` hook that was DEAD: WTForms
    # resolves a `validate_<name>` hook by field name, and CreateEventForm has
    # no `link_url` field -- its URL fields are `more_info_url` and
    # `online_link` -- so the hook was never looked up and never ran. It was
    # wired to the two fields it was evidently meant to guard rather than
    # deleted, because deleting it would have left an event's two
    # user-submitted, stored-and-rendered URLs as the only ones on the site
    # with no domain-ban and no parseability check at all. Both fields are
    # Optional, so an empty one short-circuits before either check.
    def validate_more_info_url(self, field):
        return self.url_field_is_acceptable(
            field, lambda name: _l("Links to %(domain)s are not allowed.", domain=name))

    def validate_online_link(self, field):
        return self.url_field_is_acceptable(
            field, lambda name: _l("Links to %(domain)s are not allowed.", domain=name))

    def validate(self, extra_validators=None) -> bool:
        if not super().validate(extra_validators):
            return False

        local_tz = ZoneInfo(self.event_timezone.data)
        local_start = self.start_datetime.data.replace(tzinfo=local_tz)
        local_end = self.end_datetime.data.replace(tzinfo=local_tz)

        # Convert to UTC for comparison with utcnow()
        utc_start = local_start.astimezone(ZoneInfo('UTC'))
        utc_end = local_end.astimezone(ZoneInfo('UTC'))

        # D1400, and D1001's shape three more times: each of these appended its
        # complaint and then fell through to `return True` at the end of the method.
        # WTForms computes `validate()`'s answer from field validation, and appending
        # to `field.errors` afterwards does not change it -- so `validate_on_submit()`
        # was True and the route went on to create the event, while the message sat on
        # a form the route never renders again because it redirects on success.
        #
        # Measured: a start two days in the past validated True; so did an end in the
        # past, and an end before its start.
        #
        # All three are still collected before refusing, rather than returning at the
        # first, so a submitter with two problems is told about both. That is why this
        # is a flag and not three early returns like the block below it.
        times_are_wrong = False
        if utc_start < utcnow(naive=False):
            self.start_datetime.errors.append(_('This time is in the past.'))
            times_are_wrong = True
        if utc_end < utcnow(naive=False):
            self.end_datetime.errors.append(_('This time is in the past.'))
            times_are_wrong = True

        if self.start_datetime.data > self.end_datetime.data:
            self.start_datetime.errors.append(_('Start must be less than end.'))
            times_are_wrong = True

        if times_are_wrong:
            return False

        # Validate online vs physical event requirements
        if self.online.data:
            # Online event - online_link is required
            if not self.online_link.data or not self.online_link.data.strip():
                self.online_link.errors.append(_l('Online link is required for online events.'))
                return False
        else:
            # Physical event - address, city, and country are required
            if not self.irl_address.data or not self.irl_address.data.strip():
                self.irl_address.errors.append(_l('Address is required for physical events.'))
                return False
            if not self.irl_city.data or not self.irl_city.data.strip():
                self.irl_city.errors.append(_l('City is required for physical events.'))
                return False
            if not self.irl_country.data or not self.irl_country.data.strip():
                self.irl_country.errors.append(_l('Country is required for physical events.'))
                return False

        if 'image_file' in request.files:
            uploaded_file = request.files['image_file']
            if image_upload_too_large(self.image_file, uploaded_file):
                return False
            if self.communities:
                community = db.session.get(Community, self.communities.data)
                if community.is_local() and g.site.allow_local_image_posts is False:
                    # D1001, third site.
                    self.communities.errors.append(_l('Images cannot be posted to local communities.'))
                    return False

        return True


class CreatePollForm(CreatePostForm):
    mode = SelectField(_l('Mode'), validators=[DataRequired()], choices=[('single', _l('Voters choose one option')),
                                                                        ('multiple', _l('Voters choose many options'))],
                       render_kw={'class': 'form-select'})
    finish_choices = [
        ('30m', _l('30 minutes')),
        ('1h', _l('1 hour')),
        ('6h', _l('6 hours')),
        ('12h', _l('12 hours')),
        ('1d', _l('1 day')),
        ('3d', _l('3 days')),
        ('7d', _l('7 days')),
    ]
    finish_in = SelectField(_l('End voting in'), validators=[DataRequired()], choices=finish_choices,
                            render_kw={'class': 'form-select'})
    local_only = BooleanField(_l('Accept votes from this instance only'))
    choice_1 = StringField('Choice')  # intentionally left out of internationalization (no _l()) as this label is not used
    choice_2 = StringField('Choice')
    choice_3 = StringField('Choice')
    choice_4 = StringField('Choice')
    choice_5 = StringField('Choice')
    choice_6 = StringField('Choice')
    choice_7 = StringField('Choice')
    choice_8 = StringField('Choice')
    choice_9 = StringField('Choice')
    choice_10 = StringField('Choice')
    choice_11 = StringField('Choice')
    choice_12 = StringField('Choice')
    choice_13 = StringField('Choice')
    choice_14 = StringField('Choice')
    choice_15 = StringField('Choice')

    def validate(self, extra_validators=None) -> bool:
        if not super().validate(extra_validators):
            return False

        # Polls shouldn't be scheduled more than once
        if self.repeat.data in ['daily', 'weekly', 'monthly']:
            self.repeat.errors.append(_l("Polls can't be scheduled more than once"))
            return False

        # D1003. Two defects in four lines.
        #
        # `range(1, 10)` counted choices 1-9 of the FIFTEEN fields this form
        # declares, while `make_post` reads `range(1, 16)`. So a poll whose
        # choices were typed into 10-15 was refused with "Polls need options
        # for people to choose from" while showing six of them, and the
        # unreachable `> 15` branch below could never fire -- fifteen fields
        # cannot produce sixteen choices. The count now covers every field
        # the form has and the dead branch is gone.
        #
        # `.data.strip()` was an AttributeError for any submission that
        # omitted a choice field: WTForms leaves an unsubmitted StringField at
        # None, not ''. The browser form always posts all fifteen, so this was
        # a 500 for anything else. Measured:
        # `AttributeError: 'NoneType' object has no attribute 'strip'`.
        choices_made = 0
        for i in range(1, 16):
            choice_data = (getattr(self, f"choice_{i}").data or '').strip()
            if choice_data != '':
                choices_made += 1
        if choices_made == 0:
            self.choice_1.errors.append(_l('Polls need options for people to choose from'))
            return False
        elif choices_made <= 1:
            self.choice_2.errors.append(_l('Provide at least two choices'))
            return False
        return True


class ReportCommunityForm(FlaskForm):
    reason_choices = [('1', _l('Breaks instance rules')),
                      ('2', _l('Abandoned by moderators')),
                      ('3', _l('Cult')),
                      ('4', _l('Scam')),
                      ('5', _l('Alt-right pipeline')),
                      ('6', _l('Hate / genocide')),
                      ('7', _l('Other')),
                      ]
    reasons = MultiCheckboxField(_l('Reason'), choices=reason_choices)
    description = StringField(_l('More info'), validators=[Length(max=256)])
    report_remote = BooleanField('Also send report to originating instance')
    submit = SubmitField(_l('Report'))

    def reasons_to_string(self, reason_data) -> str:
        result = []
        for reason_id in reason_data:
            for choice in self.reason_choices:
                if choice[0] == reason_id:
                    result.append(str(choice[1]))
        return ', '.join(result)[:255]


class SetMyFlairForm(FlaskForm):
    my_flair = StringField(_l('Flair'), validators=[Optional(), Length(min=0, max=50)])
    submit = SubmitField(_l('Save'))


class DeleteCommunityForm(FlaskForm):
    submit = SubmitField(_l('Delete community'))


class InviteAcceptForm(FlaskForm):
    submit = SubmitField(_l('Accept invitation'))


class RetrieveRemotePost(FlaskForm):
    address = StringField(_l('Full URL'),
                          render_kw={'placeholder': 'e.g. https://lemmy.world/post/123', 'autofocus': True},
                          validators=[DataRequired()])
    submit = SubmitField(_l('Retrieve'))


class InviteCommunityForm(FlaskForm):
    # community_invite calls invite_with_email() once per line, from this
    # instance's own mail server. Community.invitations defaults to 0 and
    # can_invite() returns True for anyone when it is 0, so without a cap any
    # account past created_very_recently() could paste ten thousand addresses
    # into a default community's invite box and have the instance send ten
    # thousand emails under its own reputation. The feature is for inviting
    # people you know.
    MAX_INVITATIONS = 20

    to = TextAreaField(_l('To'), validators=[DataRequired()],
                       render_kw={'placeholder': _l('Email addresses or fediverse handles, one per line'),
                                  'autofocus': True})
    submit = SubmitField(_l('Invite'))

    def validate_to(self, field):
        if ',' in field.data:
            raise ValidationError(_l('Use new lines instead of commas.'))
        recipients = [line.strip() for line in field.data.split('\n') if line.strip()]
        if len(recipients) > self.MAX_INVITATIONS:
            raise ValidationError(
                _l('Please invite no more than %(num)d people at a time.',
                   num=self.MAX_INVITATIONS))
        lines = field.data.split('\n')
        if len(lines) > 50:
            raise ValidationError(_l('Maximum of 50 at a time.'))


class MoveCommunityForm(FlaskForm):
    old_community_locked = BooleanField(_l('The old community is locked'), validators=[DataRequired()])
    post_link = StringField(_l('Move notification post in old community'), validators=[DataRequired()])
    submit = SubmitField(_l('Request move'))


class EditCommunityFlairForm(FlaskForm):
    flair = StringField(_l('Flair'), validators=[DataRequired()])
    text_color = StringField(_l('Text color'), render_kw={"type": "color"})
    background_color = StringField(_l('Background color'), render_kw={"type": "color"})
    blur_images = BooleanField(_l('Blur images and thumbnails for posts with this flair'))
    submit = SubmitField(_l('Save'))


class EditCommunityMembership(FlaskForm):
    block_flair = MultiCheckboxField(_l('Block posts with this flair'), coerce=int, validators=[Optional()],
                                        render_kw={'class': 'form-multicheck-columns'})
    submit = SubmitField(_l('Save'))


class CommunityRssFeedEdit(FlaskForm):
    name = StringField(_l('Name'))
    url = StringField(_l('Url'), validators=[DataRequired(), URL(allow_ip=False)])
    flair = SelectField(_l('Flair'), render_kw={'class': 'form-select'})
    polling_choices = [
        ('30', _l('30 minutes')),
        ('60', _l('1 hour')),
        ('360', _l('6 hours')),
        ('720', _l('12 hours')),
        ('1440', _l('1 day')),
        ('4320', _l('3 days')),
        ('10080', _l('7 days')),
    ]
    check_frequency = SelectField(_l('Polling frequency'), validators=[DataRequired()], choices=polling_choices,
                                  render_kw={'class': 'form-select'})
    submit = SubmitField(_l('Save'))


class DeleteCommunityRssFeedForm(FlaskForm):
    submit = SubmitField(_l('Delete RSS feed'))


class InstanceAddPeopleForm(FlaskForm):
    people = TextAreaField(_('Fediverse handles'),
                           render_kw={'placeholder': _l('Fediverse handles, such as @person@example.org, one per line'),
                                      'autofocus': True})
    mastodon_csv = FileField(_l('Mastodon CSV'), render_kw={'accept': 'text/csv'})
    referrer = HiddenField()
    submit = SubmitField(_l('Add people'))
