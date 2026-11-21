"""Content settings that decide what the server stores, on a site with more than one domain (spec
2026-11-20-multitenant-domain-config, the storage rule).

What one copy of the server stores serves every domain, so for what is stored the most permissive domain's value
decides: an NSFW community is taken in when any domain shows NSFW, an incoming reply is dropped by a filter only when
every domain has that filter on. What each domain shows, and what its own users may do, is its own setting. Settings
read outside a request (a Celery task) are the first domain's.
"""
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import current_app, g

from app import db
from app.activitypub.util import actor_json_to_model, refresh_community_profile_task
from app.community.forms import is_chan_screenshot
from app.domain_config import any_domain, every_domain, every_domains, here
from app.models import Community, PostReply, PostReplyValidationError, SiteDomain, first_config
from app.storefront import clear_domain_cache, domain_for_host
from app.utils import blocked_referrers
from tests.factories import make_community, make_site, peer_instance
from tests.test_ap_actor_json_group import PEER, _group
from tests.test_ap_interop_actor_update import seed_community_owner
from tests.test_community_backfill_body import EMPTY_MODS, MODS, OUTBOX, an_announce, backfill, env, outbox, titles  # noqa: F401
from tests.test_models_post_reply_new import a_reply, an_activity
from tests.test_podcast_community import podcast_document
from tests.two_domains import request_on

SISTER = 'sister.example.org'


@pytest.fixture
def sister(app, db_session):
    """The first domain's row at SERVER_NAME and a sister domain, which inherits everything until it sets a value."""
    first = app.config['SERVER_NAME']
    if db.session.query(SiteDomain).filter_by(is_default=True).first() is None:
        db.session.add(SiteDomain(domain=first, scheme='https', is_default=True))
    db.session.add(SiteDomain(domain=SISTER, scheme='https', federates=False))
    db.session.commit()
    clear_domain_cache()
    yield domain_for_host(SISTER)
    clear_domain_cache()


def settings(domain=None, **values):
    """Set `values` on `domain` (a sister) or, with None, on the first domain, and commit."""
    row = first_config() if domain is None else db.session.get(SiteDomain, domain.id).config()
    for name, value in values.items():
        setattr(row, name, value)
    db.session.commit()


# ---------------------------------------------------------------------------------------------------------------------
# The readers

def test_here_is_the_request_domains_and_outside_a_request_the_first_domains(app, sister):
    settings(enable_nsfw=False)
    settings(sister, enable_nsfw=True)
    assert here('enable_nsfw') is False
    with request_on(app, sister):
        assert here('enable_nsfw') is True
    with request_on(app, domain_for_host(app.config['SERVER_NAME'])):
        assert here('enable_nsfw') is False


def test_a_sister_that_sets_nothing_has_the_first_domains_value_everywhere(app, sister):
    settings(enable_nsfl=True)
    assert every_domains('enable_nsfl') == [True, True]
    with request_on(app, sister):
        assert here('enable_nsfl') is True


def test_stored_when_any_domain_shows_it(app, sister):
    settings(enable_nsfw=False)
    assert any_domain('enable_nsfw') is False
    settings(sister, enable_nsfw=True)
    assert every_domains('enable_nsfw') == [False, True] and any_domain('enable_nsfw') is True
    settings(enable_nsfw=True)
    settings(sister, enable_nsfw=False)
    assert any_domain('enable_nsfw') is True


def test_dropped_only_when_every_domain_drops_it(app, sister):
    settings(enable_this_comment_filter=True)
    assert every_domain('enable_this_comment_filter') is True   # the sister inherits the first domain's
    settings(sister, enable_this_comment_filter=False)
    assert every_domain('enable_this_comment_filter') is False
    settings(enable_this_comment_filter=False)
    settings(sister, enable_this_comment_filter=True)
    assert every_domain('enable_this_comment_filter') is False


def test_with_no_sister_the_first_domain_decides_alone(app, db_session):
    settings(enable_nsfw=True, enable_chan_image_filter=False)
    assert every_domains('enable_nsfw') == [True]
    assert any_domain('enable_nsfw') is True and every_domain('enable_chan_image_filter') is False


# ---------------------------------------------------------------------------------------------------------------------
# What is stored: NSFW and NSFL communities, feeds and podcasts

def test_a_sensitive_community_is_taken_in_when_a_sister_shows_nsfw(app, sister, site):
    peer_instance(PEER)
    settings(enable_nsfw=False)
    assert actor_json_to_model(_group('memes', fields={'sensitive': True}), '!memes', PEER) is None
    settings(sister, enable_nsfw=True)
    community = actor_json_to_model(_group('memes', fields={'sensitive': True}), '!memes', PEER)
    assert community is not None and community.nsfw is True


def test_an_nsfl_community_is_taken_in_when_a_sister_shows_nsfl(app, sister, site):
    peer_instance(PEER)
    settings(enable_nsfl=False)
    assert actor_json_to_model(_group('gore', fields={'nsfl': True}), '!gore', PEER) is None
    settings(sister, enable_nsfl=True)
    assert actor_json_to_model(_group('gore', fields={'nsfl': True}), '!gore', PEER) is not None


def test_a_sensitive_podcast_gets_its_community_when_a_sister_shows_nsfw(app, sister):
    make_site()
    peer_instance(PEER)
    settings(enable_nsfw=False)
    settings(sister, enable_nsfw=True)
    assert actor_json_to_model(podcast_document(sensitive=True), 'mypodcast', PEER) is not None
    assert Community.query.count() == 1


def test_an_nsfw_community_is_backfilled_when_a_sister_shows_nsfw(env, sister):   # noqa: F811
    env.community.nsfw = True
    settings(enable_nsfw=False)
    settings(sister, enable_nsfw=True)
    backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce())})
    assert titles(env.community) == {'a post'}


def test_an_nsfl_community_is_not_backfilled_when_no_domain_shows_nsfl(env, sister):   # noqa: F811
    env.community.nsfl = True
    settings(enable_nsfl=False)
    backfill(env.community, {MODS: EMPTY_MODS, OUTBOX: outbox(an_announce())})
    assert titles(env.community) == set()


@pytest.mark.parametrize('shown', [True, False])
def test_a_local_communitys_nsfw_follows_its_own_domain_not_the_first(app, sister, shown):
    """R203 on a sister: a pushed document for a sister's community is held to the sister's switches."""
    seed_community_owner(PEER)
    make_site()
    settings(enable_nsfw=not shown, enable_nsfl=not shown)
    settings(sister, enable_nsfw=shown, enable_nsfl=shown)
    community = make_community(host=app.config['SERVER_NAME'])
    community.ap_followers_url = None
    community.site_domain_id = sister.id
    db.session.commit()
    community_id = community.id

    refresh_community_profile_task(community_id, {'type': 'Group', 'id': community.ap_profile_id,
                                                  'name': 'microblogs', 'sensitive': True, 'nsfl': True})

    db.session.expire_all()
    community = db.session.get(Community, community_id)
    assert (community.nsfw, community.nsfl) == (shown, shown)


# ---------------------------------------------------------------------------------------------------------------------
# Filters: an incoming reply against every domain, a local one against its own domain

GIF = 'https://giphy.test/a.gif'


def test_an_incoming_gif_reply_is_kept_while_a_sister_keeps_them(models_post_reply_new_env, sister):
    settings(enable_gif_reply_rep_decrease=True)
    settings(sister, enable_gif_reply_rep_decrease=False)
    with patch('app.utils.reply_is_just_link_to_gif_reaction', return_value=True):
        reply = a_reply(models_post_reply_new_env, body=GIF, request_json=an_activity())
    assert isinstance(reply, PostReply)


def test_an_incoming_gif_reply_is_dropped_when_every_domain_drops_them(models_post_reply_new_env, sister):
    settings(enable_gif_reply_rep_decrease=True)
    with patch('app.utils.reply_is_just_link_to_gif_reaction', return_value=True):
        with pytest.raises(PostReplyValidationError, match='Gif comment ignored'):
            a_reply(models_post_reply_new_env, body=GIF, request_json=an_activity())


def test_a_local_reply_is_held_to_the_filter_of_the_domain_it_is_written_on(models_post_reply_new_env, sister):
    settings(enable_this_comment_filter=False)
    settings(sister, enable_this_comment_filter=True)
    app = current_app._get_current_object()
    with patch('app.utils.reply_is_low_effort', return_value=True):
        with request_on(app, sister), pytest.raises(PostReplyValidationError, match='Low quality reply'):
            a_reply(models_post_reply_new_env, body='this')
        with request_on(app, domain_for_host(app.config['SERVER_NAME'])):
            assert isinstance(a_reply(models_post_reply_new_env, body='this'), PostReply)


def chan_upload():
    upload = BytesIO(b'not really a png')
    return SimpleNamespace(filename='screenshot.png', seek=upload.seek, read=upload.read)


def test_an_upload_is_read_for_chan_screenshots_on_a_domain_that_asks_for_it(app, sister, monkeypatch):
    settings(enable_chan_image_filter=False)
    settings(sister, enable_chan_image_filter=True)
    monkeypatch.setitem(app.config, 'CHAN_DETECTION_ENDPOINT', '')
    with patch('app.community.forms.Image.open'), \
            patch('app.community.forms.pytesseract.image_to_string', return_value='Anonymous No.12345'):
        with request_on(app, sister):
            assert is_chan_screenshot(chan_upload()) is True
        with request_on(app, domain_for_host(app.config['SERVER_NAME'])):
            assert is_chan_screenshot(chan_upload()) is False


# ---------------------------------------------------------------------------------------------------------------------
# Joining: the referrers a domain declines registration from are its own

def test_each_domain_declines_the_referrers_it_names(app, sister):
    settings(auto_decline_referrers='spam.example')
    settings(sister, auto_decline_referrers='other.example\n\nmore.example')
    assert blocked_referrers() == ['spam.example']
    with request_on(app, sister):
        assert blocked_referrers() == ['other.example', 'more.example']
    settings(sister, auto_decline_referrers='')
    with request_on(app, sister):
        assert blocked_referrers() == ['spam.example']   # blank: the first domain's


# ---------------------------------------------------------------------------------------------------------------------
# Votes: a peer's downvote is stored while any domain counts downvotes

def test_a_peers_downvote_is_stored_while_a_sister_counts_downvotes(app, sister):
    from app.utils import can_downvote
    from tests.factories import make_instance, make_user
    seed_community_owner(PEER)
    make_site()
    community = make_community(host=app.config['SERVER_NAME'])
    peer = make_user(make_instance('votes.example'), 'voter')
    settings(enable_downvotes=False)
    assert can_downvote(peer, community, incoming=True) is False
    settings(sister, enable_downvotes=True)
    assert can_downvote(peer, community, incoming=True) is True
    with request_on(app, domain_for_host(app.config['SERVER_NAME'])):
        g.site = SimpleNamespace(enable_downvotes=False)   # the first domain's page: its own users may not
        assert can_downvote(peer, community) is False
