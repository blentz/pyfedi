"""OAuth 2 scopes for client apps (plan W0.2): which exist, and which include which. No imports from the app, so
app.models can use it."""

# Mastodon's scopes, without admin (PieFed has no admin API for them). `push` is accepted for the apps that ask for
# it at registration (Tusky asks for `read write follow push`) and grants nothing yet.
_READ = ('accounts', 'blocks', 'bookmarks', 'favourites', 'filters', 'follows', 'lists', 'mutes', 'notifications',
         'search', 'statuses')
_WRITE = ('accounts', 'blocks', 'bookmarks', 'conversations', 'favourites', 'filters', 'follows', 'lists', 'media',
          'mutes', 'notifications', 'reports', 'statuses')
SCOPES = frozenset(('read', 'write', 'follow', 'push', 'profile')
                   + tuple(f'read:{name}' for name in _READ) + tuple(f'write:{name}' for name in _WRITE))


def parse_scope(scope: str | None) -> str:
    """The space-separated scopes, normalised; none means `read`, as Mastodon's default. ValueError for one PieFed
    does not know."""
    names = (scope or '').split()
    if not names:
        return 'read'
    unknown = [name for name in names if name not in SCOPES]
    if unknown:
        raise ValueError(f'unknown scope: {unknown[0]}')
    return ' '.join(names)


# What `follow` (Mastodon's old scope) stood for
_FOLLOW = frozenset(('read:follows', 'write:follows', 'read:blocks', 'write:blocks', 'read:mutes', 'write:mutes'))


def covers(granted: str, scope: str) -> bool:
    """Whether the scopes `granted` include `scope`: itself, `read` or `write` for one of its parts, or `follow` for
    the follow, block and mute parts."""
    names = set(granted.split())
    if scope in names:
        return True
    if ':' in scope and scope.split(':')[0] in names:
        return True
    return 'follow' in names and scope in _FOLLOW
