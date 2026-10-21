"""Every hosted domain's instance row counts as "hosted here" (plan C2, task 3).

Row 1 is the default domain's; the onion has a row of its own (tests/two_domains.py). A site that tests
`Instance.id != 1` for "a peer" takes the onion's row for one: it lists it beside hell.cloud's peers in public, which
links the two, and delivers to, polls and counts failures against its host. Each test here sets up an onion row a
peer list would take, and a remote peer beside it as the control: the peer is listed, the onion's row never is.
"""
import json
from datetime import timedelta
from unittest.mock import patch

import httpx
import pytest
import respx
from flask import g

import app.activitypub.routes as ap_routes
import app.cli as cli
import app.community.live as live
import app.shared.tasks.maintenance as maintenance
import app.shared.tasks.pages as pages
from app import db
from app.activitypub.routes import announce_activity_to_followers
from app.api.alpha.utils.private_message import post_private_message_report
from app.api.alpha.views import federated_instances_view, site_instance_chooser_view
from app.community.live import busiest_microblog_servers, live_feed_keys, post_community_link
from app.models import (ActivityPubLog, ChatMessage, CommunityMember, Feed, FeedMember, File, Instance, Report, Site,
                        UserFollower, utcnow)
from app.shared.tasks.maintenance import check_instance_health, cleanup_old_voting_data, monitor_healthy_instances
from app.shared.tasks.pages import send_post
from app.utils import fediverse_domains
from app.visibility import follower_instances, followers_only_reply_instances
from tests.factories import (a_keypair, make_conversation, make_instance, make_post, make_post_reply,
                             make_post_reply_vote, make_post_vote, make_site, make_user)
from tests.test_per_domain_ap_routes import SHOP, get as ap_get, onion_proxy, twins  # noqa: F401
from tests.test_per_domain_html_routes import Visitor
from tests.two_domains import HELL, ONION, no_http, two_domains  # noqa: F401

PEER = 'peer.example'


@pytest.fixture
def sisters(app, two_domains):
    """hell.cloud and the onion, each with a local user and community, and a remote peer with a user.

    The onion's row is given what a peer's has, an inbox and a nodeinfo URL, so only its being hosted keeps it out:
    a row that federated once, or a field set by mistake, must not put it back on a peer list."""
    hell, onion = two_domains.domains
    onion_row = db.session.get(Instance, onion.instance_id)
    onion_row.inbox = f'http://{ONION}/inbox'
    onion_row.nodeinfo_href = f'http://{ONION}/nodeinfo/2.0'
    onion_row.software = 'piefed'
    peer = make_instance(PEER, software='piefed')
    peer.inbox = f'https://{PEER}/inbox'
    peer.nodeinfo_href = f'https://{PEER}/nodeinfo/2.0'
    db.session.commit()
    hell_user = two_domains.local_user('hellion', hell)
    onion_user = two_domains.local_user('onionist', onion)
    peer_user = make_user(peer, 'visitor')
    hell_community = two_domains.local_community('art', hell)
    return hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community


def test_the_onion_row_is_hosted(app, sisters):
    hell, onion, onion_row, peer, *_ = sisters
    assert onion_row.id != 1 and onion_row.id != peer.id


# --- peer lists ------------------------------------------------------------------------------------------------------

def test_the_lemmy_federated_instances_api_names_no_hosted_domain(app, sisters):
    body = app.test_client().get('/api/v3/federated_instances', base_url=f'https://{HELL}').get_data(as_text=True)
    assert PEER in body
    assert ONION not in body


def test_the_alpha_federated_instances_view_names_no_hosted_domain(app, sisters):
    body = json.dumps(federated_instances_view(), default=str)
    assert PEER in body
    assert ONION not in body


def test_the_alpha_instance_chooser_trusts_no_hosted_domain(app, sisters):
    hell, onion, onion_row, peer, *_ = sisters
    make_site()
    g.site = db.session.get(Site, 1)
    onion_row.trusted = peer.trusted = True   # an admin can tick "trusted" on any row
    db.session.commit()
    assert site_instance_chooser_view()['trusts'] == [PEER]


def test_hells_home_sidebar_names_no_hosted_domain(app, sisters):
    """The "new instances" card lists live piefed peers, newest first. The onion's row is the newest, and is given
    the lowercase software every peer's nodeinfo stores."""
    hell, onion, onion_row, peer, *_ = sisters
    make_site().private_instance = False
    onion_row.software = 'piefed'
    db.session.commit()
    body = app.test_client().get('/', base_url=f'https://{HELL}').get_data(as_text=True)
    assert PEER in body
    assert ONION not in body


def test_fediverse_domains_names_no_hosted_domain(app, sisters):
    assert PEER in fediverse_domains()
    assert ONION not in fediverse_domains()


def test_the_instances_federated_filter_names_no_hosted_domain(app, sisters):
    make_site().private_instance = False
    db.session.commit()
    with patch('app.instance.routes.render_template', return_value='rendered') as render:
        app.test_client().get('/instances?filters=federated')
    assert [row.domain for row in render.call_args.kwargs['instances'].items] == [PEER]


def test_who_follows_a_community_or_user_is_never_a_hosted_instance(app, sisters):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    for member in (onion_user, peer_user):
        db.session.add(CommunityMember(user_id=member.id, community_id=hell_community.id))
        db.session.add(UserFollower(local_user_id=hell_user.id, remote_user_id=member.id, is_inward=True,
                                    is_accepted=True))
    db.session.commit()

    assert hell_community.following_instances() == [peer]
    assert hell_community.following_instances(include_dormant=True, mod_hosts_only=False) == [peer]
    assert hell_user.following_instances() == [peer]
    assert follower_instances(hell_user) == [peer]


def test_a_conversations_instances_are_never_hosted(app, sisters):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, _ = sisters
    conversation = make_conversation(hell_user, onion_user)
    conversation.members.append(peer_user)
    db.session.commit()
    assert conversation.instances() == [peer]


def test_a_followers_only_reply_to_an_onion_author_adds_no_instance(app, sisters):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    post = make_post(hell_community, onion_user, None)
    reply = make_post_reply(post, hell_user)
    assert followers_only_reply_instances(reply) == []


def test_a_feeds_following_instances_are_never_hosted(app, sisters):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, _ = sisters
    feed = Feed(user_id=hell_user.id, title='f', name='f', machine_name='f', public=True)
    db.session.add(feed)
    db.session.commit()
    for member in (onion_user, peer_user):
        db.session.add(FeedMember(user_id=member.id, feed_id=feed.id))
    db.session.commit()
    assert feed.following_instances() == [peer]


# --- delivery ---------------------------------------------------------------------------------------------------------

@pytest.fixture
def sent(monkeypatch):
    """The inboxes a delivery posted to; nothing leaves."""
    inboxes = []

    def recording_send(uri, *args, **kwargs):
        inboxes.append(uri)
        return True

    monkeypatch.setattr(pages, 'send_post_request', recording_send)
    monkeypatch.setattr(ap_routes, 'send_to_remote_instance_fast', lambda inbox, *args: inboxes.append(inbox))
    return inboxes


def test_a_hell_post_with_an_onion_follower_goes_to_the_peer_only(app, sisters, no_http):
    """The real send path runs (Celery is eager): post_request signs and POSTs through httpx, which respx answers.
    The onion's inbox has a route that would answer 500, a refusal post_request books against the destination, so
    selecting the onion's row shows as a call to that route and as a failure logged to its inbox."""
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    hell_user.private_key, hell_user.public_key = a_keypair()
    hell_community.private_key, hell_community.public_key = a_keypair()
    for member in (onion_user, peer_user):
        db.session.add(CommunityMember(user_id=member.id, community_id=hell_community.id))
        db.session.add(UserFollower(local_user_id=hell_user.id, remote_user_id=member.id, is_inward=True,
                                    is_accepted=True))
    db.session.commit()
    post = make_post(hell_community, hell_user, None)

    with respx.mock(assert_all_called=False) as router:
        to_peer = router.post(f'https://{PEER}/inbox').respond(202)
        to_onion = router.route(host=ONION).respond(500)
        send_post(post.id, session=db.session)

    assert to_peer.called
    assert not to_onion.called
    assert no_http == []
    db.session.expire_all()
    assert ActivityPubLog.query.filter(ActivityPubLog.exception_message.contains(ONION)).count() == 0
    assert db.session.get(Instance, onion_row.id).failures in (0, None)


def test_a_flag_from_an_onion_author_is_not_sent_to_the_onion(app, sisters, sent):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    hell_community.private_key, hell_community.public_key = a_keypair()
    db.session.commit()
    activity = {'id': f'https://{HELL}/flag/1', 'type': 'Flag', 'actor': hell_user.ap_profile_id}

    announce_activity_to_followers(hell_community, hell_user, activity, is_flag=True,
                                   admin_instance_id=onion_row.id)
    assert sent == []

    announce_activity_to_followers(hell_community, hell_user, activity, is_flag=True, admin_instance_id=peer.id)
    assert sent == [f'https://{PEER}/inbox']


# --- polling ----------------------------------------------------------------------------------------------------------

@pytest.fixture
def polled(monkeypatch):
    urls = []

    def recording_get(url, *args, **kwargs):
        urls.append(url)
        return httpx.Response(status_code=404)

    monkeypatch.setattr(maintenance, 'get_request_instance', recording_get)
    monkeypatch.setattr(maintenance, 'get_request', recording_get)
    return urls


def test_the_healthy_instance_monitor_never_polls_a_hosted_row(app, sisters, polled):
    hell, onion, onion_row, peer, *_ = sisters
    monitor_healthy_instances()
    assert any(PEER in url for url in polled)
    assert not [url for url in polled if ONION in url]
    db.session.expire_all()
    assert db.session.get(Instance, onion_row.id).failures in (0, None)


def test_the_dormant_recheck_never_polls_a_hosted_row(app, sisters, polled):
    hell, onion, onion_row, peer, *_ = sisters
    for row in (onion_row, peer):
        row.dormant = True
        row.start_trying_again = utcnow() + timedelta(days=1)
    db.session.commit()
    check_instance_health()
    assert any(PEER in url for url in polled)
    assert not [url for url in polled if ONION in url]
    db.session.expire_all()
    assert db.session.get(Instance, onion_row.id).dormant is True   # untouched: never rechecked, never woken


# --- retention --------------------------------------------------------------------------------------------------------

def test_a_sister_domains_votes_keep_the_local_cutoff(app, sisters, monkeypatch):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    monkeypatch.setitem(app.config, 'KEEP_LOCAL_VOTE_DATA_TIME', 12)
    monkeypatch.setitem(app.config, 'KEEP_REMOTE_VOTE_DATA_TIME', 1)
    post = make_post(hell_community, hell_user, None)
    reply = make_post_reply(post, hell_user)
    stamp = utcnow() - timedelta(days=28 * 2)
    for voter in (hell_user, onion_user, peer_user):
        make_post_vote(voter, post, 1.0).created_at = stamp
        make_post_reply_vote(voter, reply, 1.0).created_at = stamp
    db.session.commit()

    cleanup_old_voting_data()

    kept = {table: set(db.session.execute(db.text(f'SELECT user_id FROM {table}')).scalars())
            for table in ('post_vote', 'post_reply_vote')}
    assert kept == {'post_vote': {hell_user.id, onion_user.id}, 'post_reply_vote': {hell_user.id, onion_user.id}}


# --- moderation -------------------------------------------------------------------------------------------------------

def test_an_onion_users_report_reads_as_local(app, sisters):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, _ = sisters
    g.admin_ids = []
    conversation = make_conversation(hell_user, onion_user)
    message = ChatMessage(sender_id=hell_user.id, recipient_id=onion_user.id, body='hi',
                          conversation_id=conversation.id)
    db.session.add(message)
    db.session.commit()

    answer = post_private_message_report(f'Bearer {onion_user.encode_jwt_token()}',
                                         {'private_message_id': message.id, 'reason': 'spam'})

    report = Report.query.one()
    assert report.source_instance_id == onion_row.id
    assert report.is_local()
    assert answer['private_message_report_view']['private_message']['ap_id'].endswith(f'/private_message/{message.id}')


def test_report_is_local_for_every_hosted_row(app, sisters):
    hell, onion, onion_row, peer, *_ = sisters
    assert Report(source_instance_id=1).is_local()
    assert Report(source_instance_id=onion_row.id).is_local()
    assert not Report(source_instance_id=peer.id).is_local()
    assert not Report(source_instance_id=None).is_local()
    hosted = frozenset({1, onion_row.id})   # a list page reads the set once and hands it in
    assert Report(source_instance_id=onion_row.id).is_local(hosted)
    assert not Report(source_instance_id=peer.id).is_local(hosted)


# --- the microblogs community's server views -------------------------------------------------------------------------

def test_an_onion_post_in_the_microblogs_community_is_no_server_view(app, sisters, monkeypatch):
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    monkeypatch.setattr(live, 'is_local_microblogs', lambda community: True)
    monkeypatch.setattr(live, 'server_view_actor', lambda instance_id: f'view-{instance_id}')
    monkeypatch.setattr(live, 'server_view_instance', lambda host: host)
    onion_post = make_post(hell_community, onion_user, None)
    peer_post = make_post(hell_community, peer_user, f'https://{PEER}/p/1')

    assert post_community_link(onion_post) == hell_community.link()
    assert post_community_link(peer_post) == f'view-{peer.id}'
    assert f'instance:{onion_row.id}' not in live_feed_keys(onion_post, hell_community, backfill=False)
    assert f'instance:{peer.id}' in live_feed_keys(peer_post, hell_community, backfill=False)
    assert [host for host, _count in busiest_microblog_servers(hell_community.id)] == [PEER]


# --- a federating sister: its own sitemap, and its indexable pages ---------------------------------------------------

@pytest.fixture
def shop(app, twins, site):
    """`twins`' federating shop.example (tests/test_per_domain_ap_routes.py), with an indexable post on it and one
    on hell.cloud."""
    hell_bob, hell_art, onion_bob, onion_art, shop_bob, shop_art = twins
    site.private_instance = False
    for user in (hell_bob, shop_bob):
        user.indexable = user.searchable = True
    db.session.commit()
    return make_post(hell_art, hell_bob, None, title='Hell news'), make_post(shop_art, shop_bob, None, title='Shop news')


@pytest.mark.parametrize('host, listed', [(HELL, 0), (SHOP, 1)])
def test_the_sitemap_lists_the_request_domains_own_posts(app, shop, host, listed):
    body = ap_get(app, '/sitemap.xml', host=host, headers={}).get_data(as_text=True)
    assert [f'/post/{post.id}</loc>' in body for post in shop] == [index == listed for index in range(2)]


@pytest.mark.parametrize('host', [HELL, SHOP])
def test_a_hosted_profile_carries_its_schema_org_metadata(app, shop, host):
    body = ap_get(app, '/u/bob', host=host, headers={'Accept': 'text/html'}).get_data(as_text=True)
    assert 'application/ld+json' in body


def test_a_hosted_post_carries_its_schema_org_metadata(app, shop):
    shop_post = shop[1]
    body = ap_get(app, f'/post/{shop_post.id}', host=SHOP, headers={'Accept': 'text/html'}).get_data(as_text=True)
    assert 'application/ld+json' in body


def test_a_local_only_profile_carries_no_schema_org_metadata(app, two_domains, site):
    hell, onion = two_domains.domains
    user = two_domains.local_user('hidden', onion)
    user.indexable = user.searchable = True
    user.private_key = 'a key'   # an approved account, signed in: a logged-out visitor is not shown the page at all
    site.private_instance = False
    db.session.commit()
    body = Visitor(app, user, ONION).get('/u/hidden', headers={'Accept': 'text/html'}).get_data(as_text=True)
    assert 'hidden' in body
    assert 'application/ld+json' not in body


def test_moving_post_images_to_s3_takes_every_hosted_domains_first(app, sisters, s3_bucket, monkeypatch):
    """`flask move-post-images-to-s3` moves local images before remote ones; an onion post's image is local."""
    hell, onion, onion_row, peer, hell_user, onion_user, peer_user, hell_community = sisters
    moved = []
    monkeypatch.setattr(cli, 'move_file_to_s3', lambda file_id, s3: moved.append(file_id))
    for key, value in (('S3_REGION', 'us-east-1'), ('S3_ENDPOINT', None)):   # the s3_bucket fixture's moto S3
        monkeypatch.setitem(app.config, key, value)
    images = {}
    for name, author in (('peer', peer_user), ('hell', hell_user), ('onion', onion_user)):
        image = File(file_path=f'app/static/media/posts/{name}.jpg')
        db.session.add(image)
        db.session.commit()
        make_post(hell_community, author, None).image_id = images.setdefault(name, image.id)
    db.session.commit()

    cli.register(app)   # pyfedi.py registers the commands; the test app has none
    result = app.test_cli_runner().invoke(args=['move-post-images-to-s3'])

    assert result.exit_code == 0, result.output
    assert moved == [images['onion'], images['hell'], images['peer']]   # newest first within each kind
