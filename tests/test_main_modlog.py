"""The moderation log and the community directory.

Sub-project 86, slice C -- `modlog`, `modlog_search_suggestions` and
`list_communities` in `app/main/routes.py`. Two defects, both measured:

* the modlog's "filter by moderator" searched for the SUSPECT's name. The
  block is a copy of the one above it with one word left behind, and with no
  suspect named it matched nobody, fell through to a lookup that only finds
  remote accounts, and returned the whole unfiltered log (D1254);
* and the modlog was the one page in this module that answered a caller with
  no account on a PRIVATE instance -- 200, with its public entries, where
  /communities and / redirect to the login (D1255).
"""
import pytest
from flask import g

from app import db
from app.models import Instance, ModLog, Site
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)


def entries(response):
    """The modlog rows a page lists, counted by their reasons -- which appear
    only in entries, unlike the action names, which also fill the filter
    dropdown."""
    text = response.get_data(as_text=True)
    return sum(text.count(reason) for reason in ('spam', 'rude', 'quiet'))


@pytest.fixture
def env(app, api_baseline):
    """Two public modlog entries by two different moderators, against the same
    suspect, in the same community."""
    from types import SimpleNamespace

    g.admin_ids = [api_baseline.user1.id]
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    community = make_community('probeland')
    moderator = api_baseline.user2
    other_mod = api_baseline.user4
    suspect = api_baseline.user3
    db.session.add(ModLog(user_id=moderator.id, target_user_id=suspect.id,
                          community_id=community.id, type='mod',
                          action='delete_post', reason='spam', public=True,
                          link='post/1', link_text='a post'))
    db.session.add(ModLog(user_id=other_mod.id, target_user_id=suspect.id,
                          community_id=community.id, type='mod',
                          action='ban_user', reason='rude', public=True,
                          link='u/1', link_text='someone'))
    db.session.commit()
    return SimpleNamespace(client=app.test_client(), site=g.site,
                           community=community, moderator=moderator,
                           other_mod=other_mod, suspect=suspect,
                           admin=api_baseline.user1, baseline=api_baseline)


def login(client, user):
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True


class TestTheModlog:
    def test_it_lists_the_public_entries(self, env):
        response = env.client.get('/modlog')
        assert response.status_code == 200
        assert entries(response) == 2

    def test_filtering_by_action(self, env):
        response = env.client.get('/modlog?mod_action=ban_user')
        assert entries(response) == 1

    def test_filtering_by_the_suspect(self, env):
        alone = make_user(env.baseline.instance_local, 'innocent', local=True)
        db.session.add(ModLog(user_id=env.moderator.id,
                              target_user_id=alone.id,
                              community_id=env.community.id, type='mod',
                              action='delete_post', reason='quiet',
                              public=True, link='post/3', link_text='a post'))
        db.session.commit()
        response = env.client.get(
            f'/modlog?suspect_user_name={alone.user_name}')
        assert entries(response) == 1

    def test_filtering_by_the_moderator(self, env):
        """D1254. This block searched for `suspect_user_name`, so filtering by
        a moderator returned the whole log."""
        response = env.client.get(
            f'/modlog?user_name={env.moderator.user_name}')
        assert entries(response) == 1

    def test_filtering_by_a_moderator_and_a_suspect_at_once(self, env):
        response = env.client.get(
            f'/modlog?user_name={env.other_mod.user_name}'
            f'&suspect_user_name={env.suspect.user_name}')
        assert entries(response) == 1

    def test_filtering_by_community(self, env):
        elsewhere = make_community('elsewhere')
        db.session.add(ModLog(user_id=env.moderator.id,
                              target_user_id=env.suspect.id,
                              community_id=elsewhere.id, type='mod',
                              action='delete_post', reason='quiet',
                              public=True, link='post/4', link_text='a post'))
        db.session.commit()
        response = env.client.get(f'/modlog?communities={elsewhere.id}')
        assert entries(response) == 1

    def test_a_name_written_with_this_instances_domain(self, env):
        """`someone@this.instance` is the same account as `someone`."""
        handle = f"{env.moderator.user_name}@{env.site.name or 'x'}"
        response = env.client.get(f'/modlog?user_name={handle}')
        assert response.status_code == 200

    def test_a_private_entry_is_hidden_from_a_reader_with_no_account(self, env):
        db.session.add(ModLog(user_id=env.moderator.id,
                              target_user_id=env.suspect.id,
                              community_id=env.community.id, type='mod',
                              action='delete_post', reason='quiet',
                              public=False, link='post/2',
                              link_text='a post'))
        db.session.commit()
        response = env.client.get('/modlog')
        assert 'quiet' not in response.get_data(as_text=True)

    def test_a_private_entry_is_hidden_from_an_ordinary_account(self, env):
        db.session.add(ModLog(user_id=env.moderator.id,
                              target_user_id=env.suspect.id,
                              community_id=env.community.id, type='mod',
                              action='delete_post', reason='quiet',
                              public=False, link='post/2',
                              link_text='a post'))
        db.session.commit()
        login(env.client, env.suspect)
        response = env.client.get('/modlog')
        assert 'quiet' not in response.get_data(as_text=True)

    def test_an_administrator_sees_the_private_entries(self, env):
        db.session.add(ModLog(user_id=env.moderator.id,
                              target_user_id=env.suspect.id,
                              community_id=env.community.id, type='mod',
                              action='delete_post', reason='quiet',
                              public=False, link='post/2',
                              link_text='a post'))
        db.session.commit()
        login(env.client, env.admin)
        response = env.client.get('/modlog')
        assert 'quiet' in response.get_data(as_text=True)

    def test_a_private_instance_will_not_show_it_to_a_stranger(self, env):
        """D1255. This was the one page in the module that answered anyway."""
        env.site.private_instance = True
        db.session.commit()
        response = env.client.get('/modlog')
        assert response.status_code == 302
        assert '/auth/login' in response.headers['Location']

    def test_a_private_instance_shows_it_to_an_account(self, env):
        env.site.private_instance = True
        db.session.commit()
        login(env.client, env.suspect)
        assert env.client.get('/modlog').status_code == 200

    def test_the_second_page(self, env):
        assert env.client.get('/modlog?page=2').status_code == 200

    def test_a_low_bandwidth_reader(self, env):
        env.client.set_cookie('low_bandwidth', '1')
        assert env.client.get('/modlog').status_code == 200


class TestTheSearchSuggestions:
    def test_it_answers(self, env):
        response = env.client.post('/modlog/search_suggestions',
                                   data={'search': 'user'})
        assert response.status_code == 200


class TestTheCommunityDirectory:
    @pytest.mark.parametrize('query', [
        '', '?search=probe', '?home_select=local', '?home_select=any',
        '?subscribe_select=yes', '?subscribe_select=any', '?nsfw=no',
        '?nsfw=all', '?sort_by=name asc', '?instance=test.piefed.local',
        '?page=2', '?topic_id=0', '?feed_id=0', '?language_id=0',
    ])
    def test_every_way_of_narrowing_it(self, env, query):
        assert env.client.get(f'/communities{query}').status_code == 200

    def test_it_lists_a_community(self, env):
        response = env.client.get('/communities')
        assert 'probeland' in response.get_data(as_text=True)

    def test_a_private_instance_will_not_show_it_to_a_stranger(self, env):
        env.site.private_instance = True
        db.session.commit()
        assert env.client.get('/communities').status_code == 302

    def test_an_account_sees_it_on_a_private_instance(self, env):
        env.site.private_instance = True
        db.session.commit()
        login(env.client, env.suspect)
        assert env.client.get('/communities').status_code == 200

    def test_nsfw_is_forced_off_when_the_site_does_not_allow_it(self, env):
        env.site.enable_nsfw = False
        db.session.commit()
        assert env.client.get('/communities?nsfw=all').status_code == 200
