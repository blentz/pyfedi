"""OAuth 2 scopes for client apps (plan W0.2): which exist, and which include which. No imports from the app, so
app.models can use it."""

# Mastodon's scopes. `push` lets an app subscribe to Web Push notifications (app.mastodon.push). The admin scopes
# (owner ruling 2026-11-09) are granted only to a person holding the role an admin maps them to, and reach no further
# than that role's permissions (app.oauth_server.admin_scopes).
_READ = ('accounts', 'blocks', 'bookmarks', 'favourites', 'filters', 'follows', 'lists', 'mutes', 'notifications',
         'search', 'statuses')
_WRITE = ('accounts', 'blocks', 'bookmarks', 'conversations', 'favourites', 'filters', 'follows', 'lists', 'media',
          'mutes', 'notifications', 'reports', 'statuses')
_ADMIN = ('accounts', 'reports', 'domain_allows', 'domain_blocks', 'ip_blocks', 'email_domain_blocks',
          'canonical_email_blocks')
ADMIN_SCOPES = frozenset(('admin:read', 'admin:write') + tuple(f'admin:read:{name}' for name in _ADMIN)
                         + tuple(f'admin:write:{name}' for name in _ADMIN))
# Pixelfed's, which its app (2.0.1) asks for its account-security screens (two-factor, password, sign-in activity, apps):
# known, so it registers, and covering nothing, as PieFed serves those screens to no app (plan W1.4a)
PIXELFED_SECURITY_SCOPES = frozenset(('security:read', 'security:write'))
# gpodder.net sync's (plan W4, ruling G1): an app password's only scope, outside SCOPES, so no OAuth client may ask for
# it and no other API's route covers it (app.gpodder.auth)
GPODDER_SCOPE = 'gpodder'
SCOPES = PIXELFED_SECURITY_SCOPES | frozenset(('read', 'write', 'follow', 'push', 'profile')
                   + tuple(f'read:{name}' for name in _READ) + tuple(f'write:{name}' for name in _WRITE)) | ADMIN_SCOPES


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


def is_admin(scope: str) -> bool:
    return scope in ADMIN_SCOPES


def covers(granted: str, scope: str) -> bool:
    """Whether the scopes `granted` include `scope`: itself, a scope it is a part of (`read` for `read:accounts`,
    `admin:read` for `admin:read:reports`), or `follow` for the follow, block and mute parts. `read` and `write` never
    reach an admin scope, as Mastodon's do not."""
    names = set(granted.split())
    if scope in names:
        return True
    if any(scope.startswith(name + ':') for name in names):
        return True
    return 'follow' in names and scope in _FOLLOW
