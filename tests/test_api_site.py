"""The site API: what an instance says about itself, the instance chooser,
link metadata and instance blocks.

Sub-project 84, slice P -- `app/api/alpha/utils/site.py`, and the one view it
leans on. Four defects, all measured first:

* the instance chooser read `db.session.get(Language, g.site.language_id)`
  and then `language.id`. `Site.language_id` is nullable and an instance that
  has never chosen one leaves it so -- so the page a NEW instance is likeliest
  to be asked for was a 500 on exactly the instances least likely to have set
  it (D1244);
* `InstanceChooser.data` is free-form JSON, and the chooser search read
  `data['registration_mode']` and `data['language']['name']` out of it
  unguarded: ONE row missing either key was a KeyError that took the whole
  listing with it, for every caller, until somebody edited that row (D1245);
* `get_site_metadata` read `data['url']` unchecked (D1246);
* blocking an instance nobody holds reached the database as an insert against
  a missing foreign key, so the caller got psycopg2's ForeignKeyViolation with
  the SQL in it and the rest of the request's session was poisoned behind it
  (D1247).
"""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.api.alpha.utils.site import (get_federated_instances, get_site,
                                      get_site_instance_chooser,
                                      get_site_instance_chooser_search,
                                      get_site_metadata, get_site_version,
                                      post_site_block)
from app.constants import VERSION
from app.models import (BannedInstances, Instance, InstanceBlock,
                        InstanceChooser, Language, Site)

MISSING = 999999


def token(user):
    return f'Bearer {user.encode_jwt_token()}'


def a_chooser(domain='elsewhere.test', **data):
    """A row of the instance chooser, whose `data` is free-form JSON."""
    payload = {
        'elevator_pitch': 'a nice place',
        'registration_mode': 'Open',
        'language': {'name': 'English', 'code': 'en'},
    }
    payload.update(data)
    row = InstanceChooser(domain=domain, data=payload)
    db.session.add(row)
    db.session.commit()
    return row


def domains(res):
    return sorted(entry['domain'] for entry in res['result'])


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return SimpleNamespace(site=g.site, reader=api_baseline.user3,
                           baseline=api_baseline)


class TestTheSiteItself:
    def test_an_anonymous_reader(self, env):
        res = get_site(None)
        assert res['version'] == VERSION
        assert 'site' in res

    def test_a_reader_with_an_account(self, env):
        res = get_site(token(env.reader))
        assert res['my_user']['local_user_view']['person']['id'] == \
            env.reader.id

    def test_the_version_on_its_own(self, env):
        assert get_site_version(None) == {'version': VERSION}

    def test_the_federated_instances(self, env):
        res = get_federated_instances({})
        listed = [entry['domain']
                  for entry in res['federated_instances']['linked']]
        assert env.baseline.instance_remote.domain in listed

    def test_a_banned_instance_is_listed_as_blocked(self, env):
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        res = get_federated_instances({})
        blocked = [entry['domain']
                   for entry in res['federated_instances']['blocked']]
        assert 'nasty.test' in blocked


class TestTheInstanceChooser:
    def test_an_instance_that_has_chosen_a_language(self, env):
        english = Language(code='en', name='English')
        db.session.add(english)
        db.session.commit()
        env.site.language_id = english.id
        db.session.commit()
        res = get_site_instance_chooser(None)
        assert res['language']['code'] == 'en'
        assert res['language']['name'] == 'English'

    def test_an_instance_that_has_not(self, env):
        """D1244. `Site.language_id` is nullable, and the chooser read
        `language.id` off what `db.session.get` answered for None.

        The lookup is skipped rather than handed the None, because
        `db.session.get(Model, None)` warns `SAWarning: fully NULL primary key
        identity cannot load any object` -- which is why this row watches the
        warnings as well as the answer (fact 560).
        """
        import warnings

        env.site.language_id = None
        db.session.commit()
        with warnings.catch_warnings(record=True) as raised:
            warnings.simplefilter('always')
            res = get_site_instance_chooser(None)
        assert res['language'] == {'id': None, 'code': None, 'name': None}
        assert [str(w.message) for w in raised
                if 'NULL primary key' in str(w.message)] == []


class TestTheChooserSearch:
    def test_every_instance_by_default(self, env):
        a_chooser()
        a_chooser('another.test')
        assert domains(get_site_instance_chooser_search({})) == \
            ['another.test', 'elsewhere.test']

    def test_a_search_of_the_domain(self, env):
        a_chooser()
        a_chooser('another.test')
        assert domains(get_site_instance_chooser_search({'q': 'another'})) == \
            ['another.test']

    def test_a_search_of_the_elevator_pitch(self, env):
        a_chooser()
        a_chooser('another.test', elevator_pitch='a quiet corner')
        assert domains(get_site_instance_chooser_search({'q': 'quiet'})) == \
            ['another.test']

    def test_an_empty_search_narrows_nothing(self, env):
        a_chooser()
        assert domains(get_site_instance_chooser_search({'q': ''})) == \
            ['elsewhere.test']

    @pytest.mark.parametrize('asked, expected', [
        ('yes', ['nsfw.test']),
        ('no', ['elsewhere.test']),
        ('', ['elsewhere.test', 'nsfw.test']),
        ('maybe', ['elsewhere.test', 'nsfw.test']),
    ])
    def test_the_nsfw_filter(self, env, asked, expected):
        a_chooser()
        rough = a_chooser('nsfw.test')
        rough.nsfw = True
        db.session.commit()
        assert domains(get_site_instance_chooser_search({'nsfw': asked})) == \
            expected

    @pytest.mark.parametrize('asked, expected', [
        ('yes', ['newbie.test']),
        ('no', ['elsewhere.test']),
        ('', ['elsewhere.test', 'newbie.test']),
        ('maybe', ['elsewhere.test', 'newbie.test']),
    ])
    def test_the_newbie_filter(self, env, asked, expected):
        # `newbie_friendly` defaults to True, so the one that is NOT friendly
        # is the one that has to say so.
        plain = a_chooser()
        plain.newbie_friendly = False
        a_chooser('newbie.test')
        db.session.commit()
        assert domains(
            get_site_instance_chooser_search({'newbie': asked})) == expected

    def test_a_language_filter(self, env):
        english = Language(code='en', name='English')
        french = Language(code='fr', name='French')
        db.session.add_all([english, french])
        db.session.commit()
        a_chooser().language_id = english.id
        a_chooser('french.test').language_id = french.id
        db.session.commit()
        assert domains(
            get_site_instance_chooser_search({'language': 'fr'})) == \
            ['french.test']

    def test_a_language_nobody_has_narrows_nothing(self, env):
        a_chooser()
        assert domains(
            get_site_instance_chooser_search({'language': 'zz'})) == \
            ['elsewhere.test']

    def test_an_instance_that_asked_to_be_hidden(self, env):
        hidden = a_chooser('hidden.test')
        hidden.hide = True
        db.session.commit()
        a_chooser()
        assert domains(get_site_instance_chooser_search({})) == \
            ['elsewhere.test']

    def test_an_instance_this_one_has_banned(self, env):
        a_chooser('nasty.test')
        a_chooser()
        db.session.add(BannedInstances(domain='nasty.test'))
        db.session.commit()
        assert domains(get_site_instance_chooser_search({})) == \
            ['elsewhere.test']

    def test_an_instance_that_is_not_taking_registrations(self, env):
        a_chooser('closed.test', registration_mode='Closed')
        a_chooser()
        assert domains(get_site_instance_chooser_search({})) == \
            ['elsewhere.test']

    def test_the_language_comes_back_as_its_name(self, env):
        a_chooser()
        res = get_site_instance_chooser_search({})
        assert res['result'][0]['language'] == 'English'

    def test_a_row_that_does_not_say_how_it_registers(self, env):
        """D1245. One row missing a key took the whole listing with it."""
        row = InstanceChooser(domain='sparse.test',
                              data={'elevator_pitch': 'x'})
        db.session.add(row)
        a_chooser()
        res = get_site_instance_chooser_search({})
        assert domains(res) == ['elsewhere.test', 'sparse.test']

    def test_a_row_that_names_no_language(self, env):
        """D1245."""
        row = InstanceChooser(domain='sparse.test',
                              data={'registration_mode': 'Open'})
        db.session.add(row)
        db.session.commit()
        res = get_site_instance_chooser_search({})
        assert res['result'][0]['language'] is None

    def test_a_row_whose_language_is_already_a_name(self, env):
        """D1245. Written by hand, some rows carry a string where others carry
        the whole language object."""
        row = InstanceChooser(domain='sparse.test',
                              data={'registration_mode': 'Open',
                                    'language': 'English'})
        db.session.add(row)
        db.session.commit()
        res = get_site_instance_chooser_search({})
        assert res['result'][0]['language'] == 'English'

    def test_a_row_with_no_data_at_all(self, env):
        """D1245."""
        row = InstanceChooser(domain='sparse.test', data=None)
        db.session.add(row)
        db.session.commit()
        res = get_site_instance_chooser_search({})
        assert domains(res) == ['sparse.test']

    def test_the_stored_row_is_left_as_it_was(self, env):
        """The listing rewrites `language` and adds `domain` and `id` to the
        dict it hands back; that must not become what the row says."""
        row = a_chooser()
        get_site_instance_chooser_search({})
        db.session.commit()
        stored = db.session.get(InstanceChooser, row.id)
        assert stored.data['language'] == {'name': 'English', 'code': 'en'}
        assert 'domain' not in stored.data


class TestLinkMetadata:
    def test_naming_no_url(self, env):
        """D1246."""
        with pytest.raises(Exception, match='url required'):
            get_site_metadata(None, {})

    def test_naming_nothing_at_all(self, env):
        """D1246."""
        with pytest.raises(Exception, match='url required'):
            get_site_metadata(None, None)

    def test_a_page_that_answers_nothing(self, env):
        with patch('app.api.alpha.utils.site.opengraph_parse',
                   return_value=None):
            with pytest.raises(Exception, match='fetch_failed'):
                get_site_metadata(None, {'url': 'https://example.test/x'})

    def test_a_page_with_everything(self, env):
        with patch('app.api.alpha.utils.site.opengraph_parse',
                   return_value={'og:title': 'a title',
                                 'description': 'a description',
                                 'og:image': 'https://example.test/i.png'}):
            res = get_site_metadata(None, {'url': 'https://example.test/x'})
        assert res['metadata'] == {'title': 'a title',
                                   'description': 'a description',
                                   'image': 'https://example.test/i.png',
                                   'embed_video_url': ''}

    def test_a_page_with_only_a_title(self, env):
        with patch('app.api.alpha.utils.site.opengraph_parse',
                   return_value={'og:title': 'a title'}):
            res = get_site_metadata(None, {'url': 'https://example.test/x'})
        assert res['metadata'] == {'title': 'a title', 'description': '',
                                   'image': '', 'embed_video_url': ''}

    def test_a_reader_with_an_account(self, env):
        with patch('app.api.alpha.utils.site.opengraph_parse',
                   return_value={'og:title': 'a title'}):
            res = get_site_metadata(token(env.reader),
                                    {'url': 'https://example.test/x'})
        assert res['metadata']['title'] == 'a title'


class TestInstanceBlocks:
    def test_an_instance_nobody_holds_is_refused_by_name(self, env):
        """D1247. This reached the database as an insert against a missing
        foreign key."""
        with pytest.raises(Exception, match='instance not found'):
            post_site_block(token(env.reader),
                            {'instance_id': MISSING, 'block': True})

    def test_blocking_and_unblocking(self, env):
        remote = env.baseline.instance_remote
        assert post_site_block(token(env.reader),
                               {'instance_id': remote.id,
                                'block': True}) == {'blocked': True}
        assert InstanceBlock.query.filter_by(
            user_id=env.reader.id, instance_id=remote.id).count() == 1
        assert post_site_block(token(env.reader),
                               {'instance_id': remote.id,
                                'block': False}) == {'blocked': False}
        assert InstanceBlock.query.filter_by(
            user_id=env.reader.id, instance_id=remote.id).count() == 0
