"""Mastodon's entities, built from PieFed's rows (spec 2026-11-07-mastodon-client-api-design §3.2). The fields every
app decodes are always there; Tusky's decoder, the strictest, fails a whole page on one that is missing (plan W1.0)."""
from app.models import User
from app.reads.users import listable_counts
from app.storefront import current_storefront, domain_base_url

BLANK_IMAGE = '/static/images/1px.gif'   # what an account without an avatar or header shows: nothing, not our logo


def absolute(url: str | None, fallback: str = BLANK_IMAGE) -> str:
    """`url` as an absolute URL on this domain when it is a path of ours, `fallback`'s when there is none."""
    url = url or fallback
    return domain_base_url(current_storefront()) + url if url.startswith('/') else url


def timestamp(moment) -> str:
    """RFC 3339 in UTC with its zone, as every app parses it (Tusky refuses one without)."""
    return moment.strftime('%Y-%m-%dT%H:%M:%S.000Z')


def acct(user: User) -> str:
    """The name an app shows and looks up: bare for an account of the domain asked, name@host for any other."""
    if user.is_local() and user.home_domain() == current_storefront():
        return user.user_name
    return user.lemmy_link()


def _fields(user: User) -> list[dict]:
    return [{'name': field.label, 'value': field.text, 'verified_at': None} for field in user.extra_fields]


def account(user: User, viewer_id: int | None, with_source: bool = False) -> dict:
    """Mastodon's Account for `user`, as the viewer `viewer_id` (None: nobody signed in) is told it; with
    `with_source`, the CredentialAccount the person gets of themselves."""
    posts, comments = listable_counts([user], viewer_id)[user.id]
    avatar, header = absolute(user.avatar_image()), absolute(user.cover_image())
    entity = {
        'id': str(user.id), 'username': user.user_name, 'acct': acct(user), 'display_name': user.display_name(),
        'locked': bool(user.ap_manually_approves_followers), 'bot': bool(user.bot or user.bot_override),
        'discoverable': bool(user.searchable), 'indexable': bool(user.indexable), 'group': False,
        'created_at': timestamp(user.created), 'note': user.about_html or '', 'url': user.public_url(),
        'uri': user.profile_id(), 'avatar': avatar, 'avatar_static': avatar, 'header': header,
        'header_static': header, 'followers_count': user.num_followers or 0,
        'following_count': user.num_following or 0, 'statuses_count': (posts or 0) + (comments or 0),
        'last_status_at': None, 'hide_collections': False, 'noindex': not user.indexable, 'emojis': [],
        'roles': [], 'fields': _fields(user),
    }
    if with_source:
        entity['source'] = {'privacy': 'public', 'sensitive': False, 'language': None, 'note': user.about or '',
                            'fields': _fields(user), 'follow_requests_count': 0,
                            'hide_collections': False, 'discoverable': bool(user.searchable),
                            'indexable': bool(user.indexable)}
    return entity
