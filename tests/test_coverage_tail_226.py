"""Round 226: `app/api/alpha/views.py`'s two uncovered clusters.

The file sits at 94.09% with 21 missing lines, and seventeen of them are two blocks of
`if X: v[...] = X` -- optional fields that no test had ever set. An API view that omits a
field when it is falsy and includes it otherwise has one line per field, and each is only
covered by a row that supplies the value.

    post_view          246, 248, 250, 252, 254, 256   an Event's six optional fields
    registration_view  1500, 1503, 1506, 1511, 1513   a registration's five

The Event half is this session's own subject seen from the other end: rounds 212 and 216
fixed what may be STORED in `online_link`, `external_participation_url` and
`buy_tickets_link`; these rows are what the API SERVES from those columns, and they were
unexercised while the storage rules were being rewritten.

The remaining four (`150`, `381-382`, `916`, `1204`, `1370`, `1387`, `1418`, `1480-1481`)
are left for a later round: each needs its own fixture -- a read-posts row with an
`interacted_at`, a federated-instances listing, a modlog cache -- rather than a value on an
object these rows already build.
"""
import os
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.constants import POST_TYPE_EVENT
from app.models import Event, Site, User, UserRegistration, utcnow
from tests.factories import make_community, make_community_member, make_post, make_user


@pytest.fixture
def env(app, api_baseline):
    g.site = db.session.get(Site, 1)
    # `post_view` reaches `g.admin_ids` through the nested user view, and
    # `before_request` -- which populates it -- does not run in these direct calls.
    g.admin_ids = []
    community = make_community('probeland')
    author = make_user(None, 'viewauthor', local=True)
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/v/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author, post=post)


# --------------------------------------------------------------------------
# post_view: an Event's six optional fields
# --------------------------------------------------------------------------


class TestAnEventInThePostView:
    """`post_view` builds `event_data` from the `Event` row and then adds six fields one
    `if` at a time, so a row that leaves them unset covers none of them.

    Each field is asserted BY VALUE rather than by presence: `'online_link' in view` is
    satisfied by a view that copies the wrong column into it, and three of these six are
    the columns rounds 212 and 216 were about.
    """

    @pytest.fixture
    def event_post(self, env):
        env.post.type = POST_TYPE_EVENT
        event = Event(post_id=env.post.id,
                      start=utcnow(), end=utcnow(),
                      timezone='Europe/London', online=True,
                      online_link='https://meet.example/room',
                      external_participation_url='https://tickets.example/e/1',
                      buy_tickets_link='http://shop.example/t/1',
                      event_fee_currency='GBP', event_fee_amount=12.5,
                      location={'address': '1 Street', 'city': 'Town'})
        db.session.add(event)
        db.session.commit()
        return env

    def _view(self, env):
        from app.api.alpha.views import post_view

        return post_view(env.post, variant=2, user_id=env.author.id)['post']['event']

    def test_every_optional_field_is_served_with_its_own_value(self, event_post):
        view = self._view(event_post)

        assert view['online_link'] == 'https://meet.example/room'
        assert view['external_participation_url'] == 'https://tickets.example/e/1'
        assert view['buy_tickets_link'] == 'http://shop.example/t/1'
        assert view['event_fee_currency'] == 'GBP'
        assert view['event_fee_amount'] == 12.5
        assert view['location'] == {'address': '1 Street', 'city': 'Town'}

    def test_an_end_time_is_served_as_an_iso_string_with_a_z(self, event_post):
        """`:244`, the same shape one field up, and the only one of the seven that is
        reformatted rather than copied."""
        view = self._view(event_post)

        assert view['end'].endswith('Z')
        assert 'T' in view['end']

    def test_an_event_with_none_of_them_omits_all_six(self, env):
        """The other arm of every `if`, and the reason they are `if`s: a minimal event
        must not carry six null keys. Without this row a fix that always assigned them
        would pass everything above."""
        env.post.type = POST_TYPE_EVENT
        db.session.add(Event(post_id=env.post.id, start=utcnow(), timezone='UTC'))
        db.session.commit()

        view = self._view(env)

        for field in ['end', 'online_link', 'external_participation_url',
                      'buy_tickets_link', 'event_fee_currency', 'event_fee_amount',
                      'location']:
            assert field not in view, field

    def test_a_zero_fee_is_omitted_which_is_what_the_truthiness_test_means(self, env):
        """`if event.event_fee_amount:` not `is not None`, so a fee of 0 is indistinguishable
        from no fee at all in the API. Recorded rather than changed: 0 and absent both mean
        'free' to a client, and changing it is a schema decision."""
        env.post.type = POST_TYPE_EVENT
        db.session.add(Event(post_id=env.post.id, start=utcnow(), timezone='UTC',
                             event_fee_amount=0, event_fee_currency='GBP'))
        db.session.commit()

        view = self._view(env)

        assert 'event_fee_amount' not in view
        assert view['event_fee_currency'] == 'GBP'


# --------------------------------------------------------------------------
# registration_view: a registration's five optional fields
# --------------------------------------------------------------------------


class TestARegistrationView:
    """The admin's registration queue. Five fields are added one `if` at a time, and one of
    them -- `throwaway_email` -- depends on a config flag AND a file on disk, which is why
    it and the four beside it were never reached.
    """

    @pytest.fixture
    def applicant(self, env):
        user = make_user(None, 'applicant', local=True)
        user.email = 'someone@throwaway.example'
        user.ip_address = '203.0.113.4'
        user.ip_address_country = 'GB'
        user.referrer = 'https://somewhere.example/invite'
        db.session.commit()
        return user

    def _view(self, registration):
        from app.api.alpha.views import registration_view

        return registration_view(registration)

    def test_the_optional_fields_are_served_when_they_are_set(self, env, applicant):
        registration = UserRegistration(user_id=applicant.id, answer='because',
                                        status=0)
        db.session.add(registration)
        db.session.commit()

        view = self._view(registration)

        assert view['country_code'] == 'GB'
        assert view['referrer'] == 'https://somewhere.example/invite'
        assert view['ip_address'] == '203.0.113.4'
        assert view['status'] == 'awaiting review'

    def test_an_approved_registration_carries_who_approved_it_and_when(self, env,
                                                                      applicant):
        """`:1511` and `:1513`, inside the `status == 1` arm -- and both are themselves
        optional, so an approved row with neither field set takes the arm and skips them."""
        approver = make_user(None, 'approver', local=True)
        db.session.commit()
        registration = UserRegistration(user_id=applicant.id, answer='because',
                                        status=1, approved_by=approver.id,
                                        approved_at=utcnow())
        db.session.add(registration)
        db.session.commit()

        view = self._view(registration)

        assert view['status'] == 'approved'
        # `user_view(..., variant=1)` is a flat person dict, not the {'person': ...}
        # envelope the list views wrap it in.
        assert view['approved_by']['id'] == approver.id
        assert view['approved_at'].endswith('Z')

    def test_an_approved_registration_with_neither_omits_both(self, env, applicant):
        registration = UserRegistration(user_id=applicant.id, answer='because', status=1)
        db.session.add(registration)
        db.session.commit()

        view = self._view(registration)

        assert view['status'] == 'approved'
        assert 'approved_by' not in view
        assert 'approved_at' not in view

    def test_a_throwaway_email_is_flagged_when_the_feature_is_on(self, env, applicant,
                                                                monkeypatch, tmp_path):
        """`:1503`, and the reason it was unreachable: the flag is off by default AND the
        domain list is a file this repository does not ship, so the `disposable_domains`
        list is empty and no address can match it.

        The file is written into the path the view reads, `app/static/tmp/`, and removed
        afterwards -- the view opens a hardcoded relative path, so there is nowhere else to
        put it.
        """
        monkeypatch.setitem(env.app.config, 'FLAG_THROWAWAY_EMAILS', True)
        listing = 'app/static/tmp/disposable_domains.txt'
        os.makedirs('app/static/tmp', exist_ok=True)
        existed = os.path.isfile(listing)
        if not existed:
            with open(listing, 'w', encoding='utf-8') as handle:
                handle.write('throwaway.example\n')
        try:
            registration = UserRegistration(user_id=applicant.id, answer='because',
                                            status=0)
            db.session.add(registration)
            db.session.commit()

            view = self._view(registration)

            assert view['throwaway_email'] is True
        finally:
            if not existed:
                os.remove(listing)

    def test_an_ordinary_email_is_not_flagged(self, env, applicant, monkeypatch):
        """The control. With the flag off the list is empty, so nothing is ever flagged --
        which is the state every instance is in until an admin turns it on and supplies the
        file."""
        monkeypatch.setitem(env.app.config, 'FLAG_THROWAWAY_EMAILS', False)
        registration = UserRegistration(user_id=applicant.id, answer='because', status=0)
        db.session.add(registration)
        db.session.commit()

        view = self._view(registration)

        assert 'throwaway_email' not in view
