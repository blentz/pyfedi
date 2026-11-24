"""ATProto names, with nothing imported, so any module may use them (app.local_names does): handles and DIDs."""
import re

HANDLE = re.compile(r'([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]([a-z0-9-]{0,61}[a-z0-9])?')   # used with fullmatch
DID = re.compile(r'did:(plc:[a-z2-7]{24}|web:[a-z0-9.:-]{1,253})')   # used with fullmatch
DOMAIN = 'bsky.app'   # every ATProto actor's Instance, and where its profile and posts are seen


def looks_atproto(name) -> str | None:
    """`name` as an ATProto handle or DID (`@alice.bsky.social`, `did:plc:...`), or None when it is neither, such as
    an ActivityPub `user@server` or a local name."""
    text = (name or '').strip().lstrip('@').lower() if isinstance(name, str) else ''
    if '@' in text or '/' in text:
        return None
    if DID.fullmatch(text) or HANDLE.fullmatch(text):
        return text
    return None
