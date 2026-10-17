"""Property checks for the podcast surface (tier T9): the Document attachment a local podcast episode's Page
carries, and the fields its feed item adds.

Both read a post's media record and its `extensions['podcast']` episode fields, which are stored JSON, so any value
can sit in either. uses_podcast_episode decides whether a post is an episode; one it rejects is an ordinary link
Page, which is fine. For one it accepts, no input may raise, the attachment must keep what Mastodon needs (an
audio Document at the post's URL, a name within its description limit), and the feed fields must be what feedgen
takes -- the item target hands them to feedgen through RSSFeed._add_episode, the writer the feed uses, so a value feedgen refuses (an
itunes_duration ValueError, say) is found.

No database and no network. The post is built unsaved and never added to a session; both targets need an app for
SERVER_URL.
"""

from feedgen.feed import FeedGenerator

from flask import current_app

from app.activitypub.serializers.podcast import NAME_MAX, episode_attachment, uses_podcast_episode
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import Community, File, Post, User, utcnow
from app.rss_extras import PodcastIndexEntryExtension, PodcastIndexExtension, RSSFeed, episode_item_fields
from tests.fuzz.targets._target import Target
from tests.fuzz.targets.signatures import _fields, _json_or_text, _text


def _record(fuzzed) -> dict:
    """A ready audio record, with the fuzzed keys merged over it; a non-dict fuzz leaves it whole."""
    record = {'status': 'ready', 'uuid': '2f3c8f0e-6f8d-4b5e-9c1a-0d6a5e1b7f42', 'mime': 'audio/mpeg', 'bytes': 12741,
              'duration_s': 10, 'codec': 'mp3'}
    if isinstance(fuzzed, dict):
        record.update(fuzzed)
    return record


def _episode_post(record, podcast, title: str, image_url: str, flags: int) -> Post:
    server = current_app.config['SERVER_URL']
    community = Community(name='show', kind=COMMUNITY_KIND_PODCAST, ap_followers_url=f'{server}/c/show/followers')
    post = Post(id=1, title=title, type=POST_TYPE_LINK, url=f'{server}/static/media/posts/ab/cd/e.mp3',
                posted_at=utcnow(), nsfw=bool(flags & 1), nsfl=bool(flags & 2),
                extensions={'media': record, 'podcast': podcast})
    post.community, post.author = community, User(user_name='alice')
    if flags & 4:
        post.image = File(source_url=image_url, width=640, height=640)
    return post


def check_podcast_episode_attachment(data: bytes) -> None:
    """Fields: the media record (JSON, merged over a ready record), the title, the cover art URL. For an episode,
    the attachment is an audio Document at the post's URL whose name fits Mastodon's description limit."""
    record_bytes, title_bytes, image_bytes = _fields(data, 3)
    post = _episode_post(_record(_json_or_text(record_bytes)), {}, _text(title_bytes), _text(image_bytes),
                         len(record_bytes))
    if not uses_podcast_episode(post):
        return
    document = episode_attachment(post)
    assert isinstance(document, dict) and document['type'] == 'Document', f'attachment {document!r}'
    assert str(document['mediaType']).startswith('audio/'), f'mediaType {document["mediaType"]!r}'
    assert document['url'] == post.url
    assert len(document['name']) <= NAME_MAX, f'name of {len(document["name"])} characters'


def check_podcast_feed_item(data: bytes) -> None:
    """Fields: the media record, the episode fields (`extensions['podcast']`, any JSON value), the title, the cover
    art URL. For an episode, the fields have the shapes the feed relies on, and feedgen takes them and renders."""
    record_bytes, podcast_bytes, title_bytes, image_bytes = _fields(data, 4)
    post = _episode_post(_record(_json_or_text(record_bytes)), _json_or_text(podcast_bytes), _text(title_bytes),
                         _text(image_bytes), len(record_bytes) + len(podcast_bytes))
    if not uses_podcast_episode(post):
        return
    fields = episode_item_fields(post)
    assert fields['enclosure'][1].isdecimal() and fields['enclosure'][1].isascii(), f'length {fields["enclosure"][1]!r}'
    assert fields['explicit'] in ('yes', 'no'), f'explicit {fields["explicit"]!r}'
    for key in ('episode_number', 'season'):
        assert key not in fields or (type(fields[key]) is int and fields[key] >= 1), f'{key} {fields.get(key)!r}'
    feed = FeedGenerator()
    feed.load_extension('podcast', rss=True)
    feed.register_extension('podcastindex', PodcastIndexExtension, PodcastIndexEntryExtension)
    feed.title('show')
    feed.link(href='https://fuzz.example/', rel='alternate')
    feed.description('show')
    entry = feed.add_entry()
    entry.title('episode')
    RSSFeed._add_episode(entry, fields)
    feed.rss_str()


# None is budgeted: both are linear in the input. Both need an app (SERVER_URL).
TARGETS = [
    Target(name='podcast_episode_attachment', check=check_podcast_episode_attachment, surface='podcasts',
           budget_ms=None, needs_app=True),
    Target(name='podcast_feed_item', check=check_podcast_feed_item, surface='podcasts', budget_ms=None,
           needs_app=True),
]
