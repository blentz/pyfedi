"""The name a peer publishes for an actor: `actor_name_from_ap`, and the five
places that used to read `preferredUsername` by hand.

D1372. `activity_json['preferredUsername'].strip()` appeared at five sites --
`refresh_user_profile_task`, and the Person/Service, Group and Feed branches of
`actor_json_to_model`, which also wrote `ap_preferred_username` and
`machine_name` from the same key unstripped. One untrusted value failed three
different ways:

    key absent                KeyError
    a number, list, dict,     AttributeError: 'int' object has no attribute
    bool or null              'strip'
    longer than the column    DataError at commit

and the only guard, `except KeyError` around the constructors, catches exactly
one of the three.

Measured through `refresh_user_profile_task`, which has no such handler and
re-raises after rolling back -- so every one of these makes an actor
permanently unrefreshable, and with it their avatar, bio, `indexable` flag and
any rotated key:

    preferredUsername absent  KeyError: 'preferredUsername'
    None                      AttributeError: 'NoneType' object has no ...
    5                         AttributeError: 'int' object has no ...
    []                        AttributeError: 'list' object has no ...
    {}                        AttributeError: 'dict' object has no ...
    True                      AttributeError: 'bool' object has no ...
    ['wakko']                 AttributeError: 'list' object has no ...

This is D1354 and D1355's family -- a peer's document read by hand -- for the
one key those two rounds did not reach. `_as_text`, `public_key_pem`,
`language_from_ap`, `parse_ap_timestamp`, `property_value_fields` and
`markdown_source` all came out of the same shape, and `actor_name_from_ap` joins
them.

TWO NEIGHBOURS FIXED WITH IT, both inside the same constructors:

* `publicKey` was still read by hand at all three creation branches --
  `public_key_pem` was written for D1354 and applied only to the refresh tasks.
  A `publicKey` that is a string raised TypeError past the `except KeyError`,
  and `{'publicKeyPem': None}` stored the STRING 'None', which no signature can
  ever verify against;
* the Feed constructor assigned the peer's `summary` straight into
  `description_html`, which the block below overwrites through `allowlist_html`
  -- but only when `_as_text` accepts it. For a non-string the block is skipped
  and the raw value stayed in a Text column.

WHY REFUSING IS RIGHT AT CREATION AND WRONG AT REFRESH. A row being created has
no name yet, and `name`/`machine_name`/`user_name` are what every lookup and
every `/u/`, `/c/` and `/f/` route resolves on, so an actor that publishes no
usable name is one this instance cannot represent -- which is the outcome
`except KeyError` already chose for one of the three shapes. A row being
refreshed already holds a name that worked, and a peer that stops publishing a
usable one has not renamed itself to nothing; `public_key_pem`'s reasoning under
D1354, applied to the name.
"""
import pytest

from app import db
from app.activitypub.util import actor_json_to_model, refresh_user_profile_task
from app.models import Community, Feed, User, actor_name_from_ap
from tests.factories import peer_actor_json, peer_instance
from tests.test_ap_refresh_profiles import (PEER, _person_document,
                                            _remote_user, _serve)


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


class TestActorNameFromAp:
    @pytest.mark.parametrize('document', [
        {},                                  # the key absent
        {'preferredUsername': None},
        {'preferredUsername': ''},
        {'preferredUsername': '   '},        # a string, but nothing after strip
        {'preferredUsername': 5},
        {'preferredUsername': 0},
        {'preferredUsername': True},
        {'preferredUsername': []},
        {'preferredUsername': {}},
        {'preferredUsername': ['alice']},
        {'preferredUsername': {'value': 'alice'}},
    ])
    def test_what_names_no_actor(self, document):
        assert actor_name_from_ap(document) is None

    def test_a_plain_name(self):
        assert actor_name_from_ap({'preferredUsername': 'alice'}) == 'alice'

    def test_padding_is_stripped(self):
        assert actor_name_from_ap({'preferredUsername': ' \t alice \n '}) == 'alice'

    def test_another_key(self):
        """`name` is read through the same helper, with the wider limit of the
        title columns."""
        assert actor_name_from_ap({'name': 'Alice A'}, 'name') == 'Alice A'
        assert actor_name_from_ap({'preferredUsername': 'alice'}, 'name') is None

    def test_a_name_wider_than_the_column_is_cut_to_it(self):
        """`user_name` and `ap_preferred_username` are String(255) and the raw
        value went in unbounded, which is a DataError at commit -- and a commit
        that raises loses whatever else was in the session, not just the name."""
        assert actor_name_from_ap({'preferredUsername': 'a' * 300}) == 'a' * 255
        assert actor_name_from_ap({'name': 'a' * 300}, 'name', limit=256) == 'a' * 256

    def test_it_is_stripped_before_it_is_cut(self):
        """Otherwise the padding spends part of the column's width and the value
        stored still has to be stripped by whoever reads it."""
        assert actor_name_from_ap({'preferredUsername': '  ' + 'a' * 255 + '  '}) \
            == 'a' * 255

    @pytest.mark.parametrize('document', [None, 'alice', 5, [], ()])
    def test_a_document_that_is_not_a_mapping(self, document):
        """`actor_json_to_model` is reached with whatever `.json()` returned, and
        a peer may answer with a list or a bare string."""
        assert actor_name_from_ap(document) is None


# --------------------------------------------------------------------------
# refresh_user_profile_task -- the site with no handler at all
# --------------------------------------------------------------------------


def _refresh_with(http_mock, value, key='preferredUsername'):
    """Serve a Person document whose `key` is `value`, or absent for `'absent'`,
    and refresh. Returns the user id."""
    user = _remote_user()
    uid = user.id
    document = _person_document()
    if value == 'absent':
        document.pop(key, None)
    else:
        document[key] = value
    _serve(http_mock, f'https://{PEER}/u/wakko', document)
    refresh_user_profile_task(uid)
    db.session.expire_all()
    return uid


class TestRefreshingAgainstAnUnusableName:
    """Each of these used to raise out of the task. The task rolls back and
    re-raises, so the actor stayed stale for ever."""

    @pytest.mark.parametrize('value', ['absent', None, 5, [], {}, True,
                                       ['wakko'], '', '   '])
    def test_the_refresh_completes(self, app, db_session, http_mock, value):
        uid = _refresh_with(http_mock, value)

        assert db.session.get(User, uid) is not None

    @pytest.mark.parametrize('value', ['absent', None, 5, [], {}, True,
                                       ['wakko'], '', '   '])
    def test_the_name_this_instance_holds_is_kept(self, app, db_session,
                                                  http_mock, value):
        """Not blanked, and not replaced with a stringified `None` or `5`. The
        name is what `/u/<name>` resolves on, so writing an unusable one would
        make a working actor unreachable."""
        uid = _refresh_with(http_mock, value)

        assert db.session.get(User, uid).user_name == 'wakko'

    @pytest.mark.parametrize('value', ['absent', None, 5, [], {}, True])
    def test_the_rest_of_the_document_is_still_applied(self, app, db_session,
                                                       http_mock, value):
        """The point of the fix, rather than its mechanism: the task raised at
        the name and never reached the key, the avatar or `indexable`. A peer
        that publishes an unusable name must not be able to freeze everything
        else about the actor."""
        user = _remote_user()
        uid = user.id
        document = _person_document(fields={'indexable': False,
                                            'name': 'Wakko W'})
        if value == 'absent':
            del document['preferredUsername']
        else:
            document['preferredUsername'] = value
        _serve(http_mock, f'https://{PEER}/u/wakko', document)

        refresh_user_profile_task(uid)
        db.session.expire_all()

        refreshed = db.session.get(User, uid)
        assert refreshed.indexable is False
        assert refreshed.title == 'Wakko W'
        assert refreshed.public_key == '-----BEGIN PUBLIC KEY-----refreshed'


class TestRefreshingWithAUsableName:
    def test_a_renamed_actor_is_renamed(self, app, db_session, http_mock):
        """The write still happens -- a fix that only ever kept the old name
        would pass every test above."""
        uid = _refresh_with(http_mock, 'wakko_renamed')

        assert db.session.get(User, uid).user_name == 'wakko_renamed'

    def test_a_padded_name_is_stripped(self, app, db_session, http_mock):
        uid = _refresh_with(http_mock, '  wakko_renamed  ')

        assert db.session.get(User, uid).user_name == 'wakko_renamed'

    def test_a_name_wider_than_the_column_is_cut(self, app, db_session, http_mock):
        """String(255). Unbounded, this was a DataError at the task's commit --
        which rolls back the whole refresh, not just the name."""
        uid = _refresh_with(http_mock, 'w' * 300)

        assert db.session.get(User, uid).user_name == 'w' * 255


class TestTheTitleReadBesideIt:
    """`if 'name' in activity_json: user.title = activity_json['name'].strip()
    if activity_json['name'] else ''` -- presence and falsiness were guarded,
    the type was not."""

    @pytest.mark.parametrize('value', [5, [], {}, True, ['Wakko'], '   '])
    def test_an_unusable_title_becomes_empty(self, app, db_session, http_mock,
                                             value):
        uid = _refresh_with(http_mock, value, key='name')

        assert db.session.get(User, uid).title == ''

    def test_an_absent_name_leaves_the_title_alone(self, app, db_session,
                                                   http_mock):
        """The `in` guard, which was already there and must stay: a document
        with no `name` is not a request to clear the display name."""
        user = _remote_user()
        uid = user.id
        user.title = 'Held Title'
        db.session.commit()
        document = _person_document()
        assert 'name' not in document
        _serve(http_mock, f'https://{PEER}/u/wakko', document)

        refresh_user_profile_task(uid)
        db.session.expire_all()

        assert db.session.get(User, uid).title == 'Held Title'

    def test_a_usable_title_is_applied(self, app, db_session, http_mock):
        uid = _refresh_with(http_mock, '  Wakko W  ', key='name')

        assert db.session.get(User, uid).title == 'Wakko W'


# --------------------------------------------------------------------------
# actor_json_to_model -- the three creation branches
# --------------------------------------------------------------------------

UNUSABLE = [None, 5, [], {}, True, ['alice'], '', '   ']


class TestCreatingAPerson:
    @pytest.mark.parametrize('value', UNUSABLE)
    def test_an_unusable_name_is_refused_cleanly(self, app, db_session, value):
        """`except KeyError` gave the absent case exactly this outcome. The
        other shapes raised past it -- out of `actor_json_to_model`, out of
        `find_actor_or_create`, and into whatever was processing the activity
        that named the actor."""
        peer_instance(PEER)
        document = peer_actor_json('Person', name='alice', server=PEER,
                                   fields={'preferredUsername': value})

        assert actor_json_to_model(document, 'alice', PEER) is None
        assert db.session.query(User).count() == 0

    def test_a_name_wider_than_the_column_is_cut(self, app, db_session):
        """`user_name` and `ap_preferred_username` are both String(255), and the
        row is committed by the caller -- so an over-wide value was a DataError
        somewhere else entirely."""
        peer_instance(PEER)
        document = peer_actor_json('Person', name='alice', server=PEER,
                                   fields={'preferredUsername': 'a' * 300})

        user = actor_json_to_model(document, 'alice', PEER)

        assert user.user_name == 'a' * 255
        assert user.ap_preferred_username == 'a' * 255

    @pytest.mark.parametrize('value', [5, [], {}, True, '   '])
    def test_an_unusable_display_name_leaves_no_title(self, app, db_session, value):
        """`title=... if 'name' in activity_json and activity_json['name'] else
        None` -- the falsy shapes were handled, the truthy non-strings were not."""
        peer_instance(PEER)
        document = peer_actor_json('Person', name='alice', server=PEER,
                                   fields={'name': value})

        assert actor_json_to_model(document, 'alice', PEER).title is None


class TestCreatingAGroup:
    @pytest.mark.parametrize('value', UNUSABLE)
    def test_an_unusable_name_is_refused_cleanly(self, app, db_session, value):
        peer_instance(PEER)
        document = peer_actor_json('Group', name='memes', server=PEER,
                                   fields={'preferredUsername': value})

        assert actor_json_to_model(document, '!memes', PEER) is None
        assert db.session.query(Community).count() == 0

    def test_low_quality_is_decided_from_the_validated_name(self, app, db_session,
                                                            monkeypatch):
        """`'memes' in activity_json['preferredUsername']` sat OUTSIDE every
        `try` in this function, so a non-string there was an uncaught TypeError
        rather than the refusal the constructor's handler would have given."""
        from app import cache
        peer_instance(PEER)
        monkeypatch.setattr('app.activitypub.util.get_setting',
                            lambda key, default=None:
                            True if key == 'meme_comms_low_quality' else default)
        document = peer_actor_json('Group', name='  dankmemes  ', server=PEER)

        community = actor_json_to_model(document, '!dankmemes', PEER)

        assert community.name == 'dankmemes'
        assert community.low_quality is True
        cache.clear()

    def test_a_padded_name_and_title(self, app, db_session):
        peer_instance(PEER)
        document = peer_actor_json('Group', name='memes', server=PEER,
                                   fields={'preferredUsername': '  memes  ',
                                           'name': '  Memes  '})

        community = actor_json_to_model(document, '!memes', PEER)

        assert community.name == 'memes'
        assert community.title == 'Memes'


class TestThePublicKeyReadInTheSameConstructors:
    """`public_key_pem` was written for D1354 and applied to the refresh tasks
    only. These three shapes reached the creation branches unguarded."""

    @pytest.mark.parametrize('value', ['not an object', 5, [], None,
                                       {'publicKeyPem': None},
                                       {'publicKeyPem': ''},
                                       {'publicKeyPem': 5},
                                       {'id': 'x'}])
    def test_a_person_with_no_usable_key_is_refused(self, app, db_session, value):
        peer_instance(PEER)
        document = peer_actor_json('Person', name='alice', server=PEER,
                                   fields={'publicKey': value})

        assert actor_json_to_model(document, 'alice', PEER) is None
        assert db.session.query(User).count() == 0

    def test_the_string_none_is_never_stored_as_a_key(self, app, db_session):
        """D1354's worst shape: `{'publicKeyPem': None}` used to store the
        four-character string 'None', against which no signature this actor ever
        sends can verify -- an actor that looks present and whose every activity
        is rejected."""
        peer_instance(PEER)
        document = peer_actor_json('Person', name='alice', server=PEER,
                                   fields={'publicKey': {'publicKeyPem': None}})

        assert actor_json_to_model(document, 'alice', PEER) is None
        assert not db.session.query(User).filter(
            User.public_key == 'None').count()

    def test_a_usable_key_is_stored(self, app, db_session):
        """The other direction: a fix that refused everything would pass every
        row above."""
        peer_instance(PEER)
        document = peer_actor_json('Person', name='alice', server=PEER)

        user = actor_json_to_model(document, 'alice', PEER)

        assert user.public_key.startswith('-----BEGIN PUBLIC KEY-----')
