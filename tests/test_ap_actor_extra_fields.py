"""An actor's `attachment` fields and its `publicKey`, on refresh and on create.

`property_value_fields` and `public_key_pem` in `app/models.py`, and their two
call sites: `refresh_user_profile_task` and `actor_json_to_model` in
`app/activitypub/util.py`.

D1354. Both copies of the attachment loop read `field_data['type']`, `['value']`
and `['name']` outright and then called `.strip()` on both, and the refresh task
read `activity_json['publicKey']['publicKeyPem']` the same way. Every shape below
raised out of the task -- and a refresh task that raises leaves the actor
UNREFRESHABLE, which is the consequence D1325 was about: their display name, their
bio, their deleted flag and their KEY all stop being picked up.

`{'publicKeyPem': None}` was worse than raising: it stored the string 'None' as
the key, and no signature can verify against that.
"""
import pytest

from app import db
from app.activitypub.util import (actor_json_to_model, refresh_user_profile_task)
from app.models import (User, UserExtraField, property_value_fields,
                        public_key_pem)
from tests.test_ap_refresh_profiles import (PEER, _person_document, _remote_user,
                                            _serve)

PEM = '-----BEGIN PUBLIC KEY-----refreshed'


class TestTheFieldsAPeerOffers:
    """The helper directly."""

    def test_a_proper_field(self):
        assert property_value_fields(
            [{'type': 'PropertyValue', 'name': 'Website',
              'value': 'https://me.test'}]) == [('Website', 'https://me.test')]

    def test_several_fields_keep_their_order(self):
        attachment = [{'type': 'PropertyValue', 'name': 'a', 'value': '1'},
                      {'type': 'PropertyValue', 'name': 'b', 'value': '2'}]

        assert property_value_fields(attachment) == [('a', '1'), ('b', '2')]

    @pytest.mark.parametrize('entry', [
        {'type': 'PropertyValue'},
        {'type': 'PropertyValue', 'value': 'x'},
        {'type': 'PropertyValue', 'name': 'n'},
        {'type': 'PropertyValue', 'name': 'n', 'value': 5},
        {'type': 'PropertyValue', 'name': 5, 'value': 'x'},
        {'type': 'PropertyValue', 'name': 'n', 'value': None},
        {'type': 'PropertyValue', 'name': None, 'value': 'x'},
        {'type': 'PropertyValue', 'name': ['n'], 'value': 'x'},
        {'value': 'x', 'name': 'n'},
        {'type': 'Image', 'name': 'n', 'value': 'x'},
        'a string',
        5,
        None,
        [],
    ])
    def test_an_entry_it_cannot_use_is_skipped(self, entry):
        assert property_value_fields([entry]) == []

    def test_a_usable_entry_beside_an_unusable_one_survives(self):
        """Skipping, not refusing: one bad field must not cost the others."""
        attachment = [{'type': 'PropertyValue'},
                      {'type': 'PropertyValue', 'name': 'ok', 'value': 'yes'},
                      5]

        assert property_value_fields(attachment) == [('ok', 'yes')]

    @pytest.mark.parametrize('attachment', ['a string', {}, 5, None])
    def test_an_attachment_that_is_not_a_list(self, attachment):
        assert property_value_fields(attachment) == []

    def test_both_sides_are_stripped(self):
        assert property_value_fields(
            [{'type': 'PropertyValue', 'name': '  a  ',
              'value': '  b  '}]) == [('a', 'b')]

    def test_both_sides_are_capped_at_the_column_width(self):
        """`UserExtraField.label` and `.text` are String(1024), and a longer value
        was `DataError: value too long for type character varying`, which took the
        whole refresh with it."""
        fields = property_value_fields(
            [{'type': 'PropertyValue', 'name': 'n' * 3000, 'value': 'v' * 3000}])

        assert [len(part) for part in fields[0]] == [1024, 1024]

    def test_a_mastodon_anchor_becomes_its_href(self):
        fields = property_value_fields(
            [{'type': 'PropertyValue', 'name': 'Site',
              'value': '<a href="https://me.test" rel="me">me.test</a>'}])

        assert fields == [('Site', 'https://me.test')]

    def test_a_value_mentioning_an_anchor_without_being_one(self):
        """`'<a ' in value` is a substring test, which is why the conversion has to
        be safe on a value that only looks like markup. `mastodon_extra_field_link`
        returns its input unchanged when there is no href."""
        fields = property_value_fields(
            [{'type': 'PropertyValue', 'name': 'Note', 'value': 'write <a > here'}])

        assert fields == [('Note', 'write <a > here')]


class TestThePublicKeyAPeerOffers:
    def test_a_proper_key(self):
        assert public_key_pem({'publicKey': {'publicKeyPem': PEM}}) == PEM

    @pytest.mark.parametrize('document', [
        {},
        {'publicKey': None},
        {'publicKey': 5},
        {'publicKey': 'a string'},
        {'publicKey': []},
        {'publicKey': {}},
        {'publicKey': {'publicKeyPem': None}},
        {'publicKey': {'publicKeyPem': 5}},
        {'publicKey': {'publicKeyPem': ''}},
        'a string',
        None,
    ])
    def test_what_offers_no_key(self, document):
        assert public_key_pem(document) is None


class TestRefreshingAnActorWithAwkwardFields:
    """Through the task, because the defect is that the task raises: the actor is
    left with the profile it had, for ever."""

    def refresh(self, http_mock, **fields):
        user = _remote_user()
        _serve(http_mock, user.ap_public_url, _person_document(fields=fields))
        refresh_user_profile_task(user.id)
        db.session.refresh(user)
        return user

    @pytest.mark.parametrize('attachment', [
        [{'type': 'PropertyValue'}],
        [{'type': 'PropertyValue', 'name': 'n'}],
        [{'type': 'PropertyValue', 'name': 'n', 'value': 5}],
        [{'type': 'PropertyValue', 'name': 5, 'value': 'x'}],
        [{'value': 'x', 'name': 'n'}],
        ['a string'],
        [5],
        [None],
        'not a list',
        5,
    ])
    def test_the_refresh_still_happens(self, app, db_session, http_mock, attachment):
        user = self.refresh(http_mock, attachment=attachment, name='Refreshed')

        assert user.title == 'Refreshed'
        assert user.ap_fetched_at is not None
        assert list(user.extra_fields) == []

    def test_a_usable_field_is_stored(self, app, db_session, http_mock):
        user = self.refresh(http_mock, attachment=[
            {'type': 'PropertyValue', 'name': 'Website', 'value': 'https://me.test'}])

        assert [(f.label, f.text) for f in user.extra_fields] == \
            [('Website', 'https://me.test')]

    def test_an_over_long_field_no_longer_loses_the_refresh(self, app, db_session,
                                                            http_mock):
        """This was a DataError at the commit, so the refresh was lost at the very
        end -- after the document had been read and applied in memory."""
        user = self.refresh(http_mock, name='Refreshed', attachment=[
            {'type': 'PropertyValue', 'name': 'n' * 3000, 'value': 'v' * 3000}])

        assert user.title == 'Refreshed'
        assert len(user.extra_fields[0].label) == 1024

    def test_the_fields_are_replaced_not_appended(self, app, db_session, http_mock):
        """`user.extra_fields = [...]`: an actor's second refresh must not
        accumulate. Asserted because the list comprehension replaced a loop that
        cleared the list first, and losing that clear would be invisible on a
        first refresh."""
        user = _remote_user()
        db.session.add(UserExtraField(user_id=user.id, label='old', text='gone'))
        db.session.commit()
        _serve(http_mock, user.ap_public_url, _person_document(fields={'attachment': [
            {'type': 'PropertyValue', 'name': 'new', 'value': 'here'}]}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert [(f.label, f.text) for f in user.extra_fields] == [('new', 'here')]

    def test_an_attachment_of_no_usable_fields_clears_the_old_ones(self, app,
                                                                   db_session,
                                                                   http_mock):
        """The peer sent an `attachment`, so it is describing its whole field list;
        that it contains nothing this instance can read still means "no fields"."""
        user = _remote_user()
        db.session.add(UserExtraField(user_id=user.id, label='old', text='gone'))
        db.session.commit()
        _serve(http_mock, user.ap_public_url,
               _person_document(fields={'attachment': [5]}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert list(user.extra_fields) == []


class TestRefreshingAnActorWithNoUsableKey:
    def refreshed_key(self, http_mock, public_key=...):
        user = _remote_user()
        user.public_key = 'the key we already hold'
        db.session.commit()
        document = _person_document(name=user.user_name)
        if public_key is ...:
            del document['publicKey']
        else:
            document['publicKey'] = public_key
        _serve(http_mock, user.ap_public_url, document)
        refresh_user_profile_task(user.id)
        db.session.refresh(user)
        return user

    @pytest.mark.parametrize('public_key', [..., None, 5, 'a string', {},
                                            {'publicKeyPem': None},
                                            {'publicKeyPem': ''}])
    def test_the_key_this_instance_holds_is_kept(self, app, db_session, http_mock,
                                                 public_key):
        """An actor that stops publishing a key has not rotated to nothing, and the
        string 'None' -- which `{'publicKeyPem': None}` used to store -- breaks
        every signature check against them."""
        user = self.refreshed_key(http_mock, public_key)

        assert user.public_key == 'the key we already hold'

    def test_the_rest_of_the_refresh_still_applies(self, app, db_session, http_mock):
        user = _remote_user()
        document = _person_document(name=user.user_name, fields={'name': 'Refreshed'})
        del document['publicKey']
        _serve(http_mock, user.ap_public_url, document)

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.title == 'Refreshed'

    def test_a_real_rotation_is_applied(self, app, db_session, http_mock):
        user = self.refreshed_key(http_mock, {'publicKeyPem': PEM})

        assert user.public_key == PEM


class TestCreatingAnActorWithAwkwardFields:
    """`actor_json_to_model`, the second copy. It shortens the LABEL for display,
    which the refresh task does not; that difference is deliberate and asserted so
    the shared helper is known not to have flattened it."""

    def person(self, attachment):
        from tests.factories import peer_actor_json

        return peer_actor_json('Person', name='fieldy', server=PEER,
                               fields={'inbox': f'https://{PEER}/u/fieldy/inbox',
                                       'attachment': attachment})

    @pytest.fixture
    def peer(self, app, db_session, http_mock):
        from tests.factories import peer_instance

        peer_instance(PEER)

    @pytest.mark.parametrize('attachment', [
        [{'type': 'PropertyValue'}],
        [{'type': 'PropertyValue', 'name': 'n', 'value': 5}],
        ['a string'],
        [5],
        'not a list',
    ])
    def test_the_actor_is_still_created(self, peer, attachment):
        user = actor_json_to_model(self.person(attachment), 'fieldy', PEER)

        assert isinstance(user, User)
        assert list(user.extra_fields) == []

    def test_a_usable_field_is_stored_with_a_shortened_label(self, peer):
        user = actor_json_to_model(self.person([
            {'type': 'PropertyValue', 'name': 'L' * 80,
             'value': 'https://me.test'}]), 'fieldy', PEER)

        # 48, not 50: `shorten_string(s, 50)` returns `s[:47] + '…'`, reserving
        # three characters for an ellipsis and then appending a one-character one.
        # Measured rather than assumed, and left as it is -- it is cosmetic.
        label = user.extra_fields[0].label
        assert len(label) == 48 and label.endswith('…')
        assert user.extra_fields[0].text == 'https://me.test'
