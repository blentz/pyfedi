"""/admin/messaging: every email the site sends, the admins' text for the built-in ones, and messages admins write to
members (spec docs/superpowers/specs/2026-11-24-admin-messaging-design.md). Replaces /admin/newsletter (D21)."""
from flask import abort, current_app, flash, redirect, request, url_for
from flask_babel import _, lazy_gettext as _l
from flask_login import current_user, login_required
from flask_wtf import FlaskForm
from wtforms import BooleanField, RadioField, SelectField, StringField, SubmitField, TextAreaField
from wtforms.fields import DateTimeLocalField
from wtforms.fields.choices import SelectMultipleField
from wtforms.validators import DataRequired, Length, Optional

from app import db
from app.admin import bp
from app.email import mail_name, send_email
from app.form_choices import select_choices
from app.local_names import local_clause
from app.messaging.catalog import BY_KEY, CATALOGUE, MESSAGE_PLACEHOLDERS
from app.messaging.messages import mail_one, recipient_count, send_now, schedule, unschedule
from app.messaging.render import compose, problems, sample_values
from app.models import AdminMessage, AdminMessagePick, EmailTemplate, Offer, Role, User, utcnow
from app.permissions import user_access
from app.storefront import domain_rows
from app.utils import permission_required, render_template, roles_with

EDITABLE_STATUSES = ('draft', 'scheduled')
MAX_PICKS = 1000


class TemplateForm(FlaskForm):
    subject = StringField(_l('Subject'), validators=[DataRequired(), Length(max=255)])
    body = TextAreaField(_l('Text (Markdown)'), validators=[DataRequired(), Length(max=50000)], render_kw={'rows': 14})
    save = SubmitField(_l('Save'))
    preview = SubmitField(_l('Preview'))
    send_test = SubmitField(_l('Save and send a test to me'))


class ResetForm(FlaskForm):
    reset = SubmitField(_l('Reset to the default text'))


class MessageForm(FlaskForm):
    subject = StringField(_l('Subject'), validators=[DataRequired(), Length(max=255)])
    body = TextAreaField(_l('Text (Markdown)'), validators=[DataRequired(), Length(max=100000)], render_kw={'rows': 14})
    service_notice = BooleanField(_l('Service notice: send it to members who turned the newsletter off too'))
    audience = RadioField(_l('Send to'), default='all', choices=select_choices([
        ('all', _l('All members')), ('filtered', _l('Members matching the filters below')),
        ('picked', _l('Only the accounts listed below'))]))
    domains = SelectMultipleField(_l('Whose home domain is'), coerce=int, validators=[Optional()],
                                  render_kw={'class': 'form-select'})
    roles = SelectMultipleField(_l('Who hold the role'), coerce=int, validators=[Optional()],
                                render_kw={'class': 'form-select'})
    subscription = SelectField(_l('Subscription'), default='', choices=select_choices([
        ('', _l('Any, or none')), ('any', _l('Has an active subscription')), ('none', _l('Has no active subscription')),
        ('offers', _l('Has one of the plans below'))]), render_kw={'class': 'form-select'})
    offers = SelectMultipleField(_l('Plans'), coerce=int, validators=[Optional()], render_kw={'class': 'form-select'})
    picked = TextAreaField(_l('Accounts, one per line (name, or name@domain)'), render_kw={'rows': 4})
    scheduled_for = DateTimeLocalField(_l('Send at (UTC)'), format='%Y-%m-%dT%H:%M', validators=[Optional()])
    save = SubmitField(_l('Save draft'))
    send_test = SubmitField(_l('Save and send a test to me'))
    schedule = SubmitField(_l('Save and schedule'))
    send_now = SubmitField(_l('Save and send now'))

    def fill_choices(self):
        self.domains.choices = select_choices((row.id, row.domain) for row in domain_rows() if row.id is not None)
        self.roles.choices = select_choices((role.id, role.name)
                                            for role in Role.query.order_by(Role.built_in.desc(), Role.name))
        self.offers.choices = select_choices((offer.id, offer.title) for offer in Offer.query.filter(
            Offer.kind == 'subscription').order_by(Offer.position, Offer.id))


def _picked_users(text: str) -> tuple[list, list]:
    """The accounts named, one per line, and the names that name no local account."""
    users, unknown = [], []
    for name in dict.fromkeys(line.strip() for line in (text or '').splitlines() if line.strip()):
        user = User.query.filter(local_clause(User, name), User.deleted.is_(False)).first()
        (users.append(user) if user is not None else unknown.append(name))
    return users, unknown


def _form_problems(form: MessageForm, message: AdminMessage | None, users: list, unknown: list) -> list:
    found = problems(MESSAGE_PLACEHOLDERS, form.subject.data, form.body.data)
    if form.audience.data == 'picked' and not users and not unknown:
        found.append(_('List at least one account to send it to.'))
    if form.audience.data == 'picked' and len(users) > MAX_PICKS:
        found.append(_('List at most %(num)d accounts, or use the filters.', num=MAX_PICKS))
    found += [_('No local account is called %(name)s.', name=name) for name in unknown]
    if form.audience.data == 'filtered' and form.subscription.data == 'offers' and not form.offers.data:
        found.append(_('Choose at least one plan.'))
    # a time is needed to schedule, and a time given to a scheduled message moves it: either way, a future one
    rescheduling = message is not None and message.status == 'scheduled' and not form.send_now.data
    if form.schedule.data and form.scheduled_for.data is None:
        found.append(_('Choose when to send it.'))
    elif (form.schedule.data or rescheduling) and form.scheduled_for.data is not None \
            and form.scheduled_for.data <= utcnow():
        found.append(_('Choose a time in the future, or send it now.'))
    return found


def _store(message: AdminMessage, form: MessageForm, users: list) -> None:
    message.subject, message.body = form.subject.data.strip(), form.body.data
    message.service_notice = bool(form.service_notice.data)
    message.audience = form.audience.data   # one of AUDIENCES: the form's choices are those
    filtered = message.audience == 'filtered'
    message.domain_ids = list(form.domains.data or []) if filtered else None
    message.role_ids = list(form.roles.data or []) if filtered else None
    message.subscription = (form.subscription.data or None) if filtered else None
    message.offer_ids = list(form.offers.data or []) if filtered and message.subscription == 'offers' else None
    if message.id is None:
        db.session.add(message)
        db.session.flush()
    AdminMessagePick.query.filter_by(message_id=message.id).delete()
    if message.audience == 'picked':
        db.session.add_all(AdminMessagePick(message_id=message.id, user_id=user.id) for user in users)


def _load_form(form: MessageForm, message: AdminMessage) -> None:
    form.subject.data, form.body.data = message.subject, message.body
    form.service_notice.data, form.audience.data = message.service_notice, message.audience
    form.domains.data, form.roles.data = list(message.domain_ids or []), list(message.role_ids or [])
    form.subscription.data, form.offers.data = message.subscription or '', list(message.offer_ids or [])
    picks = User.query.join(AdminMessagePick, AdminMessagePick.user_id == User.id) \
        .filter(AdminMessagePick.message_id == message.id).order_by(User.user_name).all()
    form.picked.data = '\n'.join(user.link() for user in picks)
    form.scheduled_for.data = message.scheduled_for


def _message(message_id: int, lock: bool = False) -> AdminMessage:
    message = db.session.get(AdminMessage, message_id, with_for_update=lock)
    if message is None:
        abort(404)
    return message


@bp.route('/newsletter')
@login_required
@permission_required('send newsletter')
def newsletter():
    return redirect(url_for('admin.messaging'))


@bp.route('/messaging')
@login_required
@permission_required('send newsletter')
def messaging():
    overrides = {row.key for row in EmailTemplate.query.with_entities(EmailTemplate.key)}
    messages = AdminMessage.query.order_by(AdminMessage.status != 'scheduled', AdminMessage.status != 'draft',
                                           AdminMessage.scheduled_for, AdminMessage.updated_at.desc()).all()
    return render_template('admin/messaging.html', title=_('Messaging'), catalogue=CATALOGUE, overrides=overrides,
                           messages=messages, can_edit_templates=user_access('edit email templates', current_user.id),
                           roles_with=roles_with('send newsletter'))


def _edit_message(message: AdminMessage | None):
    """The form for a new message (`message` None) or for a draft or scheduled one, locked for the request (the caller
    checked it may still be changed); a sent one is shown read-only."""
    form = MessageForm()
    form.fill_choices()
    if message is not None and request.method == 'GET':
        _load_form(form, message)
    found = []
    if form.validate_on_submit():
        users, unknown = _picked_users(form.picked.data) if form.audience.data == 'picked' else ([], [])
        found = _form_problems(form, message, users, unknown)
        if not found:
            message = message or AdminMessage(created_by=current_user.id, status='draft')
            _store(message, form, users)
            if form.schedule.data:
                schedule(message, form.scheduled_for.data)
            elif message.status == 'scheduled' and form.scheduled_for.data is not None:
                message.scheduled_for = form.scheduled_for.data   # saved: stays scheduled, at the time shown
            db.session.commit()
            if form.send_now.data:
                send_now(message)
                flash(_('Sending.'))
                return redirect(url_for('admin.messaging'))
            if form.schedule.data:
                flash(_('Scheduled for %(when)s UTC.', when=message.scheduled_for.strftime('%Y-%m-%d %H:%M')))
                return redirect(url_for('admin.messaging'))
            if form.send_test.data:
                mail_one(message, current_user)
                flash(_('Saved. A test was sent to %(email)s.', email=current_user.email))
            else:
                flash(_('Saved.'))
            return redirect(url_for('admin.messaging_edit', message_id=message.id))
    stored = message is not None and message.id is not None
    return render_template('admin/messaging_message.html', title=_('Message'), form=form, message=message,
                           problems=found, placeholders=MESSAGE_PLACEHOLDERS,
                           preview=compose(message.subject, message.body, MESSAGE_PLACEHOLDERS,
                                           sample_values(MESSAGE_PLACEHOLDERS)) if stored else None,
                           count=recipient_count(message) if stored else None,
                           read_only=stored and message.status not in EDITABLE_STATUSES,
                           roles_with=roles_with('send newsletter'))


@bp.route('/messaging/new', methods=['GET', 'POST'])
@login_required
@permission_required('send newsletter')
def messaging_new():
    return _edit_message(None)


@bp.route('/messaging/<int:message_id>', methods=['GET', 'POST'])
@login_required
@permission_required('send newsletter')
def messaging_edit(message_id):
    # A POST locks the row, so the dispatcher's claim (one UPDATE) waits for it, and the status read here holds
    message = _message(message_id, lock=request.method == 'POST')
    if request.method == 'POST' and message.status not in EDITABLE_STATUSES:
        abort(409)
    return _edit_message(message)


@bp.route('/messaging/<int:message_id>/unschedule', methods=['POST'])
@login_required
@permission_required('send newsletter')
def messaging_unschedule(message_id):
    if not FlaskForm().validate_on_submit():
        abort(400)
    if unschedule(_message(message_id)):
        flash(_('Back to a draft. It will not be sent.'))
    else:
        flash(_('It was not scheduled, or sending has already begun.'), 'warning')
    return redirect(url_for('admin.messaging_edit', message_id=message_id))


@bp.route('/messaging/<int:message_id>/delete', methods=['POST'])
@login_required
@permission_required('send newsletter')
def messaging_delete(message_id):
    if not FlaskForm().validate_on_submit():
        abort(400)
    message = _message(message_id)
    if message.status != 'draft':
        flash(_('Only a draft can be deleted.'), 'warning')
        return redirect(url_for('admin.messaging_edit', message_id=message_id))
    db.session.delete(message)
    db.session.commit()
    flash(_('Deleted.'))
    return redirect(url_for('admin.messaging'))


@bp.route('/messaging/<int:message_id>/duplicate', methods=['POST'])
@login_required
@permission_required('send newsletter')
def messaging_duplicate(message_id):
    if not FlaskForm().validate_on_submit():
        abort(400)
    original = _message(message_id)
    copy = AdminMessage(subject=original.subject, body=original.body, audience=original.audience,
                        domain_ids=original.domain_ids, role_ids=original.role_ids,
                        subscription=original.subscription, offer_ids=original.offer_ids,
                        service_notice=original.service_notice, status='draft', created_by=current_user.id)
    db.session.add(copy)
    db.session.flush()
    db.session.add_all(AdminMessagePick(message_id=copy.id, user_id=pick.user_id)
                       for pick in AdminMessagePick.query.filter_by(message_id=original.id))
    db.session.commit()
    flash(_('Copied into a new draft.'))
    return redirect(url_for('admin.messaging_edit', message_id=copy.id))


def _entry(key: str):
    entry = BY_KEY.get(key)
    if entry is None or not entry.editable:
        abort(404)
    return entry


@bp.route('/messaging/email/<key>', methods=['GET', 'POST'])
@login_required
@permission_required('edit email templates')
def messaging_template(key):
    entry = _entry(key)
    row = db.session.get(EmailTemplate, key)
    form = TemplateForm()
    if request.method == 'GET' and row is not None:
        form.subject.data, form.body.data = row.subject, row.body
    found, preview = [], None
    if form.validate_on_submit():
        found = problems(entry.placeholders, form.subject.data, form.body.data)
        if not found and form.preview.data:
            preview = compose(form.subject.data, form.body.data, entry.placeholders, sample_values(entry.placeholders))
        elif not found:
            row = row or EmailTemplate(key=key)
            row.subject, row.body, row.updated_by = form.subject.data.strip(), form.body.data, current_user.id
            db.session.add(row)
            db.session.commit()
            if form.send_test.data:
                mail = compose(row.subject, row.body, entry.placeholders, sample_values(entry.placeholders))
                send_email(mail.subject, sender=f'{mail_name(current_user)} <{current_app.config["MAIL_FROM"]}>',
                           recipients=[current_user.email], text_body=mail.text_body, html_body=mail.html_body)
                flash(_('Saved. A test with sample values was sent to %(email)s.', email=current_user.email))
            else:
                flash(_('Saved. The site now sends this text.'))
            return redirect(url_for('admin.messaging_template', key=key))
    return render_template('admin/messaging_template.html', title=entry.name, entry=entry, form=form, row=row,
                           reset_form=ResetForm(), problems=found, preview=preview,
                           roles_with=roles_with('edit email templates'))


@bp.route('/messaging/email/<key>/reset', methods=['POST'])
@login_required
@permission_required('edit email templates')
def messaging_template_reset(key):
    _entry(key)
    if not ResetForm().validate_on_submit():
        abort(400)
    EmailTemplate.query.filter_by(key=key).delete()
    db.session.commit()
    flash(_('The default text is back.'))
    return redirect(url_for('admin.messaging_template', key=key))
