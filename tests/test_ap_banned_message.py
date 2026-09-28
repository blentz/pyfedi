"""`process_banned_message`: what a peer tells us when it refuses our delivery.

`app/activitypub/signature.py` calls this when an activity we sent comes back
`400` with `person_is_banned_from_site` in the body. Both the status and the body
are the peer's choice, so every value here is untrusted, and the function had no
tests at all -- measured over the whole suite, the only executed line in it was
its `def`.

D1379, D1372's family at a fifth site. `banned_json['message']` was read by hand:

    {}                       KeyError: 'message'
    {'msg': 'x'}             KeyError: 'message'
    {'message': None}        AttributeError: 'NoneType' object has no attribute
                             'strip'
    {'message': 5}           AttributeError: 'int' object has no attribute 'strip'
    {'message': []}          AttributeError: 'list' object has no attribute
                             'strip'

The caller catches both, into a logged failure -- but `result.close()` sits AFTER
this branch, so a raise here leaked the HTTP response, every time a peer chose to
answer that way.

The caller had its own half of the same shape: the branch fires on a SUBSTRING of
`result.text`, so the body need not be JSON, and `result.json()` was called on it
unguarded. An HTML error page carrying the phrase was enough.

WHAT THIS DOES *NOT* FIND. The insert names `(banned_person, that peer)`, and the
peer is the host we chose to deliver to, so a peer can only record a ban about
itself -- which is its own business. There is no cross-instance escalation here,
and the round looked for one before settling on the input handling.
"""
import pytest
from flask import g
from unittest.mock import patch

from app import db
from app.activitypub.util import process_banned_message
from app.models import Instance, Site
from tests.factories import make_instance, make_user

PEER = 'peer.banned.test'


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    db.session.commit()
    instance = make_instance(PEER)
    user = make_user(instance, 'alice')
    user.ap_profile_id = f'https://{PEER}/u/alice'
    user.ap_public_url = f'https://{PEER}/u/alice'
    db.session.commit()
    return instance, user


def bans():
    return db.session.execute(
        db.text('SELECT count(*) FROM instance_ban')).scalar()


def ban_rows():
    return db.session.execute(db.text(
        'SELECT user_id, instance_id, banned_until FROM instance_ban')).all()


UNUSABLE = [{}, {'msg': 'x'}, {'message': None}, {'message': 5},
            {'message': []}, {'message': {}}, {'message': True},
            {'message': ''}, {'message': '   '}]


class TestABodyThatNamesNobody:
    @pytest.mark.parametrize('body', UNUSABLE)
    def test_it_does_not_raise(self, app, env, body):
        """Each of these raised out of the function and past `result.close()`."""
        process_banned_message(body, PEER, db.session)

    @pytest.mark.parametrize('body', UNUSABLE)
    def test_it_bans_nobody(self, app, env, body):
        process_banned_message(body, PEER, db.session)

        assert bans() == 0

    @pytest.mark.parametrize('body', [None, [], 'a string', 5])
    def test_a_body_that_is_not_an_object(self, app, env, body):
        """`result.json()` returns whatever parses -- a list, a bare string, a
        number -- and `.get` on any of them is an AttributeError."""
        process_banned_message(body, PEER, db.session)

        assert bans() == 0

    def test_an_unresolvable_actor_bans_nobody(self, app, env):
        """`create_if_not_found=False`: a peer naming an actor this instance has
        never seen is told nothing, and must not create the row as a side
        effect of being named."""
        process_banned_message({'message': f'https://{PEER}/u/nobody'}, PEER,
                               db.session)

        assert bans() == 0


class TestABodyThatNamesSomeone:
    def test_the_ban_is_recorded(self, app, env):
        instance, user = env

        process_banned_message({'message': f'https://{PEER}/u/alice'}, PEER,
                               db.session)

        rows = ban_rows()
        assert len(rows) == 1
        assert rows[0][0] == user.id and rows[0][1] == instance.id

    def test_the_ban_lasts_a_day(self, app, env):
        """`utcnow() + timedelta(days=1)`. A peer's refusal is not permanent --
        it is re-checked tomorrow -- and a ban with no expiry would make one
        400 response final."""
        from app.models import utcnow

        before = utcnow()
        process_banned_message({'message': f'https://{PEER}/u/alice'}, PEER,
                               db.session)

        banned_until = ban_rows()[0][2]
        assert 0 < (banned_until - before).total_seconds() <= 86400 + 60

    def test_a_padded_actor_id_still_resolves(self, app, env):
        """`find_actor_or_create` strips; the guard must not refuse a value it
        would have accepted."""
        process_banned_message({'message': f'  https://{PEER}/u/alice  '}, PEER,
                               db.session)

        assert bans() == 1

    def test_a_second_message_updates_rather_than_duplicates(self, app, env):
        """`ON CONFLICT (user_id, instance_id) DO UPDATE` -- a peer that refuses
        every delivery must not grow a row per attempt."""
        process_banned_message({'message': f'https://{PEER}/u/alice'}, PEER,
                               db.session)
        first = ban_rows()[0][2]
        process_banned_message({'message': f'https://{PEER}/u/alice'}, PEER,
                               db.session)

        rows = ban_rows()
        assert len(rows) == 1
        assert rows[0][2] >= first

    def test_a_domain_this_instance_does_not_know_bans_nobody(self, app, env):
        """`if instance:` -- the domain comes from the URI we delivered to, so
        an unknown one means the row cannot name an instance. Without the guard
        this is `AttributeError: 'NoneType' object has no attribute 'id'`."""
        process_banned_message({'message': f'https://{PEER}/u/alice'},
                               'nosuch.test', db.session)

        assert bans() == 0

    def test_the_domain_is_matched_case_insensitively(self, app, env):
        """`Instance.domain == instance_domain.lower()`. `furl(uri).host` can
        carry the case the peer published, and a ban recorded against no
        instance is a ban not recorded."""
        instance, user = env

        process_banned_message({'message': f'https://{PEER}/u/alice'},
                               PEER.upper(), db.session)

        assert bans() == 1


class TestTheCallerSide:
    """`post_request` in app/activitypub/signature.py, through the harness in
    tests/test_activitypub_signature.py whose `_Response` counts `close()` --
    "a leak there is invisible to every other assertion", and the leak is
    exactly what D1379's raise caused.
    """

    BANNED = 'person_is_banned_from_site'

    def _deliver_400(self, payload=None, raises=None, text=None):
        from tests.test_activitypub_signature import _Response, _deliver

        response = _Response(status_code=400,
                             text=text if text is not None else self.BANNED,
                             payload=payload)
        if raises is not None:
            def boom():
                raise raises
            response.json = boom
        _deliver(response, uri=f'https://{PEER}/inbox')
        return response

    def test_a_body_that_is_not_json_does_not_leak_the_response(self, app, env):
        """The measured half: the branch fires on a SUBSTRING of `result.text`,
        so an HTML error page carrying the phrase reached `.json()`. The raise
        was caught below -- but `result.close()` is after this branch, so the
        response was never closed."""
        import json

        response = self._deliver_400(raises=json.JSONDecodeError('no', '', 0),
                                     text=f'<html>{self.BANNED}</html>')

        assert response.closed is True
        assert bans() == 0

    def test_a_json_body_naming_nobody_does_not_leak_the_response(self, app, env):
        """The other measured half, in the function rather than the caller:
        `{}` was `KeyError: 'message'`, which reached the same handler and
        skipped the same `close()`."""
        response = self._deliver_400(payload={})

        assert response.closed is True
        assert bans() == 0

    @pytest.mark.parametrize('payload', [{'message': None}, {'message': 5},
                                         {'message': []}])
    def test_an_unusable_actor_does_not_leak_the_response(self, app, env,
                                                          payload):
        response = self._deliver_400(payload=payload)

        assert response.closed is True
        assert bans() == 0

    def test_a_usable_body_still_records_the_ban(self, app, env):
        """End to end, and the direction a fix that refused everything would
        fail: the peer's refusal is what this branch exists to act on."""
        instance, user = env

        response = self._deliver_400(
            payload={'message': f'https://{PEER}/u/alice'})

        assert response.closed is True
        rows = ban_rows()
        assert len(rows) == 1 and rows[0][0] == user.id

    def test_an_ordinary_400_never_reaches_the_branch(self, app, env):
        """`status_code == 400 and 'person_is_banned_from_site' in result.text`
        -- a 400 that says something else must not be read as a ban."""
        response = self._deliver_400(payload={'message': f'https://{PEER}/u/alice'},
                                     text='some other problem')

        assert response.closed is True
        assert bans() == 0
