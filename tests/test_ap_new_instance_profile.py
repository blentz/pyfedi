"""What this instance learns about one it has just heard of.

Sub-project 100 -- `new_instance_profile_task` in `app/activitypub/util.py`.
It runs the first time an activity arrives from a domain nobody here has seen:
it asks for the domain's actor document, then `/api/v3/site` for the list of
that instance's admins, then `/.well-known/nodeinfo` for what software it
runs. Every answer is the remote instance's own, and the task runs in a Celery
worker, so anything it raises is a traceback in a log and an Instance row that
stays empty -- no inbox to deliver to, no software recorded.

Two defects, measured first:

* `instance.outbox = instance_json['outbox']` -- the line immediately above it
  membership-tests `inbox`, and this one did not. An Application actor with no
  outbox was `KeyError: 'outbox'`, so nothing about that instance was learned,
  not even its software (D1290);
* `admin['person']['actor_id']` -- whatever `/api/v3/site` answered. An entry
  with no person was `KeyError: 'person'` (D1290).

One equivalent mutant: removing the `if 'software' in node_json` guard
survives, because the `KeyError` it would raise is swallowed by the bare
`except: return` around it and the software is left unset either way.

An instance's admins are the accounts it may name as having admin power over
ITS OWN communities (`Community.is_instance_admin` reads
`InstanceRole.instance_id == self.instance_id`). A remote instance naming an
actor on a third domain therefore gives away only its own authority, which is
why that is not filtered -- and `User.is_instance_admin`, the check that grants
power over an account's own instance, reads the account's OWN `instance_id`,
so a role held for somebody else's instance grants nothing there.
"""
from unittest.mock import patch

import httpx
import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import (find_instance_by_domain,
                                  new_instance_profile,
                                  new_instance_profile_task)
from app.models import Instance, InstanceRole, Site, User
from tests.factories import make_instance, make_user

DOMAIN = 'faraway.test'
ACTOR = f'https://{DOMAIN}/'
SITE = f'https://{DOMAIN}/api/v3/site'
NODEINFO = f'https://{DOMAIN}/.well-known/nodeinfo'
NODE = f'https://{DOMAIN}/nodeinfo/2.0'


@pytest.fixture
def env(app, api_baseline, http_mock, monkeypatch):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    instance = make_instance(DOMAIN, software='')
    instance.inbox = None
    instance.outbox = None
    db.session.commit()
    monkeypatch.setattr('app.activitypub.util.time.sleep',
                        lambda seconds: None)
    return SimpleNamespace(instance=instance, http=http_mock,
                           baseline=api_baseline)


def answer(env, actor=None, site=None, nodeinfo=None, node=None):
    """Mock the three (or four) requests the task makes, in order."""
    env.http.get(ACTOR).mock(return_value=actor if actor is not None
                             else httpx.Response(404))
    env.http.get(SITE).mock(return_value=site if site is not None
                            else httpx.Response(404))
    env.http.get(NODEINFO).mock(return_value=nodeinfo if nodeinfo is not None
                                else httpx.Response(404))
    if node is not None:
        env.http.get(NODE).mock(return_value=node)


def only_the_actor(env, response):
    """For the answers that stop the task before it asks anything else --
    `http_mock` runs with assert_all_called, so a route registered and never
    requested fails the test."""
    env.http.get(ACTOR).mock(return_value=response)


def reload(env):
    db.session.expire_all()
    return db.session.get(Instance, env.instance.id)


def an_application(**extra):
    document = {'type': 'Application', 'inbox': f'https://{DOMAIN}/inbox',
                'outbox': f'https://{DOMAIN}/outbox'}
    document.update(extra)
    return httpx.Response(200, json=document)


def nodeinfo_links(rel='http://nodeinfo.diaspora.software/ns/schema/2.0',
                   href=NODE):
    return httpx.Response(200, json={'links': [{'rel': rel, 'href': href}]})


def software(name='Lemmy', version='0.19.5'):
    return httpx.Response(200, json={'software': {'name': name,
                                                  'version': version}})


class TestLookingAnInstanceUpByName:
    def test_by_its_domain(self, env):
        assert find_instance_by_domain(DOMAIN) == env.instance

    def test_in_any_case_and_with_space_around_it(self, env):
        assert find_instance_by_domain(f'  {DOMAIN.upper()} ') == env.instance

    def test_a_domain_nobody_here_knows(self, env):
        assert find_instance_by_domain('nowhere.test') is None


class TestHowTheLookupIsDispatched:
    def test_in_debug_it_runs_here_and_now(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', True)
        with patch('app.activitypub.util.new_instance_profile_task') as task:
            new_instance_profile(env.instance.id)
        task.assert_called_once_with(env.instance.id)

    def test_otherwise_it_is_queued_with_a_delay(self, env, monkeypatch):
        monkeypatch.setattr(current_app, 'debug', False)
        with patch('app.activitypub.util.new_instance_profile_task') as task:
            new_instance_profile(env.instance.id)
        assert task.apply_async.call_args.kwargs['args'] == (env.instance.id,)
        assert 1 <= task.apply_async.call_args.kwargs['countdown'] <= 10

    def test_no_instance_named_at_all(self, env):
        with patch('app.activitypub.util.new_instance_profile_task') as task:
            new_instance_profile(None)
        assert task.call_count == 0
        assert task.apply_async.call_count == 0


class TestWhatTheActorDocumentSays:
    def test_an_application_names_both_its_boxes(self, env):
        answer(env, actor=an_application())
        new_instance_profile_task(env.instance.id)
        instance = reload(env)
        assert instance.inbox == f'https://{DOMAIN}/inbox'
        assert instance.outbox == f'https://{DOMAIN}/outbox'
        assert instance.updated_at is not None

    def test_one_that_names_no_inbox(self, env):
        document = an_application()
        answer(env, actor=httpx.Response(200, json={
            'type': 'Application', 'outbox': f'https://{DOMAIN}/outbox'}))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox == f'https://{DOMAIN}/inbox'

    def test_one_that_names_no_outbox(self, env):
        """D1290. This was `KeyError: 'outbox'`, and it killed the task before
        the software was ever asked for."""
        answer(env, actor=httpx.Response(200, json={
            'type': 'Application', 'inbox': f'https://{DOMAIN}/inbox'}),
               nodeinfo=nodeinfo_links(), node=software())
        new_instance_profile_task(env.instance.id)
        instance = reload(env)
        assert instance.inbox == f'https://{DOMAIN}/inbox'
        assert instance.outbox is None
        assert instance.software == 'lemmy'

    def test_something_that_is_not_an_application(self, env):
        """Most software answers with something else, and its inbox is at
        /inbox regardless."""
        answer(env, actor=httpx.Response(200, json={'type': 'Service'}))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox == f'https://{DOMAIN}/inbox'

    def test_an_answer_that_is_not_json(self, env):
        answer(env, actor=httpx.Response(200, text='<html>hello</html>'))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox == f'https://{DOMAIN}/inbox'

    @pytest.mark.parametrize('status', [404, 406])
    def test_the_two_statuses_that_still_mean_inbox_is_at_slash_inbox(
            self, env, status):
        only_the_actor(env, httpx.Response(status))
        env.http.get(NODEINFO).mock(return_value=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox == f'https://{DOMAIN}/inbox'

    def test_a_status_that_means_nothing_is_learned(self, env):
        only_the_actor(env, httpx.Response(500))
        env.http.get(NODEINFO).mock(return_value=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox is None

    def test_a_domain_that_only_answers_over_http(self, env):
        env.http.get(ACTOR).mock(side_effect=httpx.ConnectError('no tls'))
        env.http.get(f'http://{DOMAIN}/').mock(return_value=an_application())
        env.http.get(f'http://{DOMAIN}/api/v3/site').mock(
            return_value=httpx.Response(404))
        env.http.get(f'http://{DOMAIN}/.well-known/nodeinfo').mock(
            return_value=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox == f'https://{DOMAIN}/inbox'

    def test_a_domain_that_answers_neither_way(self, env):
        env.http.get(ACTOR).mock(side_effect=httpx.ConnectError('no route'))
        env.http.get(f'http://{DOMAIN}/').mock(
            side_effect=httpx.ConnectError('no route'))
        new_instance_profile_task(env.instance.id)
        assert reload(env).inbox is None


class TestWhoTheInstanceSaysItsAdminsAre:
    def an_admin(self, actor_id):
        return {'person': {'actor_id': actor_id}}

    @pytest.fixture
    def their_admin(self, env):
        admin = make_user(env.instance, 'boss')
        admin.ap_id = f'boss@{DOMAIN}'
        admin.ap_profile_id = f'https://{DOMAIN}/u/boss'
        admin.ap_public_url = f'https://{DOMAIN}/u/boss'
        db.session.commit()
        return admin

    def roles(self, env):
        return InstanceRole.query.filter_by(instance_id=env.instance.id).all()

    def test_one_they_name_is_recorded(self, env, their_admin):
        answer(env, actor=an_application(), site=httpx.Response(200, json={
            'admins': [self.an_admin(their_admin.ap_profile_id)]}))
        new_instance_profile_task(env.instance.id)
        assert [role.user_id for role in self.roles(env)] == [their_admin.id]

    def test_naming_the_same_one_twice_records_one_role(self, env,
                                                       their_admin):
        site = httpx.Response(200, json={
            'admins': [self.an_admin(their_admin.ap_profile_id)]})
        answer(env, actor=an_application(), site=site)
        new_instance_profile_task(env.instance.id)
        answer(env, actor=an_application(), site=httpx.Response(200, json={
            'admins': [self.an_admin(their_admin.ap_profile_id)]}))
        new_instance_profile_task(env.instance.id)
        assert len(self.roles(env)) == 1

    def test_one_they_have_stopped_naming_loses_the_role(self, env,
                                                        their_admin):
        db.session.add(InstanceRole(instance_id=env.instance.id,
                                    user_id=their_admin.id, role='admin'))
        db.session.commit()
        answer(env, actor=an_application(),
               site=httpx.Response(200, json={'admins': []}))
        new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []

    def test_an_entry_with_no_person(self, env, their_admin):
        """D1290. This was `KeyError: 'person'`."""
        answer(env, actor=an_application(), site=httpx.Response(200, json={
            'admins': [{'name': 'nobody'},
                       self.an_admin(their_admin.ap_profile_id)]}),
               nodeinfo=nodeinfo_links(), node=software())
        new_instance_profile_task(env.instance.id)
        assert [role.user_id for role in self.roles(env)] == [their_admin.id]
        assert reload(env).software == 'lemmy'

    @pytest.mark.parametrize('entry', [
        'a string', None, {'person': 'a string'}, {'person': {}},
        {'person': {'actor_id': ''}}, {'person': {'actor_id': None}},
    ])
    def test_every_other_shape_an_entry_can_take(self, env, entry):
        answer(env, actor=an_application(),
               site=httpx.Response(200, json={'admins': [entry]}))
        new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []

    def test_an_actor_this_instance_cannot_resolve(self, env):
        with patch('app.activitypub.util.find_actor_or_create',
                   return_value=None):
            answer(env, actor=an_application(),
                   site=httpx.Response(200, json={
                       'admins': [self.an_admin(f'https://{DOMAIN}/u/ghost')]}))
            new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []

    def test_a_site_endpoint_that_names_no_admins_at_all(self, env):
        answer(env, actor=an_application(),
               site=httpx.Response(200, json={'site_view': {}}))
        new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []

    def test_one_that_answers_something_that_is_not_json(self, env):
        answer(env, actor=an_application(),
               site=httpx.Response(200, text='<html>no api here</html>'))
        new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []

    def test_one_that_is_not_there(self, env):
        answer(env, actor=an_application(), site=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []

    def test_one_that_cannot_be_reached(self, env):
        env.http.get(ACTOR).mock(return_value=an_application())
        env.http.get(SITE).mock(side_effect=httpx.ConnectError('no route'))
        env.http.get(NODEINFO).mock(return_value=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert self.roles(env) == []


class TestWhatSoftwareTheInstanceRuns:
    def test_it_is_read_from_nodeinfo(self, env):
        answer(env, actor=an_application(), nodeinfo=nodeinfo_links(),
               node=software('Lemmy', '0.19.5'))
        new_instance_profile_task(env.instance.id)
        instance = reload(env)
        assert instance.software == 'lemmy'
        assert instance.version == '0.19.5'
        assert instance.nodeinfo_href == NODE

    @pytest.mark.parametrize('rel', [
        'http://nodeinfo.diaspora.software/ns/schema/2.0',
        'https://nodeinfo.diaspora.software/ns/schema/2.0',
        'http://nodeinfo.diaspora.software/ns/schema/2.1',
    ])
    def test_each_schema_this_instance_accepts(self, env, rel):
        answer(env, actor=an_application(), nodeinfo=nodeinfo_links(rel=rel),
               node=software())
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == 'lemmy'

    def test_a_schema_it_does_not_know_is_not_even_fetched(self, env):
        """Driven through `get_request` rather than respx, because what is
        asserted is that a request is NOT made -- and `http_mock` fails a
        route it registered and nobody called."""
        asked = []

        def record(url, *args, **kwargs):
            asked.append(url)
            if url == ACTOR:
                return an_application()
            if url == SITE:
                return httpx.Response(404)
            if url == NODEINFO:
                return nodeinfo_links(
                    rel='http://nodeinfo.diaspora.software/ns/schema/1.0')
            return software()

        with patch('app.activitypub.util.get_request', side_effect=record):
            new_instance_profile_task(env.instance.id)
        assert NODE not in asked
        assert reload(env).software == ''

    def test_a_link_that_is_not_an_object(self, env):
        answer(env, actor=an_application(),
               nodeinfo=httpx.Response(200, json={'links': ['a string']}))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''

    def test_nodeinfo_that_names_no_software(self, env):
        answer(env, actor=an_application(), nodeinfo=nodeinfo_links(),
               node=httpx.Response(200, json={'version': '2.0'}))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''

    def test_nodeinfo_that_is_not_there(self, env):
        answer(env, actor=an_application(), nodeinfo=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''

    def test_the_second_document_that_is_not_there(self, env):
        answer(env, actor=an_application(), nodeinfo=nodeinfo_links(),
               node=httpx.Response(404))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''

    def test_nodeinfo_that_cannot_be_reached(self, env):
        env.http.get(ACTOR).mock(return_value=an_application())
        env.http.get(SITE).mock(return_value=httpx.Response(404))
        env.http.get(NODEINFO).mock(
            side_effect=httpx.ConnectError('no route'))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''

    def test_the_second_document_being_unreachable(self, env):
        env.http.get(ACTOR).mock(return_value=an_application())
        env.http.get(SITE).mock(return_value=httpx.Response(404))
        env.http.get(NODEINFO).mock(return_value=nodeinfo_links())
        env.http.get(NODE).mock(side_effect=httpx.ConnectError('no route'))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''

    def test_nodeinfo_with_no_links_at_all(self, env):
        answer(env, actor=an_application(),
               nodeinfo=httpx.Response(200, json={}))
        new_instance_profile_task(env.instance.id)
        assert reload(env).software == ''
