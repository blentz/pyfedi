"""Admin messaging (spec docs/superpowers/specs/2026-11-24-admin-messaging-design.md): the catalogue of every email,
an admin's text for the built-in ones, and messages admins write, store, schedule and send."""
import os
import re
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g, session as flask_session
from flask_wtf.csrf import generate_csrf

from app import cache, cli, db
from app.constants import NOTIF_POST
from app.email import send_password_reset_email, send_registration_approved_email, send_verification_email
from app.messaging import messages as messaging
from app.messaging.catalog import BY_KEY, CATALOGUE, MESSAGE_PLACEHOLDERS, Placeholder
from app.messaging.messages import (audience_query, claim_due, deliver, dispatch_due, recipient_count, render_for,
                                    send_admin_message, send_now, unschedule)
from app.messaging.render import (Value, compose, fill_html, fill_text, overridden, placeholders_in, problems,
                                  sample_values, secret_fits)
from app.messaging.values import digest_values
from app.models import (AdminMessage, AdminMessageDelivery, AdminMessagePick, EmailTemplate, MemberSubscription,
                        Notification, Offer, Post, Role, RoleGrant, Site, SiteDomain, User, utcnow)
from app.shared.community import invite_with_email
from app.storefront import clear_domain_cache, home_storefront
from app.utils import set_setting
from tests.factories import (INSTANCE_SETTINGS, configure, grant_permission, grant_permissions, make_admin,
                             make_community,
                             make_instance, make_user)

DEFAULT_HOST = 'test.piefed.local'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.name = 'Test Site'
    g.site = site
    if SiteDomain.query.filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=DEFAULT_HOST, scheme='https', is_default=True))
    other = SiteDomain(domain='other.example', scheme='https')
    db.session.add(other)
    db.session.commit()
    clear_domain_cache()
    cache.clear()
    admin = make_user(api_baseline.instance_local, 'siteadmin', local=True)
    admin.email = 'admin@test.piefed.local'
    admin.newsletter = False   # the admin's own opt-out never stops a test to themselves
    grant_permissions(admin, *INSTANCE_SETTINGS)
    db.session.commit()
    client = app.test_client()
    login(client, admin)
    # Only the accounts a test makes count: every fixture account is local, verified and takes the newsletter
    for user in User.query.filter(User.id != admin.id):
        user.newsletter = False
    db.session.commit()
    yield SimpleNamespace(app=app, site=site, admin=admin, client=client, baseline=api_baseline,
                          default=SiteDomain.query.filter_by(is_default=True).one(), other=other)
    clear_domain_cache()


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


def csrf(env, client=None):
    client = client or env.client
    g.pop('csrf_token', None)   # generate_csrf keeps the last token in g, which outlives a request context here
    with env.app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def member(env, name, **fields):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.newsletter = True
    user.verification_token = f'tok{user.id}'
    for field, value in fields.items():
        setattr(user, field, value)
    db.session.commit()
    return user


def a_message(**fields):
    fields.setdefault('subject', 'News')
    fields.setdefault('body', 'Hello {name}')
    fields.setdefault('audience', 'all')
    message = AdminMessage(**fields)
    db.session.add(message)
    db.session.commit()
    return message


def reached(message):
    return {user.user_name for user in audience_query(message)}


class Sent(list):
    """send_email's calls, as dicts with the subject among the keyword arguments."""
    def __call__(self, subject, *args, **kwargs):
        if args:   # send_email(subject, sender, recipients, text_body, html_body, ...)
            kwargs.update(zip(('sender', 'recipients', 'text_body', 'html_body', 'reply_to'), args))
        self.append(dict(kwargs, subject=subject))

    def to(self, address):
        return [mail for mail in self if address in mail['recipients']]


# ---------------------------------------------------------------------------------------------------------------------
# The catalogue (D1, D2)

class TestTheCatalogue:
    def test_every_bundled_template_belongs_to_an_entry(self, app):
        named = {name for entry in CATALOGUE for name in entry.templates} | {'override'}
        folder = os.path.join(current_app.root_path, 'templates', 'email')
        bundled = {os.path.splitext(name)[0] for name in os.listdir(folder)}
        assert bundled - named == set()

    def test_each_key_once_and_found_by_key(self):
        assert len(BY_KEY) == len(CATALOGUE)
        assert all(BY_KEY[entry.key] is entry for entry in CATALOGUE)

    def test_the_editable_ones(self):
        assert {entry.key for entry in CATALOGUE if entry.editable} == {
            'welcome', 'verify_email', 'password_reset', 'subscription_expiring', 'unread_notifications',
            'community_invite'}

    def test_the_links_that_make_an_email_work_are_required(self):
        assert BY_KEY['verify_email'].placeholder('verify_url').required
        assert BY_KEY['password_reset'].placeholder('reset_url').required
        assert BY_KEY['unread_notifications'].placeholder('notifications').required
        assert BY_KEY['community_invite'].placeholder('join_url').required
        assert BY_KEY['welcome'].placeholder('nothing') is None

    def test_the_expiring_one_says_daily_from_ten_days(self):
        assert 'Once a day, from 10 days before' in str(BY_KEY['subscription_expiring'].when)


# ---------------------------------------------------------------------------------------------------------------------
# Filling in placeholders (D5-D9)

URL = Placeholder('url', 'a link', 'https://piefed.example/x', 'url')
NAME = Placeholder('name', 'a name', 'Alex')
LIST = Placeholder('items', 'a list', '- one', 'list', True)


class TestPlaceholders:
    def test_found_but_not_escaped_braces(self):
        assert placeholders_in('{name} and {{name}} and {url}') == {'name', 'url'}
        assert placeholders_in(None) == set()

    def test_unknown_and_missing_ones_are_refused(self):
        found = problems((NAME, LIST), 'Hi {name}', 'Hi {nobody}')
        assert found == ['{nobody} is not a placeholder this email has', '{items} has to be in the text']

    def test_a_list_cannot_be_in_the_subject(self):
        assert problems((LIST,), '{items}', '{items}') == ['{items} is a list, which cannot be in the subject']

    def test_good_text_has_none(self):
        assert problems((NAME, LIST), 'Hi {name}', '{items}') == []

    def test_text_takes_values_as_they_are(self):
        assert fill_text('{name} <b> {{name}} {unknown}', {'name': Value('A & B')}) == 'A & B <b> {name} {unknown}'

    def test_html_escapes_values(self):
        out = fill_html('<p>Hi {name}</p>', {'name': Value('<script>x</script>')}, {'name': 'text'})
        assert out == '<p>Hi &lt;script&gt;x&lt;/script&gt;</p>'

    def test_a_url_on_its_own_becomes_a_link(self):
        out = fill_html('<p>Go {url}</p>', {'url': Value('https://a.example/?a=1&b=2')}, {'url': 'url'})
        assert out == '<p>Go <a href="https://a.example/?a=1&amp;b=2">https://a.example/?a=1&amp;b=2</a></p>'

    def test_a_url_already_in_a_link_stays_text_and_its_href_is_filled(self):
        out = fill_html('<p><a href="{url}">{url}</a> then {url}</p>', {'url': Value('https://a.example/"x')},
                        {'url': 'url'})
        assert out == ('<p><a href="https://a.example/&quot;x">https://a.example/&quot;x</a> then '
                       '<a href="https://a.example/&quot;x">https://a.example/&quot;x</a></p>')

    def test_a_percent_encoded_placeholder_in_a_link_is_filled(self):
        out = fill_html('<a href="%7Burl%7D">x</a><a href="%7bnope%7d">y</a>', {'url': Value('https://a.example/')},
                        {'url': 'url'})
        assert out == '<a href="https://a.example/">x</a><a href="%7bnope%7d">y</a>'

    def test_a_list_is_put_in_as_made(self):
        out = fill_html('<p>{items}</p>', {'items': Value('- a', '<ul><li>a</li></ul>')}, {'items': 'list'})
        assert out == '<p><ul><li>a</li></ul></p>'

    def test_braces_and_unknown_names_in_html(self):
        out = fill_html('<p title="{{x}} {nope}">{{name}} {nope}</p>', {'name': Value('A')}, {})
        assert out == '<p title="{x} {nope}">{name} {nope}</p>'

    def test_a_url_with_no_value_is_left(self):
        assert fill_html('<p>{url}</p>', {}, {'url': 'url'}) == '<p>{url}</p>'


SECRET = Placeholder('reset_url', 'a reset link', 'https://piefed.example/r/abc', 'secret', True)
TOKEN_LINK = 'https://piefed.example/auth/reset_password/TOKEN'


class TestSecretLinks:
    """D23: a link carrying a sign-in token only ever stands as a link of its own, so an editor cannot send it to
    their own server."""

    @pytest.mark.parametrize('body', [
        '{reset_url}', 'Open {reset_url}', 'Open {reset_url}.', 'Open {reset_url}, now', '[Reset it]({reset_url})',
        'Line one\n{reset_url}\nLine three', 'Open {reset_url}! now',
    ])
    def test_where_it_may_stand(self, body):
        assert problems((SECRET,), 'Reset', body) == []

    @pytest.mark.parametrize('body', [
        '![x]({reset_url})', '[x](https://evil.example/?u={reset_url})', 'https://evil.example/{reset_url}',
        '<img src="{reset_url}">', '({reset_url})', '{reset_url}x', 'x{reset_url}', '[x]({reset_url} "t")',
        '{reset_url}.x', '{reset_url}](x)', '](x){reset_url}',
    ])
    def test_where_it_may_not(self, body):
        assert any('carries a sign-in token, so it can only be a link of its own' in problem
                   for problem in problems((SECRET,), 'Reset', body))

    def test_never_in_the_subject(self):
        assert '{reset_url} carries a sign-in token, so it cannot be in the subject' in problems(
            (SECRET,), 'Go {reset_url}', '{reset_url}')

    def test_a_link_without_its_opening_bracket(self):
        assert not secret_fits('x]({reset_url})', 3, 14)

    def test_text_drops_it_anywhere_else(self):
        values = {'reset_url': Value(TOKEN_LINK)}
        kinds = {'reset_url': 'secret'}
        assert fill_text('a {reset_url} b', values, kinds) == f'a {TOKEN_LINK} b'
        assert fill_text('https://evil.example/?{reset_url}', values, kinds) == 'https://evil.example/?'
        assert fill_text('[x]({reset_url})', values, kinds) == f'[x]({TOKEN_LINK})'

    def test_html_puts_it_only_as_a_links_whole_href(self, env):
        values, kinds = {'reset_url': Value(TOKEN_LINK)}, {'reset_url': 'secret'}
        rendered = ('<p><a href="{reset_url}">a</a> <a href="%7Breset_url%7D">b</a> <img src="{reset_url}"> '
                    '<a href="https://evil.example/?{reset_url}">c</a> <img src="%7Breset_url%7D"> '
                    '<a title="{reset_url}" href="x">d</a> {reset_url}</p>')
        out = fill_html(rendered, values, kinds)
        assert out.count('TOKEN') == 4
        assert f'<a href="{TOKEN_LINK}">a</a>' in out and f'<a href="{TOKEN_LINK}">b</a>' in out
        assert '<img src="">' in out and '<a href="https://evil.example/?">c</a>' in out
        assert '<a title="" href="x">d</a>' in out and f'<a href="{TOKEN_LINK}">{TOKEN_LINK}</a></p>' in out

    def test_html_leaves_a_secret_with_no_value(self, env):
        assert fill_html('<a href="{reset_url}">a</a>', {}, {'reset_url': 'secret'}) == '<a href="{reset_url}">a</a>'

    def test_a_row_written_some_other_way_leaks_nothing(self, env):
        """The checks at saving are not all there is: a row that skipped them is filled the same way."""
        with home_storefront(env.admin):
            mail = compose('{reset_url}', '![x](https://evil.example/{reset_url}) [y](https://evil.example/?{reset_url})',
                           (SECRET,), {'reset_url': Value(TOKEN_LINK)})
        assert 'TOKEN' not in mail.subject + mail.text_body + mail.html_body

    def test_the_sign_in_emails_are_admin_only(self):
        assert {entry.key for entry in CATALOGUE if entry.admin_only} == {'password_reset', 'verify_email'}


class TestCompose:
    def test_markdown_values_and_layout(self, env):
        with home_storefront(env.admin):
            mail = compose('Hi\n{name}', '**Hello** {name}, [go]({url})', (NAME, URL),
                           {'name': Value('<A>'), 'url': Value('https://a.example/')}, 'https://u.example/', 'Stop')
        assert mail.subject == 'Hi <A>'
        assert '<strong>Hello</strong> &lt;A&gt;' in mail.html_body
        assert re.search(r'<a href="https://a.example/"[^>]*>go</a>', mail.html_body)
        assert '<a href="https://u.example/">Stop</a>' in mail.html_body
        assert '**Hello** <A>, [go](https://a.example/)' in mail.text_body and 'Stop: https://u.example/' in mail.text_body

    def test_a_value_is_never_read_as_markdown(self, env):
        with home_storefront(env.admin):
            mail = compose('s', 'Hi {name}', (NAME,), {'name': Value('**bold** [x](https://evil.example/)')})
        assert '<strong>' not in mail.html_body and 'href="https://evil.example/' not in mail.html_body

    def test_only_its_own_placeholders_are_filled(self, env):
        with home_storefront(env.admin):
            mail = compose('{name} {other}', 'x', (NAME,), {'name': Value('A'), 'other': Value('B')})
        assert mail.subject == 'A {other}'

    def test_no_footer_without_an_unsubscribe_link(self, env):
        with home_storefront(env.admin):
            mail = compose('s', 'x', (), {})
        assert 'Unsubscribe' not in mail.html_body and 'href="None"' not in mail.html_body

    def test_no_row_no_override(self, env):
        assert overridden('welcome', {}) is None

    def test_sample_values_render_lists(self):
        values = sample_values((NAME, Placeholder('items', 'x', '- one\n- two', 'list')))
        assert values['name'] == Value('Alex')
        assert values['items'].html == '<ul><li>one</li><li>two</li></ul>'


# ---------------------------------------------------------------------------------------------------------------------
# The built-in emails in the admin's words (D3, D4)

def override(key, subject, body):
    db.session.merge(EmailTemplate(key=key, subject=subject, body=body))
    db.session.commit()


class TestBuiltInsUseTheAdminsText:
    def test_password_reset(self, env):
        user = member(env, 'forgetful')
        override('password_reset', 'Reset at {site_name}', 'Hi {name}: {reset_url}')
        sent = Sent()
        with patch('app.email.send_email', sent):
            send_password_reset_email(user)
        assert sent[0]['subject'].startswith('Reset at ') and '{' not in sent[0]['subject']
        assert '/auth/reset_password/' in sent[0]['text_body'] and 'Hi forgetful:' in sent[0]['text_body']
        assert re.search(r'<a href="[^"]*/auth/reset_password/[^"]+">', sent[0]['html_body'])

    def test_verification(self, env):
        user = member(env, 'newbie')
        override('verify_email', 'Check it, {name}', 'Open {verify_url}')
        sent = Sent()
        with patch('app.email.send_email', sent):
            send_verification_email(user)
        assert sent[0]['subject'] == 'Check it, newbie'
        assert f'/auth/verify_email/{user.verification_token}' in sent[0]['text_body']

    def test_the_defaults_without_one(self, env):
        user = member(env, 'plain')
        sent = Sent()
        with patch('app.email.send_email', sent):
            send_password_reset_email(user)
            send_verification_email(user)
            send_registration_approved_email(user)
        assert [mail['subject'] for mail in sent] == [
            '[PieFed] Reset Your Password', '[PieFed] Please verify your email address',
            'Your application has been approved - welcome to PieFed']
        assert sent[2]['list_unsubscribe'] is None   # the default welcome is as it was

    def test_welcome_comes_before_the_older_setting(self, env):
        user = member(env, 'joiner')
        set_setting('registration_approved_email', 'the **setting**')
        override('welcome', 'Welcome to {site_name}', 'Hi {name}, start at {site_url}')
        sent = Sent()
        with patch('app.email.send_email', sent):
            send_registration_approved_email(user)
        assert sent[0]['subject'].startswith('Welcome to ')
        assert 'setting' not in sent[0]['html_body'] and 'Hi joiner, start at https://' in sent[0]['text_body']
        assert sent[0]['list_unsubscribe'].endswith(f'/user/newsletter/{user.id}/tok{user.id}/unsubscribe')
        assert 'Unsubscribe from the newsletter' in sent[0]['html_body']

    def test_welcome_with_no_token_has_no_unsubscribe(self, env):
        user = member(env, 'tokenless', verification_token=None)
        override('welcome', 'Welcome', 'Hi')
        sent = Sent()
        with patch('app.email.send_email', sent):
            send_registration_approved_email(user)
        assert sent[0]['list_unsubscribe'] is None and 'Unsubscribe' not in sent[0]['html_body']

    def test_the_older_setting_still_works_without_one(self, env):
        user = member(env, 'joiner')
        set_setting('registration_approved_email', 'the **setting**')
        sent = Sent()
        with patch('app.email.send_email', sent):
            send_registration_approved_email(user)
        assert '<strong>setting</strong>' in sent[0]['html_body']

    def test_community_invite(self, env):
        community = make_community('gardening', host=DEFAULT_HOST)
        community.instance_id = env.baseline.instance_local.id
        db.session.commit()
        override('community_invite', '{inviter} invites you', 'Join {community}: {join_url} or {register_url}')
        sent = Sent()
        with env.app.test_request_context(), patch('app.shared.community.send_email', sent), \
                patch('flask_login.utils._get_user', return_value=env.admin), \
                patch('app.models.Community.can_invite', return_value=True):
            assert invite_with_email(community.id, 'friend@example.org', 'web') == 1
        assert sent[0]['subject'] == 'siteadmin invites you'
        assert f'/c/{community.link()}/subscribe' in sent[0]['text_body']
        assert '/auth/register' in sent[0]['html_body']

    def test_community_invite_by_default(self, env):
        community = make_community('gardening', host=DEFAULT_HOST)
        sent = Sent()
        with env.app.test_request_context(), patch('app.shared.community.send_email', sent), \
                patch('flask_login.utils._get_user', return_value=env.admin), \
                patch('app.models.Community.can_invite', return_value=True):
            invite_with_email(community.id, 'friend@example.org', 'web')
        assert sent[0]['subject'].startswith(community.display_name())


class TestTheUnreadNotificationsEmail:
    def setup_unread(self, env):
        user = member(env, 'away', email_unread=True, email_unread_sent=False, last_seen=utcnow() - timedelta(days=2))
        db.session.add(Notification(user_id=user.id, title='Somebody <replied>', url='/x', notif_type=NOTIF_POST,
                                    created_at=utcnow() - timedelta(hours=1)))
        db.session.commit()
        g.pop('site', None)
        return user

    def run(self):
        sent = Sent()
        with patch('app.cli.send_email', sent):
            cli.register(current_app)
            result = current_app.test_cli_runner().invoke(args=['send_missed_notifs'])
        assert result.exception is None, result.exception
        return sent

    def test_in_the_admins_words(self, env):
        user = self.setup_unread(env)
        override('unread_notifications', 'You missed things, {name}', 'Look:\n\n{notifications}\n\n{recent_posts}')
        sent = self.run().to(user.email)
        assert sent[0]['subject'] == 'You missed things, away'
        assert '- Somebody <replied> - http' in sent[0]['text_body']
        assert re.search(r'<ul><li><a href="[^"]*/notification/\d+/goto">Somebody &lt;replied&gt;</a></li></ul>',
                         sent[0]['html_body'])
        assert sent[0]['list_unsubscribe'].endswith(f'/user/email_notifs/{user.id}/tok{user.id}/unsubscribe')
        assert 'Unsubscribe from these emails' in sent[0]['html_body']

    def test_by_default(self, env):
        user = self.setup_unread(env)
        sent = self.run().to(user.email)
        assert sent[0]['subject'] == '[PieFed] You have unread notifications'

    def test_the_lists_values(self, env):
        user = member(env, 'reader')
        post = Post(title='A <post>', slug='/c/x/p/1/a-post')
        other = Post(id=987654, title='Another', slug=None)
        notification = Notification(id=55, title='n', url='/n')
        with home_storefront(user):
            values = digest_values(user, [notification], [post, other])
            empty = digest_values(user, [notification], [])
        assert values['recent_posts'].text.splitlines()[0].endswith('/c/x/p/1/a-post')
        assert '/post/987654' in values['recent_posts'].text
        assert '<a href="https://' in values['recent_posts'].html and 'A &lt;post&gt;' in values['recent_posts'].html
        assert empty['recent_posts'] == Value('', '')
        assert values['notifications_url'].text.endswith('/notifications')


# ---------------------------------------------------------------------------------------------------------------------
# Who a message reaches (D12-D14, MS4)

class TestTheAudience:
    def test_everyone_who_takes_the_newsletter(self, env):
        member(env, 'reader')
        member(env, 'optedout', newsletter=False)
        member(env, 'unverified', verified=False)
        member(env, 'banned', banned=True)
        member(env, 'gone', deleted=True)
        member(env, 'noaddress', email=None)
        member(env, 'blankaddress', email='')
        remote = make_user(make_instance('far.example'), 'faraway')
        remote.newsletter = True
        db.session.commit()
        assert reached(a_message()) == {'reader'}

    def test_a_service_notice_reaches_those_who_opted_out(self, env):
        member(env, 'reader')
        member(env, 'optedout', newsletter=False)
        member(env, 'banned', banned=True, newsletter=False)
        assert reached(a_message(service_notice=True)) & {'reader', 'optedout', 'banned', 'siteadmin'} == {
            'reader', 'optedout', 'siteadmin'}
        assert reached(a_message()) & {'reader', 'optedout', 'siteadmin'} == {'reader'}

    def test_picked(self, env):
        member(env, 'reader')
        chosen = member(env, 'chosen')
        banned = member(env, 'banned', banned=True)
        message = a_message(audience='picked')
        db.session.add_all([AdminMessagePick(message_id=message.id, user_id=chosen.id),
                            AdminMessagePick(message_id=message.id, user_id=banned.id)])
        db.session.commit()
        assert reached(message) == {'chosen'}

    def test_by_domain_the_defaults_include_those_with_none(self, env):
        member(env, 'nodomain', site_domain_id=None)
        member(env, 'ondefault', site_domain_id=env.default.id)
        member(env, 'onother', site_domain_id=env.other.id)
        assert reached(a_message(audience='filtered', domain_ids=[env.other.id])) == {'onother'}
        assert reached(a_message(audience='filtered', domain_ids=[env.default.id])) == {'nodomain', 'ondefault'}
        assert reached(a_message(audience='filtered', domain_ids=[env.default.id, env.other.id])) == {
            'nodomain', 'ondefault', 'onother'}

    def test_by_role_while_it_is_held(self, env):
        role = Role(name='Gardeners')
        db.session.add(role)
        db.session.commit()
        holder, lapsed = member(env, 'holder'), member(env, 'lapsed')
        member(env, 'outsider')
        db.session.add_all([RoleGrant(user_id=holder.id, role_id=role.id),
                            RoleGrant(user_id=lapsed.id, role_id=role.id, expires_at=utcnow() - timedelta(days=1)),
                            ])
        db.session.commit()
        later = member(env, 'later')
        db.session.add(RoleGrant(user_id=later.id, role_id=role.id, expires_at=utcnow() + timedelta(days=1)))
        db.session.commit()
        assert reached(a_message(audience='filtered', role_ids=[role.id])) == {'holder', 'later'}

    def subscribed(self, env):
        plan = Offer(provider_kind='btcpay', kind='subscription', title='Plan', offer_group='g')
        other = Offer(provider_kind='btcpay', kind='subscription', title='Other', offer_group='h')
        db.session.add_all([plan, other])
        db.session.commit()
        paying, other_plan, ended = member(env, 'paying'), member(env, 'otherplan'), member(env, 'ended')
        member(env, 'never')
        for user, offer, status in ((paying, plan, 'active'), (other_plan, other, 'active'), (ended, plan, 'ended')):
            db.session.add(MemberSubscription(user_id=user.id, offer_id=offer.id, provider_kind='btcpay',
                                              provider_reference=f'ref{user.id}', status=status))
        db.session.commit()
        return plan

    def test_by_subscription(self, env):
        plan = self.subscribed(env)
        assert reached(a_message(audience='filtered', subscription='any')) == {'paying', 'otherplan'}
        assert reached(a_message(audience='filtered', subscription='none')) == {'ended', 'never'}
        assert reached(a_message(audience='filtered', subscription='offers', offer_ids=[plan.id])) == {'paying'}
        assert reached(a_message(audience='filtered', subscription='offers', offer_ids=None)) == set()

    def test_filters_together_must_all_match(self, env):
        self.subscribed(env)
        User.query.filter_by(user_name='paying').one().site_domain_id = env.other.id
        User.query.filter_by(user_name='otherplan').one().site_domain_id = env.default.id
        db.session.commit()
        assert reached(a_message(audience='filtered', subscription='any', domain_ids=[env.other.id])) == {'paying'}

    def test_no_filters_is_everyone(self, env):
        member(env, 'reader')
        assert reached(a_message(audience='filtered')) == {'reader'}
        assert recipient_count(a_message(audience='filtered')) == 1


# ---------------------------------------------------------------------------------------------------------------------
# Sending (D15-D18)

class TestRendering:
    def test_a_copy_names_its_reader_and_can_be_unsubscribed(self, env):
        user = member(env, 'reader')
        message = a_message(body='Hi {name} of {site_name} at {site_url}')
        with home_storefront(user):
            mail, unsubscribe = render_for(message, user)
        assert 'Hi reader of' in mail.text_body and 'https://' in mail.text_body
        assert unsubscribe.endswith(f'/user/newsletter/{user.id}/tok{user.id}/unsubscribe')

    def test_a_service_notice_has_no_unsubscribe(self, env):
        user = member(env, 'reader')
        with home_storefront(user):
            mail, unsubscribe = render_for(a_message(service_notice=True), user)
        assert unsubscribe is None and 'Unsubscribe' not in mail.html_body

    def test_no_token_no_link(self, env):
        user = member(env, 'reader', verification_token=None)
        with home_storefront(user):
            _mail, unsubscribe = render_for(a_message(), user)
        assert unsubscribe is None


class TestSendingFrom:
    """MS6, D24: every copy from one picked domain, or each from the reader's home domain."""

    def names(self, env):
        configure(env.default, name='Home Site')
        configure(env.other, name='Other Site')
        db.session.commit()
        clear_domain_cache()
        cache.clear()

    def test_by_default_each_reader_gets_it_from_their_home_domain(self, env):
        self.names(env)
        at_home, away = member(env, 'athome'), member(env, 'away', site_domain_id=env.other.id)
        message = a_message(body='{site_name} at {site_url}', status='sending')
        assert messaging.sent_from(message, at_home).id == env.default.id
        assert messaging.sent_from(message, away).id == env.other.id
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            deliver(message.id)
        (home_copy,), (away_copy,) = sent.to(at_home.email), sent.to(away.email)
        assert home_copy['sender'].startswith('Home Site <') and f'Home Site at https://{DEFAULT_HOST}' in \
            home_copy['text_body']
        assert away_copy['sender'].startswith('Other Site <') and 'Other Site at https://other.example' in \
            away_copy['text_body']

    def test_a_picked_domain_names_every_copy_and_its_links(self, env):
        self.names(env)
        at_home, away = member(env, 'athome'), member(env, 'away', site_domain_id=env.other.id)
        message = a_message(body='{site_name} at {site_url}', status='sending', from_domain_id=env.other.id)
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            assert deliver(message.id) == 2
        for user in (at_home, away):
            (mail,) = sent.to(user.email)
            assert mail['sender'] == f'Other Site <{current_app.config["MAIL_FROM"]}>'
            assert 'Other Site at https://other.example' in mail['text_body']
            assert mail['list_unsubscribe'].startswith('https://other.example/user/newsletter/')
        assert 'Home Site' not in str(sent)

    def test_the_picked_domain_is_saved_shown_and_copied(self, env):
        response = post(env, '/admin/messaging/new', MESSAGE, from_domain=env.other.id, save='y')
        message = AdminMessage.query.one()
        assert response.status_code == 302 and message.from_domain_id == env.other.id
        body = text_of(env.client.get(f'/admin/messaging/{message.id}'))
        assert re.search(rf'<option selected value="{env.other.id}">other.example</option>', body) or \
            re.search(rf'<option value="{env.other.id}" selected>other.example</option>', body)
        message.status, message.sent_at = 'sent', utcnow()
        db.session.commit()
        body = text_of(env.client.get(f'/admin/messaging/{message.id}'))
        assert re.search(r'id="message_from_domain">other.example<', body)
        post(env, f'/admin/messaging/{message.id}/duplicate')
        copy = AdminMessage.query.filter(AdminMessage.id != message.id).one()
        assert copy.from_domain_id == env.other.id

    def test_home_domain_is_the_default_choice_and_stores_none(self, env):
        body = text_of(env.client.get('/admin/messaging/new'))
        assert 'name="from_domain"' in body and "Each member&#39;s home domain" in body
        post(env, '/admin/messaging/new', MESSAGE, from_domain=0, save='y')
        message = AdminMessage.query.one()
        assert message.from_domain_id is None
        message.status, message.sent_at = 'sent', utcnow()
        db.session.commit()
        assert "id=\"message_from_domain\">Each member&#39;s home domain<" in \
            text_of(env.client.get(f'/admin/messaging/{message.id}'))

    def test_a_domain_that_is_not_one_is_refused(self, env):
        response = post(env, '/admin/messaging/new', MESSAGE, from_domain=999999, save='y')
        assert response.status_code == 200 and AdminMessage.query.count() == 0

    def test_the_test_copy_comes_from_the_picked_domain(self, env):
        self.names(env)
        env.admin.verification_token = 'tokadmin'
        db.session.commit()
        message = a_message(from_domain_id=env.other.id)
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            messaging.mail_one(message, env.admin)
        assert sent[0]['sender'].startswith('Other Site <')

    def logos(self, env):
        configure(env.default, logo='/static/media/home_100.png')
        configure(env.other, logo='/static/media/other_100.png')
        db.session.commit()
        clear_domain_cache()
        cache.clear()

    def test_each_copy_carries_its_home_domains_logo_by_default(self, env):
        self.logos(env)
        at_home, away = member(env, 'athome'), member(env, 'away', site_domain_id=env.other.id)
        message = a_message(status='sending')
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            deliver(message.id)
        assert f'src="https://{DEFAULT_HOST}/static/media/home_100.png"' in sent.to(at_home.email)[0]['html_body']
        assert 'src="https://other.example/static/media/other_100.png"' in sent.to(away.email)[0]['html_body']

    def test_every_copy_carries_the_picked_domains_logo(self, env):
        self.logos(env)
        at_home, away = member(env, 'athome'), member(env, 'away', site_domain_id=env.other.id)
        message = a_message(status='sending', from_domain_id=env.other.id)
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            deliver(message.id)
        for user in (at_home, away):
            html = sent.to(user.email)[0]['html_body']
            assert 'src="https://other.example/static/media/other_100.png"' in html and 'home_100' not in html

    def test_the_preview_is_laid_out_as_from_the_picked_domain(self, env):
        self.logos(env)
        message = a_message(from_domain_id=env.other.id)
        preview = self.preview(env, message)
        assert 'other.example/static/media/other_100.png' in preview and 'home_100' not in preview

    def test_the_preview_from_home_domains_is_laid_out_as_this_page_s_domain(self, env):
        self.logos(env)
        message = a_message()
        preview = self.preview(env, message)
        assert 'static/media/home_100.png' in preview and 'other_100' not in preview

    @staticmethod
    def preview(env, message) -> str:
        """The preview frame's document alone: the page round it shows the page's own domain's logo."""
        (srcdoc,) = re.findall(r'srcdoc="([^"]*)"', text_of(env.client.get(f'/admin/messaging/{message.id}')))
        return srcdoc

    def test_a_deleted_domain_falls_back_to_home_domains(self, env):
        message = a_message(from_domain_id=env.other.id)
        db.session.execute(db.text('DELETE FROM domain_config WHERE site_domain_id = :id'), {'id': env.other.id})
        db.session.delete(env.other)
        db.session.commit()
        db.session.refresh(message)
        assert message.from_domain_id is None


class TestDispatch:
    def test_only_what_is_due_is_claimed_once(self, env):
        due = a_message(status='scheduled', scheduled_for=utcnow() - timedelta(minutes=1))
        a_message(status='scheduled', scheduled_for=utcnow() + timedelta(hours=1))
        a_message(status='draft')
        a_message(status='sent')
        assert claim_due() == [due.id]
        assert claim_due() == []
        db.session.refresh(due)
        assert due.status == 'sending' and due.heartbeat_at is not None

    def test_a_dead_sending_task_is_taken_over(self, env):
        stale = a_message(status='sending', heartbeat_at=utcnow() - timedelta(minutes=16))
        silent = a_message(status='sending', heartbeat_at=None)
        a_message(status='sending', heartbeat_at=utcnow() - timedelta(minutes=1))
        assert claim_due() == sorted([stale.id, silent.id])

    def test_send_queue_hands_each_to_a_task(self, env):
        due = a_message(status='scheduled', scheduled_for=utcnow())
        with patch('app.messaging.messages.send_admin_message') as task:
            assert dispatch_due() == 1
        task.delay.assert_called_once_with(due.id)

    def test_send_queue_sends_what_is_due(self, env):
        """It runs every few minutes from cron (D15)."""
        reader = member(env, 'reader')
        due = a_message(status='scheduled', scheduled_for=utcnow() - timedelta(minutes=1))
        if 'send-queue' not in env.app.cli.commands:
            cli.register(env.app)
        sent = Sent()
        with patch('app.cli.app_pkg.redis_client') as redis, patch('app.messaging.messages.send_email', sent):
            redis.memory_stats.return_value = {'total.allocated': 0}
            result = env.app.test_cli_runner().invoke(args=['send-queue'])
        assert result.exit_code == 0, (result.output, result.exception)
        db.session.refresh(due)
        assert due.status == 'sent' and [mail['recipients'] for mail in sent] == [[reader.email]]


class TestDelivery:
    def test_each_recipient_once_and_then_it_is_sent(self, env):
        first, second = member(env, 'first'), member(env, 'second')
        message = a_message(status='sending')
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            assert deliver(message.id) == 2
        assert sorted(mail['recipients'][0] for mail in sent) == sorted([first.email, second.email])
        db.session.refresh(message)
        assert (message.status, message.sent_count, message.failed_count) == ('sent', 2, 0)
        assert message.sent_at is not None and message.heartbeat_at is None
        assert all(row.sent_at for row in AdminMessageDelivery.query.filter_by(message_id=message.id))

    def test_a_second_run_mails_nobody_twice(self, env):
        member(env, 'first')
        message = a_message(status='sending')
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            deliver(message.id)
            message.status = 'sending'
            db.session.commit()
            assert deliver(message.id) == 0
        assert len(sent) == 1

    def test_one_claimed_by_another_run_is_skipped(self, env):
        first, second = member(env, 'first'), member(env, 'second')
        message = a_message(status='sending')
        db.session.add(AdminMessageDelivery(message_id=message.id, user_id=first.id))
        db.session.commit()
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            deliver(message.id)
        assert [mail['recipients'] for mail in sent] == [[second.email]]

    def test_a_failure_is_recorded_and_the_rest_still_go(self, env):
        first, second = member(env, 'first'), member(env, 'second')
        message = a_message(status='sending')

        def flaky(subject, **kwargs):
            if kwargs['recipients'] == [first.email]:
                raise OSError('down')
        with patch('app.messaging.messages.send_email', flaky):
            assert deliver(message.id) == 1
        db.session.refresh(message)
        assert (message.sent_count, message.failed_count) == (1, 1)
        assert db.session.get(AdminMessageDelivery, (message.id, first.id)).error == 'OSError'
        assert db.session.get(AdminMessageDelivery, (message.id, second.id)).sent_at is not None

    def test_a_heartbeat_while_it_sends(self, env, monkeypatch):
        monkeypatch.setattr(messaging, 'HEARTBEAT_EVERY', 1)
        member(env, 'first')
        message = a_message(status='sending')
        beats = []
        original = messaging.utcnow
        monkeypatch.setattr(messaging, 'utcnow', lambda: beats.append(1) or original())
        with patch('app.messaging.messages.send_email'):
            deliver(message.id)
        assert len(beats) >= 3   # claim, sent, heartbeat (and the end)

    def test_nothing_to_do(self, env):
        assert deliver(123456) == 0
        assert deliver(a_message(status='draft').id) == 0

    def test_the_task(self, env):
        member(env, 'first')
        message = a_message(status='sending')
        with patch('app.messaging.messages.send_email'):
            assert send_admin_message(message.id) == 1

    def test_the_task_rolls_back_on_failure(self, env):
        with patch('app.messaging.messages.deliver', side_effect=RuntimeError('no')), pytest.raises(RuntimeError):
            send_admin_message(1)

    def test_send_now(self, env):
        member(env, 'first')
        message = a_message()
        with patch('app.messaging.messages.send_email') as send:
            send_now(message)
        db.session.refresh(message)
        assert message.status == 'sent' and send.call_count == 1

    def test_unschedule_only_what_has_not_begun(self, env):
        scheduled = a_message(status='scheduled', scheduled_for=utcnow() + timedelta(hours=1))
        sending = a_message(status='sending')
        assert unschedule(scheduled) and (scheduled.status, scheduled.scheduled_for) == ('draft', None)
        assert not unschedule(sending) and sending.status == 'sending'


# ---------------------------------------------------------------------------------------------------------------------
# The pages (D11, D18, D21, D22)

def post(env, path, data=None, client=None, **fields):
    data = dict(data or {}, csrf_token=csrf(env, client), **fields)
    return (client or env.client).post(path, data=data)


def text_of(response):
    return response.get_data(as_text=True)


MESSAGE = {'subject': 'Hello', 'body': 'Hi {name}', 'audience': 'all'}


class TestTheMessagingPage:
    def test_it_lists_every_email_and_when(self, env):
        override('welcome', 'W', 'w')
        a_message(subject='A draft')
        a_message(subject='Sent one', status='sent', sent_at=utcnow(), sent_count=3, failed_count=1,
                  service_notice=True)
        a_message(subject='Later', status='scheduled', scheduled_for=utcnow() + timedelta(days=1))
        body = text_of(env.client.get('/admin/messaging'))
        for entry in CATALOGUE:
            assert f'id="email_{entry.key}"' in body
        assert 'Once a day, from 10 days before' in body
        assert body.index('Later') < body.index('A draft') < body.index('Sent one')
        assert 'Your text' in body and 'Service notice' in body and '1 failed' in body
        assert '/admin/messaging/email/welcome' not in body   # needs `edit email templates`

    def test_the_edit_links_with_the_permission(self, env):
        grant_permission(env.admin, 'edit email templates')
        body = text_of(env.client.get('/admin/messaging'))
        assert '/admin/messaging/email/welcome' in body and '/admin/messaging/email/test_email' not in body

    def test_the_old_address_leads_here(self, env):
        response = env.client.get('/admin/newsletter')
        assert response.status_code == 302 and response.location.endswith('/admin/messaging')

    def test_the_nav_says_messaging(self, env):
        body = text_of(env.client.get('/admin/messaging'))
        assert 'id="messaging-tab"' in body and 'Newsletter' not in body

    def test_nobody_else_gets_in(self, env):
        stranger = member(env, 'stranger')
        client = env.app.test_client()
        login(client, stranger)
        message = a_message()
        for path in ('/admin/messaging', '/admin/messaging/new', f'/admin/messaging/{message.id}',
                     '/admin/messaging/email/welcome'):
            assert client.get(path).status_code == 302
        for path in (f'/admin/messaging/{message.id}/delete', f'/admin/messaging/{message.id}/unschedule',
                     f'/admin/messaging/{message.id}/duplicate', '/admin/messaging/email/welcome/reset'):
            assert post(env, path, client=client).status_code == 302
        assert db.session.get(AdminMessage, message.id) is not None


class TestWritingAMessage:
    def test_the_blank_form(self, env):
        body = text_of(env.client.get('/admin/messaging/new'))
        assert 'name="audience"' in body and 'other.example' in body
        assert '{site_url}' in body and 'Service notice' in body

    def test_a_draft_is_saved_and_shown_again(self, env):
        role = Role(name='Gardeners')
        plan = Offer(provider_kind='btcpay', kind='subscription', title='Plan', offer_group='g')
        db.session.add_all([role, plan])
        db.session.commit()
        response = post(env, '/admin/messaging/new', MESSAGE, audience='filtered', domains=[env.other.id],
                        roles=[role.id], subscription='offers', offers=[plan.id], service_notice='y', save='y')
        message = AdminMessage.query.one()
        assert response.status_code == 302 and response.location.endswith(f'/admin/messaging/{message.id}')
        assert (message.status, message.audience, message.domain_ids, message.role_ids, message.subscription,
                message.offer_ids, message.service_notice, message.created_by) == \
            ('draft', 'filtered', [env.other.id], [role.id], 'offers', [plan.id], True, env.admin.id)
        body = text_of(env.client.get(f'/admin/messaging/{message.id}'))
        assert 'Draft: not sent.' in body and 'It would reach' in body and 'Preview, with sample values' in body
        assert 'Hi Alex' in body
        assert re.search(r'<input[^>]*(checked[^>]*value="filtered"|value="filtered"[^>]*checked)', body)

    def test_filters_are_dropped_unless_filtered(self, env):
        post(env, '/admin/messaging/new', MESSAGE, domains=[env.other.id], subscription='any', save='y')
        message = AdminMessage.query.one()
        assert (message.domain_ids, message.role_ids, message.subscription, message.offer_ids) == (
            None, None, None, None)

    def test_picked_accounts(self, env):
        chosen = member(env, 'chosen')
        post(env, '/admin/messaging/new', MESSAGE, audience='picked', picked='chosen\n\n chosen \n', save='y')
        message = AdminMessage.query.one()
        assert [pick.user_id for pick in AdminMessagePick.query.filter_by(message_id=message.id)] == [chosen.id]
        assert 'chosen' in text_of(env.client.get(f'/admin/messaging/{message.id}'))
        post(env, f'/admin/messaging/{message.id}', MESSAGE, save='y')
        assert AdminMessagePick.query.count() == 0

    @pytest.mark.parametrize('fields, problem', [
        ({'body': 'Hi {nobody}'}, '{nobody} is not a placeholder'),
        ({'audience': 'picked', 'picked': ''}, 'List at least one account'),
        ({'audience': 'picked', 'picked': 'ghost'}, 'No local account is called ghost'),
        ({'audience': 'filtered', 'subscription': 'offers'}, 'Choose at least one plan'),
        ({'schedule': 'y'}, 'Choose when to send it'),
        ({'schedule': 'y', 'scheduled_for': '2000-01-01T00:00'}, 'Choose a time in the future'),
    ])
    def test_what_cannot_be_saved(self, env, fields, problem):
        data = dict(MESSAGE, save='y')
        data.update(fields)
        response = post(env, '/admin/messaging/new', data)
        assert response.status_code == 200 and problem in text_of(response)
        assert AdminMessage.query.count() == 0

    def test_too_many_picks(self, env, monkeypatch):
        monkeypatch.setattr('app.admin.messaging_routes.MAX_PICKS', 1)
        member(env, 'one')
        member(env, 'two')
        response = post(env, '/admin/messaging/new', MESSAGE, audience='picked', picked='one\ntwo', save='y')
        assert 'List at most 1 accounts' in text_of(response)

    def test_scheduled(self, env):
        when = (utcnow() + timedelta(days=1)).replace(second=0, microsecond=0)
        response = post(env, '/admin/messaging/new', MESSAGE, schedule='y', scheduled_for=when.strftime('%Y-%m-%dT%H:%M'))
        message = AdminMessage.query.one()
        assert response.location.endswith('/admin/messaging')
        assert (message.status, message.scheduled_for) == ('scheduled', when)
        body = text_of(env.client.get(f'/admin/messaging/{message.id}'))
        assert 'Scheduled for' in body and 'Unschedule' in body and 'Delete draft' not in body

    def test_saving_a_scheduled_one_keeps_it_scheduled_at_the_time_shown(self, env):
        message = a_message(status='scheduled', scheduled_for=utcnow() + timedelta(days=1))
        later = (utcnow() + timedelta(days=2)).replace(second=0, microsecond=0)
        post(env, f'/admin/messaging/{message.id}', MESSAGE, save='y', scheduled_for=later.strftime('%Y-%m-%dT%H:%M'))
        db.session.refresh(message)
        assert (message.status, message.scheduled_for, message.subject) == ('scheduled', later, 'Hello')

    def test_saving_a_scheduled_one_without_a_time_keeps_its_time(self, env):
        when = (utcnow() + timedelta(days=1)).replace(microsecond=0)
        message = a_message(status='scheduled', scheduled_for=when)
        post(env, f'/admin/messaging/{message.id}', MESSAGE, save='y')
        db.session.refresh(message)
        assert message.scheduled_for == when

    def test_a_scheduled_one_cannot_be_moved_into_the_past(self, env):
        message = a_message(status='scheduled', scheduled_for=utcnow() + timedelta(days=1))
        response = post(env, f'/admin/messaging/{message.id}', MESSAGE, save='y', scheduled_for='2000-01-01T00:00')
        assert 'Choose a time in the future' in text_of(response)

    def test_a_test_to_me(self, env):
        member(env, 'reader')
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            response = post(env, '/admin/messaging/new', MESSAGE, send_test='y')
        assert [mail['recipients'] for mail in sent] == [[env.admin.email]]
        assert AdminMessage.query.one().status == 'draft' and response.status_code == 302

    def test_send_now(self, env):
        reader = member(env, 'reader')
        sent = Sent()
        with patch('app.messaging.messages.send_email', sent):
            response = post(env, '/admin/messaging/new', MESSAGE, send_now='y')
        assert response.location.endswith('/admin/messaging')
        assert [mail['recipients'] for mail in sent] == [[reader.email]]
        message = AdminMessage.query.one()
        assert message.status == 'sent'
        body = text_of(env.client.get(f'/admin/messaging/{message.id}'))
        assert 'Sent ' in body and 'to 1 members' in body and 'name="subject"' not in body

    def test_a_sent_one_cannot_be_changed(self, env):
        message = a_message(status='sent', sent_at=utcnow(), failed_count=2)
        assert post(env, f'/admin/messaging/{message.id}', MESSAGE, save='y').status_code == 409
        assert '2 could not be sent' in text_of(env.client.get(f'/admin/messaging/{message.id}'))

    def test_one_being_sent_shows_its_count(self, env):
        message = a_message(status='sending', sent_count=4)
        assert 'Sending: 4 so far.' in text_of(env.client.get(f'/admin/messaging/{message.id}'))

    def test_an_unknown_one(self, env):
        assert env.client.get('/admin/messaging/424242').status_code == 404


class TestMessageActions:
    def test_unschedule(self, env):
        message = a_message(status='scheduled', scheduled_for=utcnow() + timedelta(days=1))
        response = post(env, f'/admin/messaging/{message.id}/unschedule')
        db.session.refresh(message)
        assert response.status_code == 302 and message.status == 'draft'
        post(env, f'/admin/messaging/{message.id}/unschedule')
        assert 'sending has already begun' in text_of(env.client.get(f'/admin/messaging/{message.id}'))

    def test_delete_only_a_draft(self, env):
        sent = a_message(status='sent', sent_at=utcnow())
        post(env, f'/admin/messaging/{sent.id}/delete')
        assert db.session.get(AdminMessage, sent.id) is not None
        draft = a_message()
        response = post(env, f'/admin/messaging/{draft.id}/delete')
        assert response.location.endswith('/admin/messaging')
        db.session.expire_all()
        assert db.session.get(AdminMessage, draft.id) is None

    def test_duplicate_into_a_draft(self, env):
        chosen = member(env, 'chosen')
        original = a_message(status='sent', audience='picked', service_notice=True, sent_at=utcnow())
        db.session.add(AdminMessagePick(message_id=original.id, user_id=chosen.id))
        db.session.commit()
        response = post(env, f'/admin/messaging/{original.id}/duplicate')
        copy = AdminMessage.query.filter(AdminMessage.id != original.id).one()
        assert response.location.endswith(f'/admin/messaging/{copy.id}')
        assert (copy.status, copy.subject, copy.service_notice, copy.audience) == ('draft', 'News', True, 'picked')
        assert AdminMessagePick.query.filter_by(message_id=copy.id).one().user_id == chosen.id

    @pytest.mark.parametrize('action', ['unschedule', 'delete', 'duplicate'])
    def test_each_needs_its_token(self, env, action, monkeypatch):
        monkeypatch.setitem(env.app.config, 'WTF_CSRF_ENABLED', True)
        message = a_message()
        assert env.client.post(f'/admin/messaging/{message.id}/{action}').status_code == 400


class TestEditingABuiltIn:
    @pytest.fixture(autouse=True)
    def may_edit(self, env):
        make_admin(env.admin)   # the sign-in emails are the built-in Admin's (D23)

    def test_the_page_says_when_and_offers_its_placeholders(self, env):
        body = text_of(env.client.get('/admin/messaging/email/subscription_expiring'))
        assert 'Once a day, from 10 days before' in body and '{days_left}' in body and '{renew_url}' in body
        assert 'The default, translated' in body and 'Reset to the default text' not in body
        assert 'welcome text in the misc settings' in text_of(env.client.get('/admin/messaging/email/welcome'))

    def test_one_that_cannot_be_edited(self, env):
        assert env.client.get('/admin/messaging/email/topic_suggestion').status_code == 404
        assert env.client.get('/admin/messaging/email/nothing').status_code == 404

    def test_preview_saves_nothing(self, env):
        response = post(env, '/admin/messaging/email/password_reset', subject='Reset', body='Go {reset_url}',
                        preview='y')
        assert 'Preview, with sample values' in text_of(response) and 'abc123' in text_of(response)
        assert EmailTemplate.query.count() == 0

    def test_save_and_load(self, env):
        response = post(env, '/admin/messaging/email/password_reset', subject=' Reset ', body='Go {reset_url}',
                        save='y')
        row = db.session.get(EmailTemplate, 'password_reset')
        assert response.status_code == 302 and (row.subject, row.body, row.updated_by) == (
            'Reset', 'Go {reset_url}', env.admin.id)
        body = text_of(env.client.get('/admin/messaging/email/password_reset'))
        assert 'Go {reset_url}' in body and 'Yours, since' in body and 'Reset to the default text' in body
        assert 'Your text' in text_of(env.client.get('/admin/messaging'))

    def test_a_broken_text_is_refused(self, env):
        response = post(env, '/admin/messaging/email/password_reset', subject='Reset', body='Go away', save='y')
        assert '{reset_url} has to be in the text' in text_of(response)
        assert EmailTemplate.query.count() == 0

    def test_save_and_test(self, env):
        sent = Sent()
        with patch('app.admin.messaging_routes.send_email', sent):
            post(env, '/admin/messaging/email/verify_email', subject='Check', body='Go {verify_url}', send_test='y')
        assert sent[0]['recipients'] == [env.admin.email] and 'abc123' in sent[0]['text_body']
        assert db.session.get(EmailTemplate, 'verify_email') is not None

    def test_reset(self, env):
        override('welcome', 'W', 'w')
        response = post(env, '/admin/messaging/email/welcome/reset')
        assert response.status_code == 302 and db.session.get(EmailTemplate, 'welcome') is None
        assert post(env, '/admin/messaging/email/test_email/reset').status_code == 404

    def test_reset_needs_its_token(self, env, monkeypatch):
        monkeypatch.setitem(env.app.config, 'WTF_CSRF_ENABLED', True)
        override('welcome', 'W', 'w')
        assert env.client.post('/admin/messaging/email/welcome/reset').status_code == 400
        assert db.session.get(EmailTemplate, 'welcome') is not None

    def test_the_sign_in_emails_need_the_built_in_admin(self, env):
        editor = member(env, 'editor')
        grant_permission(editor, 'edit email templates')
        client = env.app.test_client()
        login(client, editor)
        assert client.get('/admin/messaging/email/password_reset').status_code == 403
        assert client.get('/admin/messaging/email/verify_email').status_code == 403
        assert post(env, '/admin/messaging/email/password_reset/reset', client=client).status_code == 403
        assert client.get('/admin/messaging/email/welcome').status_code == 200
        grant_permission(editor, 'send newsletter')
        body = text_of(client.get('/admin/messaging'))
        assert '/admin/messaging/email/welcome' in body and '/admin/messaging/email/password_reset' not in body
        assert 'Admin only' in body

    def test_a_token_link_inside_another_url_is_refused(self, env):
        response = post(env, '/admin/messaging/email/password_reset', subject='Reset',
                        body='![x](https://evil.example/{reset_url})', save='y')
        assert 'carries a sign-in token' in text_of(response) and EmailTemplate.query.count() == 0

    def test_send_newsletter_alone_is_not_enough(self, env):
        holder = member(env, 'sender')
        grant_permission(holder, 'send newsletter')
        client = env.app.test_client()
        login(client, holder)
        assert client.get('/admin/messaging').status_code == 200
        assert client.get('/admin/messaging/email/welcome').status_code == 302
