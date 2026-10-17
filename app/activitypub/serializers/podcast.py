"""The audio a local podcast episode carries in its Page (spec 2026-10-14 §10 A16 option 1, A17.1-A17.2). The Page
stays a Page for every peer; only its attachment changes, from a bare Link to a Document Mastodon plays and Lemmy
parses (Lemmy 0.19's Attachment has no Audio variant, and one unknown entry rejects the whole Page)."""
from app.constants import COMMUNITY_KIND_PODCAST, POST_TYPE_LINK
from app.models import media_of
from app.utils import is_valid_xml_utf8

NAME_MAX = 1500   # Mastodon's MediaAttachment::MAX_DESCRIPTION_LENGTH


def _positive_int(value) -> bool:
    return type(value) is int and value > 0   # not a bool or a float: the feed's length and duration are whole


def uses_podcast_episode(post) -> bool:
    """A ready audio upload in a local podcast community (A17.1). A remote-audio link has no media record, so it is
    not one. A mime or uuid that is not XML text is no episode either: the feed writes both into XML, and lxml raises
    on a control character, which would fail the whole feed."""
    community = post.community
    media = media_of(post)
    return bool(community.kind == COMMUNITY_KIND_PODCAST and community.is_local()
                and post.type == POST_TYPE_LINK
                and media is not None and media.get('status') == 'ready'
                and str(media.get('mime', '')).startswith('audio/') and is_valid_xml_utf8(str(media['mime']))
                and _positive_int(media.get('bytes')) and _positive_int(media.get('duration_s'))
                and isinstance(media.get('uuid'), str) and media['uuid'] != '' and is_valid_xml_utf8(media['uuid']))


def episode_attachment(post) -> dict:
    """The one attachment of an episode's Page, pushed and fetched alike (A17.2)."""
    document = {'type': 'Document', 'mediaType': media_of(post)['mime'], 'url': post.url,
                'name': (post.title or '')[:NAME_MAX]}
    if post.image is not None and post.image.source_url:   # cover art: Mastodon's audio player artwork
        document['icon'] = {'type': 'Image', 'url': post.image.source_url}
    return document
