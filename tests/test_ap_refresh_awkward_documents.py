"""A community's and a feed's refresh, against the shapes a peer may really send.

D1355, which is D1354 for the other two actor types plus two shapes D1354 did not
cover. `refresh_community_profile_task` and `refresh_feed_profile_task` read

  * `activity_json['publicKey']['publicKeyPem']`,
  * each entry of `activity_json['language']` as `['identifier']` and `['name']`,
  * `activity_json['summary']` (or `['content']`) and then `.startswith('<')`,

all outright. Measured through `refresh_community_profile_task`:

    language=['en']                       TypeError: string indices must be
                                          integers -- and a list of plain codes
                                          is a shape peers send
    language=[{'identifier': 'en'}]       KeyError: 'name'
    language=[{'name': 'English'}]        KeyError: 'identifier'
    language=[{'identifier': 5, ...}]     ProgrammingError: operator does not
                                          exist: character varying = integer
    language=[None] / [5]                 TypeError: not subscriptable
    a 50-character identifier             DataError: value too long
    publicKey absent / None / {}          KeyError / TypeError / KeyError
    publicKey={'publicKeyPem': None}      stored the STRING 'None'
    summary=5 / [] / {} / True            AttributeError: ... has no attribute
                                          'startswith'

A refresh task that raises leaves the community stale for ever: its name, icon,
description, moderator list AND key all stop being picked up.
"""
import pytest

from app import db
from app.activitypub.util import (refresh_community_profile_task,
                                  refresh_feed_profile_task)
from app.models import Community, Feed, language_from_ap
from tests.test_ap_refresh_profiles import (PEER, _feed_document, _group_document,
                                            _remote_community, _remote_feed, _serve)

PEM = '-----BEGIN PUBLIC KEY-----refreshed'


class TestTheLanguageAPeerNames:
    def test_a_proper_entry(self):
        assert language_from_ap({'identifier': 'en', 'name': 'English'}) == \
            ('en', 'English')

    @pytest.mark.parametrize('value', [
        {},
        {'name': 'English'},
        {'identifier': ''},
        {'identifier': 5, 'name': 'English'},
        {'identifier': None, 'name': 'English'},
        {'identifier': ['en'], 'name': 'English'},
        'en',
        5,
        None,
        [],
    ])
    def test_what_names_no_language(self, value):
        assert language_from_ap(value) is None

    def test_a_missing_name_falls_back_to_the_code(self):
        """A language this instance can key on is worth keeping unnamed; without an
        identifier there is nothing to key on at all."""
        assert language_from_ap({'identifier': 'en'}) == ('en', 'en')

    def test_a_name_that_is_not_a_string_falls_back_too(self):
        assert language_from_ap({'identifier': 'en', 'name': 5}) == ('en', 'en')

    def test_the_code_is_capped_at_the_column(self):
        """`Language.code` is String(5), and a longer identifier was a DataError at
        commit that took the whole refresh with it."""
        code, name = language_from_ap({'identifier': 'e' * 50, 'name': 'x'})

        assert len(code) == 5

    def test_the_name_is_capped_at_the_column(self):
        code, name = language_from_ap({'identifier': 'en', 'name': 'n' * 200})

        assert len(name) == 50


class TestRefreshingACommunityWithAwkwardFields:
    def refresh(self, http_mock, **fields):
        community = _remote_community()
        _serve(http_mock, community.ap_public_url, _group_document(fields=fields))
        refresh_community_profile_task(community.id, None)
        db.session.refresh(community)
        return community

    @pytest.mark.parametrize('language', [
        ['en'],
        [{'identifier': 'en'}],
        [{'name': 'English'}],
        [{'identifier': 5, 'name': 'English'}],
        [{'identifier': 'e' * 50, 'name': 'n' * 100}],
        [None],
        [5],
        [{}],
    ])
    def test_the_refresh_still_happens(self, app, db_session, http_mock, language):
        community = self.refresh(http_mock, language=language, name='Refreshed')

        assert community.title == 'Refreshed'
        assert community.ap_fetched_at is not None

    def test_a_usable_entry_beside_an_unusable_one_is_kept(self, app, db_session,
                                                           http_mock):
        community = self.refresh(http_mock, language=[
            5, {'identifier': 'de', 'name': 'Deutsch'}, {'name': 'nope'}])

        assert 'de' in [language.code for language in community.languages]

    def test_an_identifier_longer_than_the_column_is_truncated(self, app, db_session,
                                                              http_mock):
        community = self.refresh(http_mock, language=[
            {'identifier': 'abcdefgh', 'name': 'x'}])

        assert 'abcde' in [language.code for language in community.languages]

    @pytest.mark.parametrize('summary', [5, [], {}, True, {'content': 'x'}])
    def test_a_summary_that_is_not_a_string(self, app, db_session, http_mock,
                                            summary):
        community = self.refresh(http_mock, summary=summary, name='Refreshed')

        assert community.title == 'Refreshed'

    def test_an_unusable_summary_falls_back_to_content(self, app, db_session,
                                                       http_mock):
        """`summary` shadowed `content` by being PRESENT, whatever it held. The
        first readable of the two is used now."""
        community = self.refresh(http_mock, summary=5, content='<p>from content</p>')

        assert community.description_html == '<p>from content</p>'

    def test_a_readable_summary_still_wins(self, app, db_session, http_mock):
        community = self.refresh(http_mock, summary='<p>from summary</p>',
                                 content='<p>from content</p>')

        assert community.description_html == '<p>from summary</p>'

    def test_a_peertube_summary_is_wrapped(self, app, db_session, http_mock):
        community = self.refresh(http_mock, summary='bare text')

        assert community.description_html == '<p>bare text</p>'

    @pytest.mark.parametrize('public_key', [..., None, 5, 'a string', {},
                                            {'publicKeyPem': None},
                                            {'publicKeyPem': ''}])
    def test_the_key_this_instance_holds_is_kept(self, app, db_session, http_mock,
                                                 public_key):
        community = _remote_community()
        community.public_key = 'the key we already hold'
        db.session.commit()
        document = _group_document()
        if public_key is ...:
            del document['publicKey']
        else:
            document['publicKey'] = public_key
        _serve(http_mock, community.ap_public_url, document)

        refresh_community_profile_task(community.id, None)

        db.session.refresh(community)
        assert community.public_key == 'the key we already hold'

    def test_a_real_rotation_is_applied(self, app, db_session, http_mock):
        community = _remote_community()
        community.public_key = 'old'
        db.session.commit()
        document = _group_document()
        document['publicKey'] = {'publicKeyPem': PEM}
        _serve(http_mock, community.ap_public_url, document)

        refresh_community_profile_task(community.id, None)

        db.session.refresh(community)
        assert community.public_key == PEM


class TestRefreshingAFeedWithAwkwardFields:
    """The feed task has the same three reads. No `following` route is registered:
    the baseline document carries no `following` key, so the task never fetches
    one, and http_mock rejects a route that goes unused."""

    def refresh(self, http_mock, **fields):
        feed = _remote_feed()
        _serve(http_mock, feed.ap_public_url, _feed_document(fields=fields))
        refresh_feed_profile_task(feed.id)
        db.session.refresh(feed)
        return feed

    @pytest.mark.parametrize('summary', [5, [], {}, True])
    def test_a_summary_that_is_not_a_string(self, app, db_session, http_mock,
                                            summary):
        feed = self.refresh(http_mock, summary=summary, name='Refreshed')

        assert feed.title == 'Refreshed'

    def test_an_unusable_summary_falls_back_to_content(self, app, db_session,
                                                       http_mock):
        feed = self.refresh(http_mock, summary=[], content='<p>from content</p>')

        assert feed.description_html == '<p>from content</p>'

    @pytest.mark.parametrize('public_key', [..., None, {}, {'publicKeyPem': None}])
    def test_the_key_this_instance_holds_is_kept(self, app, db_session, http_mock,
                                                 public_key):
        feed = _remote_feed()
        feed.public_key = 'the key we already hold'
        db.session.commit()
        document = _feed_document()
        if public_key is ...:
            del document['publicKey']
        else:
            document['publicKey'] = public_key
        _serve(http_mock, feed.ap_public_url, document)

        refresh_feed_profile_task(feed.id)

        db.session.refresh(feed)
        assert feed.public_key == 'the key we already hold'


class TestAnActorsSummaryOnRefresh:
    """The user task's own copy of the summary read. D1354 covered its
    `attachment` and its `publicKey`; this is the third read in the same task."""

    @pytest.mark.parametrize('summary', [5, [], {}, True, {'content': 'x'}])
    def test_a_summary_that_is_not_a_string(self, app, db_session, http_mock,
                                            summary):
        from tests.test_ap_refresh_profiles import (_person_document, _remote_user)
        from app.activitypub.util import refresh_user_profile_task

        user = _remote_user()
        _serve(http_mock, user.ap_public_url,
               _person_document(fields={'summary': summary, 'name': 'Refreshed'}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.title == 'Refreshed'
        assert user.about_html == ''

    def test_a_readable_summary_is_still_applied(self, app, db_session, http_mock):
        from tests.test_ap_refresh_profiles import (_person_document, _remote_user)
        from app.activitypub.util import refresh_user_profile_task

        user = _remote_user()
        _serve(http_mock, user.ap_public_url,
               _person_document(fields={'summary': '<p>about me</p>'}))

        refresh_user_profile_task(user.id)

        db.session.refresh(user)
        assert user.about_html == '<p>about me</p>'


class TestCreatingACommunityWithAwkwardLanguages:
    """`actor_json_to_model`'s language loop, the second of the two. A community
    arriving for the first time must not be refused over a language entry."""

    @pytest.fixture
    def peer(self, app, db_session, http_mock):
        from tests.factories import peer_instance

        peer_instance(PEER)

    def group(self, language):
        from tests.factories import peer_actor_json

        return peer_actor_json('Group', name='langland', server=PEER,
                               fields={'language': language})

    @pytest.mark.parametrize('language', [
        ['en'],
        [{'identifier': 'en'}],
        [{'name': 'English'}],
        [{'identifier': 5, 'name': 'English'}],
        [None],
        [5],
        [{}],
    ])
    def test_the_community_is_still_created(self, peer, language):
        from app.activitypub.util import actor_json_to_model

        community = actor_json_to_model(self.group(language), 'langland', PEER)

        assert isinstance(community, Community)

    def test_a_usable_entry_beside_an_unusable_one_is_kept(self, peer):
        from app.activitypub.util import actor_json_to_model

        community = actor_json_to_model(
            self.group([5, {'identifier': 'fr', 'name': 'Francais'}]),
            'langland', PEER)

        assert 'fr' in [language.code for language in community.languages]
