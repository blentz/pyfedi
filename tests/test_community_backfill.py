"""Filling in a community this instance has just heard of.

Sub-project 87 slice B -- `retrieve_mods_and_backfill` in
`app/community/util.py`. It runs as a Celery task after
`search_for_community` creates a remote community, and everything it reads is
JSON another instance sent. Two defects, both measured:

* a moderators collection with no `type` key was `KeyError: 'type'`, though
  the very next clause membership-tests `orderedItems` (D1259);
* and in the PeerTube and Guppe branch, an outbox entry with no `object` was
  `KeyError: 'object'`, though the branch below it membership-tests the same
  key. One malformed entry stopped the whole backfill (D1260).

Both killed the task, so the community existed with nothing in it and the
failure was a traceback in a worker log.
"""
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.community.util import retrieve_mods_and_backfill
from app.models import Community, CommunityMember, Site
from tests.factories import make_community

MODS_URL = 'https://remote.test/c/faraway/moderators'
OUTBOX_URL = 'https://remote.test/c/faraway/outbox'


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('faraway', host='remote.test')
    community.ap_id = 'faraway@remote.test'
    community.ap_moderators_url = MODS_URL
    community.ap_outbox_url = OUTBOX_URL
    db.session.commit()
    return SimpleNamespace(community=community, baseline=api_baseline)


def backfill(community, answers):
    """Run the task with `remote_object_to_json` answering from `answers`."""
    def fake(url, *args, **kwargs):
        return answers.get(url)

    with patch('app.community.util.remote_object_to_json', side_effect=fake):
        retrieve_mods_and_backfill(community.id, 'remote.test', 'faraway')


EMPTY_MODS = {'type': 'OrderedCollection', 'orderedItems': []}


class TestWhatTheRemoteSaysAboutItsModerators:
    def test_a_collection_with_no_type_at_all(self, env):
        """D1259. This was `KeyError: 'type'`, and it killed the task."""
        backfill(env.community, {MODS_URL: {'orderedItems': []},
                                 OUTBOX_URL: None})

    def test_a_collection_that_is_not_one(self, env):
        backfill(env.community, {MODS_URL: {'type': 'Person'},
                                 OUTBOX_URL: None})

    def test_a_moderators_url_that_answers_nothing(self, env):
        backfill(env.community, {MODS_URL: None, OUTBOX_URL: None})

    def test_an_empty_collection(self, env):
        backfill(env.community, {MODS_URL: EMPTY_MODS, OUTBOX_URL: None})
        assert CommunityMember.query.filter_by(
            community_id=env.community.id, is_moderator=True).count() == 0

    def test_a_community_that_names_no_moderators_url(self, env):
        env.community.ap_moderators_url = None
        db.session.commit()
        backfill(env.community, {OUTBOX_URL: None})

    def test_a_community_nobody_holds(self, env):
        """The task is queued with an id; by the time it runs the row may be
        gone."""
        backfill(type('_', (), {'id': 999999})(), {})


class TestWhatTheRemoteSaysAboutItsPosts:
    def test_an_outbox_with_no_type(self, env):
        backfill(env.community, {MODS_URL: EMPTY_MODS,
                                 OUTBOX_URL: {'orderedItems': []}})

    def test_an_outbox_that_says_it_is_empty(self, env):
        backfill(env.community, {MODS_URL: EMPTY_MODS,
                                 OUTBOX_URL: {'type': 'OrderedCollection',
                                              'totalItems': 0,
                                              'orderedItems': []}})

    def test_an_outbox_that_answers_nothing(self, env):
        backfill(env.community, {MODS_URL: EMPTY_MODS, OUTBOX_URL: None})

    def test_an_outbox_whose_entries_carry_no_object(self, env):
        backfill(env.community,
                 {MODS_URL: EMPTY_MODS,
                  OUTBOX_URL: {'type': 'OrderedCollection',
                               'orderedItems': [{'type': 'Announce'}]}})

    def test_a_paginated_outbox_whose_first_page_answers_nothing(self, env):
        page = 'https://remote.test/c/faraway/outbox?page=1'
        backfill(env.community,
                 {MODS_URL: EMPTY_MODS,
                  OUTBOX_URL: {'type': 'OrderedCollection', 'first': page},
                  page: None})

    def test_a_community_that_names_no_outbox(self, env):
        env.community.ap_outbox_url = None
        db.session.commit()
        backfill(env.community, {MODS_URL: EMPTY_MODS})


class TestAPeertubeChannel:
    """PeerTube and Guppe send a different outbox shape, and the branch that
    reads it is the one that skipped the membership test."""

    @pytest.fixture
    def channel(self, env):
        env.community.ap_profile_id = \
            'https://remote.test/video-channels/faraway'
        db.session.commit()
        return env.community

    def test_an_entry_with_no_object(self, env, channel):
        """D1260. One malformed entry was `KeyError: 'object'` and stopped the
        whole backfill."""
        backfill(channel,
                 {MODS_URL: EMPTY_MODS,
                  OUTBOX_URL: {'type': 'OrderedCollection',
                               'orderedItems': [{'type': 'Announce'}]}})

    def test_a_good_entry_beside_a_malformed_one(self, env, channel):
        """The malformed entry is skipped, not fatal."""
        backfill(channel,
                 {MODS_URL: EMPTY_MODS,
                  OUTBOX_URL: {'type': 'OrderedCollection',
                               'orderedItems': [{'type': 'Announce'},
                                                {'type': 'Announce',
                                                 'object':
                                                 'https://remote.test/v/1'}]},
                  'https://remote.test/v/1': None})
