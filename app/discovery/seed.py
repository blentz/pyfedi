"""Local validation data for discovery and Castopod (interop D24). Reads app/discovery/fixtures only and
fetches nothing: the directory rows go through the same normalisers and cleaning as a real refresh, and
the podcast through the same actor and post ingest as federation. Credits are stored in the shape the
live fetch leaves: one credit is verified (a seeded remote account whose profile links the podcast), the
rest are names only."""
import json
from pathlib import Path

from app import db
from app.activitypub.util import actor_json_to_model, create_post
from app.discovery import castopod, mastodon, peertube, pixelfed
from app.discovery.credits import credit_vouches, parse_feed_credits, store_credits
from app.discovery.filters import host_is_excluded
from app.discovery.podcast import podcast_community_for
from app.discovery.refresh import clean_entries, upsert_entries
from app.models import Instance, Post, User, UserExtraField, utcnow

FIXTURES = Path(__file__).resolve().parent / 'fixtures'
EPISODE_URL = 'https://pod.example/@mypodcast/episodes/ep-1'
EPISODE_NOTE_ID = 'https://pod.example/@mypodcast/posts/1'
PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
SEEDED_HOST_PROFILE = 'https://social.example/@ann'   # the feed href that stands for the seeded fixture account
SEEDED_HOST_NAME = 'ann'
SEEDED_HOST_DOMAIN = 'people.example'
SEEDED_HOST_ACTOR = f'https://{SEEDED_HOST_DOMAIN}/users/{SEEDED_HOST_NAME}'


def _json(fixtures: Path, name: str):
    return json.loads((fixtures / name).read_text())


def fixture_entries(fixtures: Path = FIXTURES) -> list[dict]:
    entries = [peertube.channel_to_entry(c) for c in _json(fixtures, 'sepiasearch.json')['data']]
    entries += [mastodon.account_to_entry(a, 'mastodon.example') for a in _json(fixtures, 'mastodon_directory.json')]
    entries += [pixelfed.profile_to_entry(p, 'pixelfed.example')
                for p in _json(fixtures, 'pixelfed_directory.json')['data']]
    podcastindex = _json(fixtures, 'castopod_podcastindex.json')
    feeds = {feed['id']: feed for listed in podcastindex['lists'].values() for feed in listed['feeds']}
    for feed in feeds.values():
        episodes = podcastindex['episodes'].get(str(feed['id']))
        actor_url = castopod.actor_url_from_social_interact(episodes['items'], feed['url']) if episodes else None
        if actor_url:
            entries.append(castopod.podcast_to_entry(feed, actor_url))
    return [entry for entry in entries if entry]


def _instance(domain: str, software: str) -> Instance:
    instance = db.session.query(Instance).filter_by(domain=domain).first()
    if instance is None:   # created first, so find_instance_id does not fetch the peer's nodeinfo
        instance = Instance(domain=domain, software=software)
        db.session.add(instance)
        db.session.commit()
    return instance


def _seeded_host(podcast) -> User:
    """A remote fixture account whose profile field links the podcast, so a credit naming it vouches back. It is never
    a local login account: the seed finds only its own row, by actor id, and reads or changes no other user."""
    user = db.session.query(User).filter_by(ap_profile_id=SEEDED_HOST_ACTOR).first()
    if user is None:
        instance = _instance(SEEDED_HOST_DOMAIN, 'mastodon')
        user = User(user_name=SEEDED_HOST_NAME, title='Ann Host', verified=True, banned=False,
                    instance_id=instance.id, ap_id=f'{SEEDED_HOST_NAME}@{SEEDED_HOST_DOMAIN}',
                    ap_domain=SEEDED_HOST_DOMAIN, ap_profile_id=SEEDED_HOST_ACTOR,
                    ap_public_url=f'https://{SEEDED_HOST_DOMAIN}/@{SEEDED_HOST_NAME}',
                    ap_inbox_url=f'{SEEDED_HOST_ACTOR}/inbox')
        db.session.add(user)
        db.session.flush()
        db.session.add(UserExtraField(user_id=user.id, label='Podcast', text=podcast.ap_profile_id))
        db.session.commit()
    return user


def _seeded_credits(credits: list, podcast) -> list:
    """The credits as the live fetch stores them, without its network: the one that names the seeded remote
    account is verified when that account vouches back; every other is a plain name."""
    host = _seeded_host(podcast)
    for credit in credits:
        verified = credit['profile_url'] == SEEDED_HOST_PROFILE and credit_vouches(host.id, podcast)
        credit['user_id'] = host.id if verified else None
        credit['profile_url'] = host.public_url() if verified else None
        if verified:
            credit['verified'] = True
    return credits


def seed_podcast(fixtures: Path = FIXTURES):
    existing = Post.get_by_ap_id(EPISODE_NOTE_ID)
    if existing is not None:
        return existing
    _instance('pod.example', 'castopod')
    podcast = actor_json_to_model(_json(fixtures, 'castopod_actor.json'), 'mypodcast', 'pod.example')
    community = podcast_community_for(podcast)
    if community is None:
        return None
    # The content does not open with the episode link, so no episode audio fetch is started
    activity = {'id': f'{EPISODE_NOTE_ID}/activity', 'type': 'Create', 'to': [PUBLIC], 'cc': [],
                'object': {'id': EPISODE_NOTE_ID, 'type': 'Note', 'attributedTo': podcast.ap_profile_id,
                           'to': [PUBLIC], 'cc': [],
                           'content': '<p>Episode 1 is out: a conversation with Cara Guest.</p>'}}
    post = create_post(False, community, activity, podcast)
    if post is not None:
        credits = parse_feed_credits((fixtures / 'castopod_feed.xml').read_bytes(), EPISODE_URL)
        store_credits(post, _seeded_credits(credits, podcast))
    return post


def seed_from_fixtures(fixtures: Path = FIXTURES) -> dict:
    entries = clean_entries(fixture_entries(fixtures), lambda host: host_is_excluded(host, frozenset()))
    count = upsert_entries(entries, utcnow())
    post = seed_podcast(fixtures)
    return {'entries': count, 'podcast_post': post.id if post is not None else None}
