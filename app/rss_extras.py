from dataclasses import dataclass
from datetime import timezone
from urllib.parse import urlsplit

from feedgen.feed import FeedGenerator
from app.models import media_of
from app.shared.media import episode_of
from app.utils import has_unsafe_url_scheme, feed_entry_body, mimetype_from_url, is_video_hosting_site, is_valid_xml_utf8 as _is_valid_xml_utf8
# could be used later for instance checks: from app.models import User, Community, Post, PostReply

####################################################################################################
# this is intended to be upstreamed
from feedgen.ext.base import BaseExtension
from feedgen.util import xml_elem

SLASH_NS = 'http://purl.org/rss/1.0/modules/slash/'

def _set_value(entry, name, value):
    if value:
        newelem = xml_elem('{%s}' % SLASH_NS + name, entry)
        newelem.text = value

class SlashExtension(BaseExtension):
    def extend_ns(self):
        return {'slash': SLASH_NS}

class SlashEntryExtension(BaseExtension):
    def __init__(self):
        self._comments = None

    def extend_atom(self, entry):  # TODO
        return self.extend_rss(entry)

    def extend_rss(self, entry):
        if self._comments:
            _set_value(entry, 'comments', self._comments)
        return entry

    def comments(self, value):
        if type(value) is not int or value < 0:
            raise ValueError('Slash Extension: Invalid comment count')
        self._comments = str(value)

PODCAST_NS = 'https://podcastindex.org/namespace/1.0'


class PodcastIndexExtension(BaseExtension):
    """The Podcasting 2.0 tags feedgen has no extension for (spec §4.5): channel <podcast:locked>, item <podcast:guid>."""
    def __init__(self):
        self._locked = None

    def extend_ns(self):
        return {'podcast': PODCAST_NS}

    def extend_rss(self, rss_feed):
        if self._locked is not None:
            xml_elem('{%s}locked' % PODCAST_NS, rss_feed[0]).text = self._locked
        return rss_feed

    def locked(self, value: bool):
        self._locked = 'yes' if value else 'no'


class PodcastIndexEntryExtension(BaseExtension):
    def __init__(self):
        self._guid = None

    def extend_rss(self, entry):
        if self._guid:
            xml_elem('{%s}guid' % PODCAST_NS, entry).text = self._guid
        return entry

    def guid(self, value: str):
        self._guid = value
####################################################################################################


def itunes_image_ok(url) -> bool:
    """feedgen's itunes:image takes only a URL ending exactly in .jpg or .png (a channel's silently drops any other,
    an item's raises), so those are the only images this feed offers it. A control character is refused too: lxml
    raises on one when the feed is rendered, which would fail the whole feed for one image."""
    return (isinstance(url, str) and not has_unsafe_url_scheme(url) and url.endswith(('.jpg', '.png'))
            and _is_valid_xml_utf8(url))


@dataclass
class PodcastChannel:
    """A podcast community's show fields for its feed (spec §3.3, §10 A17.6)."""
    category: str | None
    image: str
    author: str
    explicit: bool


def episode_item_fields(post) -> dict:
    """What an episode's feed item adds (A17.6), from its media record and episode fields."""
    media = media_of(post)
    episode = episode_of(post)
    fields = {'enclosure': (post.url, str(media['bytes']), media['mime']), 'duration': media['duration_s'],
              'explicit': 'yes' if episode.get('explicit') or post.nsfw or post.nsfl else 'no',
              'guid': media['uuid']}
    for key in ('episode_number', 'season'):
        if type(episode.get(key)) is int and episode[key] >= 1:   # a stored value is trusted only in this shape
            fields[key] = episode[key]
    if post.image is not None and itunes_image_ok(post.image.source_url):
        fields['image'] = post.image.source_url
    return fields


@dataclass
class RSSFeed:
    """ A strange mixin of a collection of RSS channel fields, dealing with the Post class/data model,
    and of course proxying FeedGenerator – but it seems to be manageable.
    Can be easily adapted to handle comments (PostReply class) should those be syndicated some day.
    """
    title: str
    link: str
    description: str
    logo: str|None = None
    self_link: str|None = None
    language: str|None = None
    podcast: PodcastChannel|None = None

    def create_feed(self, posts, server_url):
        fg = FeedGenerator()
        fg.load_extension('media', rss=True)
        fg.register_extension('slash', SlashExtension, SlashEntryExtension)

        fg.title(self.title)
        fg.link(href=self.link, rel='alternate')
        fg.subtitle(self.description)
        if self.logo:
            fg.logo(self.logo)
        if self.self_link:
            fg.link(href=self.self_link, rel='self')
        if self.language:
            fg.language(self.language)
        if self.podcast:
            fg.load_extension('podcast', rss=True)
            fg.register_extension('podcastindex', PodcastIndexExtension, PodcastIndexEntryExtension)
            if self.podcast.category:
                fg.podcast.itunes_category({'cat': self.podcast.category})
            fg.podcast.itunes_image(self.podcast.image)
            fg.podcast.itunes_author(self.podcast.author)
            fg.podcast.itunes_explicit('yes' if self.podcast.explicit else 'no')
            fg.podcastindex.locked(False)

        # the reversed below is so that newer entries will be first in the feed wrt document order,
        # which depending on the RSS client can be quite useful.
        # However, this also depends on ordering done by the caller,
        # and ultimately on FeedGenerator itself, so it's a heuristic only.
        for post in reversed(posts):
           self._add_post(post, fg, server_url, podcast=self.podcast is not None)

        return fg.rss_str()

    @classmethod
    def _add_post(cls, post, feed, server_url, podcast=False):
         # Validate title and body - skip this post if invalid
        # TODO can also be fixed with RegExes, feedgen could take care of it?
        # @see https://www.w3.org/TR/REC-xml/#charsets
        if not _is_valid_xml_utf8(post.title.strip()):
            return
        body = feed_entry_body(post)
        content = body.strip() if body else None
        if content and not _is_valid_xml_utf8(content):
            return

        fe = feed.add_entry()
        fe.title(post.title.strip())
        link = f"{server_url}{post.slug}" if post.slug else f"{server_url}/post/{post.id}"
        fe.link(href=link)
        fe.guid(post.profile_id(), permalink=True)

        fe.content(content, type='CDATA')  # feedgen takes care if this is empty
        medium = None if post.content_warning else cls._media_content(post.url, post.image)  # a warning hides the image too
        if medium:
            fe.media.content(medium)

        if post.author:
            # @see post.author.email
            fe.author(email=cls._email_from_public_url(post.author.public_url()))

        fe.pubDate(post.created_at.replace(tzinfo=timezone.utc))
        # reply_count_cross_posted is nullable and can drift negative, so fall back and clamp
        comment_count = post.reply_count_cross_posted
        if comment_count is None:
            comment_count = post.reply_count
        fe.slash.comments(max(0, comment_count or 0))

        if post.community:
            fe.category(term=post.community.name)
        for cat in set([flair.flair for flair in post.flair] + [tag.display_as for tag in post.tags]):
            if cat:
                fe.category(term=cat, scheme='flair/tag')

        if podcast:
            cls._add_episode(fe, episode_item_fields(post))

    @staticmethod
    def _add_episode(entry, fields):
        """What an episode's fields add to one feed entry (A17.6). The feed and its fuzz target both write them here."""
        entry.enclosure(*fields['enclosure'])
        entry.podcast.itunes_duration(fields['duration'])
        entry.podcast.itunes_explicit(fields['explicit'])
        if 'episode_number' in fields:
            entry.podcast.itunes_episode(fields['episode_number'])
        if 'season' in fields:
            entry.podcast.itunes_season(fields['season'])
        if 'image' in fields:
            entry.podcast.itunes_image(fields['image'])
        entry.podcastindex.guid(fields['guid'])

    @staticmethod
    def _email_from_public_url(url):
        comps = urlsplit(url)
        user = comps.path.replace('/u/', '', 1)
        return f"{user}@{comps.netloc}"

    @staticmethod
    def _media_content(url, image):
        """ Return a dictionary of fields for media:content, or None when that's not possible.

        For better user experience, we attach post.url even when its type is unknown.
        Since MIME type determination is tricky (*mimetype_from_url* uses the path suffix only,
        *mime_type_using_head* depends on the server and calling it on every feed generation
        introduces overhead),
        we use media:content instead of RSS'es enclosure since it mandates only a URL.

        We make best effort guesses to fill in other fields.

        :url    string, or None
        :image  File (from post.image), or None
        :return dict or None. The dictionary has fields intended for feedgen's media extension
        """
        if not url:
            return None
        medium = {'url': url}

        type = mimetype_from_url(url)
        if not type and is_video_hosting_site(url):
            type = "text/html"
        if type:
            medium['type'] = type

        if image and url == image.source_url:
            if not type:  # may be None, e.g. for lemmy's image_proxy URLs
                medium['medium'] = 'Image'
            size = image.filesize()
            if size > 0:
                medium['fileSize'] = str(size)

        return medium
