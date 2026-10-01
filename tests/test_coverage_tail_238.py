"""Round 238: the newsletter, and the instance chooser's hide flag.

Two admin surfaces in `app/admin/routes.py` with no rows.

`/admin/newsletter` mails every subscriber -- `newsletter == True`, not banned, local -- up
to 40,000 of them. A mistake here is not a broken page: it is mail to tens of thousands of
strangers, or a test send that goes to all of them instead of to the admin. `send_newsletter`
already stops after the first message in test mode; nothing said so, and a row is what keeps
it that way.

`/admin/instance/<id>/edit` reads and writes a `hide` column in `instance_chooser`, a table
with no model, through raw SQL -- and only for instances whose software is `piefed`. Both
statements were uncovered, so nothing said which instances they apply to or that the write
reaches the right row.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.models import Instance, Site
from tests.factories import grant_permission, make_instance, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.name = 'Test Site'
    g.site = site
    admin = make_user(api_baseline.instance_local, 'siteadmin', local=True)
    admin.verified = True
    admin.private_key = 'x'
    admin.email = 'admin@test.piefed.local'
    grant_permission(admin, 'change instance settings')
    # `/admin/instance/<id>/edit` is gated on a DIFFERENT permission from the newsletter.
    grant_permission(admin, 'administer all communities')
    db.session.commit()
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(admin.id)
        session['_fresh'] = True
    return SimpleNamespace(app=app, site=site, admin=admin, client=client,
                           anonymous=app.test_client(), baseline=api_baseline)


def csrf(app, client):
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def a_subscriber(env, name, email):
    user = make_user(env.baseline.instance_local, name, local=True)
    user.email = email
    user.newsletter = True
    user.verified = True
    db.session.commit()
    return user


# --------------------------------------------------------------------------
# /admin/newsletter
# --------------------------------------------------------------------------


class TestSendingANewsletter:

    def _post(self, env, **fields):
        data = {'subject': 'Hello', 'body_text': 'plain words',
                'body_html': '<p>rich words</p>',
                'csrf_token': csrf(env.app, env.client)}
        data.update(fields)
        sent = []
        # `send_newsletter` calls the `send_email` bound in `app.admin.util`.
        with patch('app.admin.util.send_email',
                   side_effect=lambda **kwargs: sent.append(kwargs)):
            response = env.client.post('/admin/newsletter', data=data)
        return response, sent

    def test_a_real_send_reaches_every_subscriber_once(self, env):
        """The subscriber list is `newsletter == True AND banned == False AND ap_id IS
        NULL`. Each address appears exactly once, and the subject and both bodies are the
        admin's.

        `User.newsletter` DEFAULTS to True, so every local account the fixtures made is
        already a subscriber -- the assertions name the two seeded here and count their
        appearances rather than comparing the whole list.
        """
        first = a_subscriber(env, 'readerone', 'one@example.com')
        second = a_subscriber(env, 'readertwo', 'two@example.com')

        response, sent = self._post(env)

        assert response.status_code == 302
        addresses = sum((call['recipients'] for call in sent), [])
        assert addresses.count(first.email) == 1
        assert addresses.count(second.email) == 1
        assert {call['subject'] for call in sent} == {'Hello'}
        assert all('rich words' in call['html_body'] for call in sent)
        assert all('plain words' in call['text_body'] for call in sent)

    def test_a_test_send_is_one_email_to_the_admin(self, env):
        """`if form.test.data: break`. Without it a test send is one message PER
        SUBSCRIBER, all of them to the admin's own inbox -- the loop is over recipients
        either way, and only the address is swapped. Two subscribers are seeded so that
        'one email' is a claim about the break and not about the list being short."""
        a_subscriber(env, 'readerone', 'one@example.com')
        a_subscriber(env, 'readertwo', 'two@example.com')

        response, sent = self._post(env, test='y')

        assert response.status_code == 302
        assert len(sent) == 1
        assert sent[0]['recipients'] == [env.admin.email]

    def test_a_banned_subscriber_is_not_mailed(self, env):
        """`banned == False`. A banned account is one this instance has decided not to
        deal with, and a newsletter is this instance initiating contact."""
        kept = a_subscriber(env, 'readerone', 'one@example.com')
        banned = a_subscriber(env, 'troublemaker', 'banned@example.com')
        banned.banned = True
        db.session.commit()

        _response, sent = self._post(env)

        addresses = sum((call['recipients'] for call in sent), [])
        assert kept.email in addresses
        assert banned.email not in addresses

    def test_an_account_that_did_not_subscribe_is_not_mailed(self, env):
        """`newsletter == True`. The column is the consent, so this is the row that says
        mail goes only to accounts that asked for it."""
        subscribed = a_subscriber(env, 'readerone', 'one@example.com')
        other = make_user(env.baseline.instance_local, 'quiet', local=True)
        other.email = 'quiet@example.com'
        other.newsletter = False
        db.session.commit()

        _response, sent = self._post(env)

        addresses = sum((call['recipients'] for call in sent), [])
        assert subscribed.email in addresses
        assert other.email not in addresses

    def test_a_remote_account_is_not_mailed(self, env):
        """`ap_id == None`. A remote account's email column holds whatever its own
        instance gave us, and mailing it would be this instance sending to somebody else's
        users."""
        peer = make_instance('mailpeer.example')
        remote = make_user(peer, 'faraway')
        remote.email = 'faraway@mailpeer.example'
        remote.newsletter = True
        db.session.commit()
        local = a_subscriber(env, 'readerone', 'one@example.com')

        _response, sent = self._post(env)

        addresses = sum((call['recipients'] for call in sent), [])
        assert local.email in addresses
        assert remote.email not in addresses

    def test_an_empty_form_sends_nothing(self, env):
        """`form.validate_on_submit()`. Subject and both bodies are `DataRequired`, so a
        half-filled form re-renders the page rather than mailing an empty newsletter."""
        a_subscriber(env, 'readerone', 'one@example.com')

        response, sent = self._post(env, subject='')

        assert response.status_code == 200
        assert sent == []

    def test_an_ordinary_account_cannot_open_the_form(self, env):
        ordinary = make_user(env.baseline.instance_local, 'nosy', local=True)
        ordinary.verified = True
        db.session.commit()
        client = env.app.test_client()
        with client.session_transaction() as session:
            session['_user_id'] = str(ordinary.id)
            session['_fresh'] = True

        response = client.get('/admin/newsletter')

        assert response.status_code == 302


# --------------------------------------------------------------------------
# instance_chooser's hide column
# --------------------------------------------------------------------------


class TestHidingAnInstanceFromTheChooser:
    """`instance_chooser` is a table with no model -- the instance chooser's own listing --
    and the admin's instance form reads and writes its `hide` column through raw SQL. Both
    statements are guarded by `instance.software == 'piefed'`, because only PieFed
    instances are in that table.
    """

    @pytest.fixture
    def seeded(self, env):
        piefed = make_instance('chooser.example')
        piefed.software = 'piefed'
        lemmy = make_instance('lemmy.example')
        lemmy.software = 'lemmy'
        db.session.commit()
        db.session.execute(
            db.text('INSERT INTO instance_chooser (domain, hide) VALUES (:d, false)'),
            {'d': piefed.domain})
        db.session.commit()
        env.piefed = piefed
        env.lemmy = lemmy
        return env

    @staticmethod
    def _hide_checkbox_is_ticked(response):
        import re

        tag = re.search(r'<input[^>]*name="hide"[^>]*>',
                        response.get_data(as_text=True))
        assert tag is not None, 'the hide checkbox is not on the page at all'
        return 'checked' in tag.group(0)

    def _hide_flag(self, domain):
        return db.session.execute(
            db.text('SELECT hide FROM instance_chooser WHERE domain = :d'),
            {'d': domain}).scalar_one_or_none()

    def _edit(self, env, instance, **fields):
        data = {'inbox': f'https://{instance.domain}/inbox',
                'csrf_token': csrf(env.app, env.client)}
        data.update(fields)
        return env.client.post(f'/admin/instance/{instance.id}/edit', data=data)

    def test_the_form_shows_the_stored_flag_for_a_piefed_instance(self, seeded):
        db.session.execute(
            db.text('UPDATE instance_chooser SET hide = true WHERE domain = :d'),
            {'d': seeded.piefed.domain})
        db.session.commit()

        response = seeded.client.get(f'/admin/instance/{seeded.piefed.id}/edit')

        assert response.status_code == 200
        # The page carries several checkboxes, so the assertion names THIS one's input tag:
        # a bare `'checked' in body` is true of a form that never read the stored value.
        assert self._hide_checkbox_is_ticked(response)

        db.session.execute(
            db.text('UPDATE instance_chooser SET hide = false WHERE domain = :d'),
            {'d': seeded.piefed.domain})
        db.session.commit()

        assert not self._hide_checkbox_is_ticked(
            seeded.client.get(f'/admin/instance/{seeded.piefed.id}/edit'))

    def test_saving_the_form_writes_the_flag(self, seeded):
        """The UPDATE names the row by DOMAIN, not by instance id -- the two tables are
        joined by that string -- so this row asserts the flag on the right domain and that
        the other instance is untouched."""
        response = self._edit(seeded, seeded.piefed, hide='y')

        assert response.status_code == 302
        assert self._hide_flag(seeded.piefed.domain) is True

    def test_clearing_it_writes_false(self, seeded):
        """The other value. A checkbox that is absent from the POST is False, so this is
        also the row that shows the write happens at all when nothing is ticked."""
        db.session.execute(
            db.text('UPDATE instance_chooser SET hide = true WHERE domain = :d'),
            {'d': seeded.piefed.domain})
        db.session.commit()

        self._edit(seeded, seeded.piefed)

        assert self._hide_flag(seeded.piefed.domain) is False

    def test_a_non_piefed_instance_is_left_out_of_the_chooser_table(self, seeded):
        """`if instance.software == 'piefed'`. The route goes further than the two SQL
        guards: it `del form.hide` for a non-PieFed instance, so the field does not exist to
        submit. A Lemmy instance has no row in `instance_chooser` either, and editing one
        must still save everything else."""
        response = self._edit(seeded, seeded.lemmy, hide='y', admin_note='a note')

        assert response.status_code == 302
        db.session.expire_all()
        assert db.session.get(Instance, seeded.lemmy.id).admin_note == 'a note'
        assert self._hide_flag(seeded.lemmy.domain) is None
