"""Plain community names: the web view without FediVerse instance details.

Spec: docs/superpowers/specs/2026-11-19-plain-community-names-design.md

A user who has not enabled "Enable FediVerse instance details" (User.show_instance_details, off by default, and off
for signed-out visitors) sees every community by its name alone. A bare name resolves local first, then a community
the reader joined, then any other community of that name. Only the HTML a browser is served changes: the API,
ActivityPub, stored text (notifications, emails) and every action URL (join, submit, moderate) keep the exact
community.
"""
import re

from flask import before_render_template, g, has_request_context, template_rendered
from flask_login import current_user
from markupsafe import escape
from sqlalchemy import desc, func
from sqlalchemy.exc import SQLAlchemyError

from app import utils   # its names are read at call time: it imports this module too
from app.local_names import local_clause, named_by_actor
from app.local_only import hidden_here
from app.models import Community, CommunityMember

# Templates whose output leaves the web view: an email goes to a recipient, not the reader of the page that sent it.
_NOT_WEB_VIEW_TEMPLATES = ('email/',)
# How many same-named remote communities `resolve_community_name` looks through for one the reader may see.
_ANY_CANDIDATES = 20
# How many names and handles mentions in one request may look up beyond those already looked up: a comment full of
# distinct `!names` must not cost a query each. Past it a handle links to the lookup page and a bare name stays text.
MENTION_LOOKUPS = 100


def instance_details_shown() -> bool:
    """Whether the reader of this request sees instance details. Outside a request (a task, the CLI) they are shown,
    so nothing stored or sent is changed by a reader's preference."""
    if not has_request_context():
        return True
    return bool(current_user.is_authenticated and current_user.show_instance_details)


# Set while a web-view template renders for a reader without instance details, and read by
# Community.display_name(), which cannot import this module (it imports app.models).
HIDE_DOMAINS_FLAG = '_plain_hide_domains'


def _render_started(sender, template, context, **extra) -> None:
    _count(template, +1)


def _render_finished(sender, template, context, **extra) -> None:
    _count(template, -1)


def _count(template, step: int) -> None:
    counter = '_plain_render_paused' if (template.name or '').startswith(_NOT_WEB_VIEW_TEMPLATES) \
        else '_plain_render_depth'
    setattr(g, counter, max(g.get(counter, 0) + step, 0))
    hide = bool(g.get('_plain_render_depth')) and not g.get('_plain_render_paused')
    if hide:
        try:
            hide = not instance_details_shown()
        except SQLAlchemyError:   # an error page after a failed transaction still renders, in today's view
            hide = False
    setattr(g, HIDE_DOMAINS_FLAG, hide)


def plain_rendering() -> bool:
    """Whether a web-view template is rendering for a reader who does not see instance details: the only time
    Community.display_name() leaves the domain off."""
    return bool(g.get(HIDE_DOMAINS_FLAG)) if has_request_context() else False


def install(app) -> None:
    """Track template rendering, and give templates the helpers below."""
    before_render_template.connect(_render_started, app)
    template_rendered.connect(_render_finished, app)
    app.jinja_env.globals['instance_details_shown'] = instance_details_shown
    app.jinja_env.globals['community_href'] = community_href
    app.jinja_env.globals['theme_community'] = theme_community
    app.jinja_env.globals['plain_communities'] = plain_communities
    app.jinja_env.globals['community_label'] = community_label
    shown_post_community_link = app.jinja_env.globals['post_community_link']
    app.jinja_env.globals['post_community_link'] = \
        lambda post: shown_post_community_link(post) if instance_details_shown() else community_href(post.community)


def resolve_community_name(name: str):
    """The community a bare `name` means to this request's reader: the local one, else one they joined (the most
    subscribed here first), else the community of that name they may see with the most subscribers here, the oldest
    on a tie. None when there is none. Memoized for the request."""
    if not name or '@' in name:
        return None
    key = name.strip().lower()
    memo = g.setdefault('_plain_resolved', {}) if has_request_context() else {}
    if key in memo:
        return memo[key]
    community = _resolve(key)
    memo[key] = community
    return community


def _resolve(name: str):
    local = Community.query.filter(local_clause(Community, name), Community.banned.is_(False)).first()
    if local is not None and not hidden_here(local):
        return local
    remote = Community.query.filter(func.lower(Community.name) == name, Community.ap_id.isnot(None),
                                    Community.banned.is_(False))
    # subscribers()'s count: local subscribers only (never a remote-reported total), then the oldest
    by_popularity = (desc(func.coalesce(Community.subscriptions_count, 0)), Community.id)
    if current_user.is_authenticated:
        joined = remote.join(CommunityMember, CommunityMember.community_id == Community.id).filter(
            CommunityMember.user_id == current_user.id, CommunityMember.is_banned.is_(False))
        for community in joined.order_by(*by_popularity).limit(_ANY_CANDIDATES):
            if _may_see(community):
                return community
    for community in remote.filter(Community.private.isnot(True)).order_by(*by_popularity).limit(_ANY_CANDIDATES):
        if _may_see(community):
            return community
    return None


def _may_see(community) -> bool:
    if hidden_here(community):
        return False
    if current_user.is_authenticated and community.instance_id in \
            set(utils.banned_instances(current_user.id)) | set(utils.blocked_instances(current_user.id)):
        return False
    return True


def community_href(community) -> str:
    """The path segment after /c/ that a navigational link to `community` uses: its bare name for a reader without
    instance details (resolved by name when followed), else its exact link(). Never use it for an action URL."""
    # A server view is not named by a name; a local community is already bare on its own domain, and needs its
    # domain on another of ours, where the bare name is that domain's own community.
    if instance_details_shown() or not isinstance(community, Community) or community.ap_id is None:
        return community.link()
    return community.name


def community_label(community) -> str:
    """A community's title with its domain (`title@domain`, as pages have always named it), or the title alone for a
    reader without instance details."""
    if instance_details_shown() or not isinstance(community, Community):
        return f'{community.title}@{community.ap_domain}'
    return community.title


def theme_community(community):
    """The community whose theme a page about `community` is shown in: the one its name resolves to for a reader
    without instance details, else the community itself."""
    if not community or instance_details_shown():
        return community
    return resolve_community_name(community.name) or community


def subscribers(community) -> int:
    """The subscriber count a same-named community is ranked by: its subscribers here, which this instance counted
    itself. Never the total a remote server reports, which a squatter could inflate to take over a bare name."""
    return community.subscriptions_count or 0


def _rank(is_local: bool, is_member: bool, subscriber_count: int, community_id: int) -> tuple:
    """`_resolve`'s order, for a group already in hand: local first, then one the reader joined, then the most
    subscribed here, then the oldest."""
    return not is_local, not is_member, -(subscriber_count or 0), community_id


def _member_ids() -> set:
    if not current_user.is_authenticated:
        return set()
    return set(utils.joined_or_modding_communities(current_user.id))   # ids


def name_key(text: str) -> str:
    """A community name or title as a reader tells them apart: any case, with `_`, `-` and runs of white space all
    one space. "World News", "world_news" and "WORLD  news" are one name."""
    return ' '.join(_SEPARATORS_RE.sub(' ', text or '').casefold().split())


_SEPARATORS_RE = re.compile(r'[_\-\s]+')


def _duplicate_groups(entries) -> dict:
    """{id: group id} for `entries` of (id, name, title): two are one group when their titles read the same
    (`name_key`), which shows a reader the same text, or their names do, which leads them to the same /c/<name>.
    Transitively, so an entry matching two others joins them."""
    parent = {}

    def root(node):
        while parent.setdefault(node, node) != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for community_id, name, title in entries:
        node = ('id', community_id)
        for key in _keys(name, title):
            parent[root(key)] = root(node)
    return {community_id: root(('id', community_id)) for community_id, _name, _title in entries}


def plain_communities(*sections) -> list:
    """The lists of communities in `sections` with each name once across all of them, for a reader without instance
    details; unchanged otherwise. Communities are one name when their titles or their names read the same
    (`_duplicate_groups`); the one `_resolve`'s order ranks first stands for them all, at the first position any of
    them held. One list in, one list out; several in, a list of lists out."""
    sections = [list(section or []) for section in sections]
    if not instance_details_shown():
        members = _member_ids()
        everyone = {community.id: community for section in sections for community in section}
        groups = _duplicate_groups([(c.id, c.name, c.title) for c in everyone.values()])
        best = {}
        for community in everyone.values():
            rank = _rank(community.ap_id is None, community.id in members, subscribers(community), community.id)
            group = groups[community.id]
            if group not in best or rank < best[group][0]:
                best[group] = (rank, community)
        seen = set()
        plain = []
        for section in sections:
            kept = []
            for community in section:
                group = groups[community.id]
                if group not in seen:
                    seen.add(group)
                    winner = best[group][1]
                    winner.is_duplicate = False
                    kept.append(winner)
            plain.append(kept)
        sections = plain
    return sections[0] if len(sections) == 1 else sections


def plain_choices(which_community: dict, facts: dict, others: str = 'Others') -> dict:
    """possible_communities()'s `{section: [(id, label)]}` with each name once across all sections, for a reader
    without instance details, labelled by title alone; unchanged otherwise. `facts` maps each id to (name, title,
    is_local, subscriber_count).

    The reader's own sections (every one but `others`) group as in plain_communities, among themselves only. The
    `others` section holds every other community, whose names and titles anyone can choose: they are grouped among
    themselves, and one that reads as a name or title the reader's own sections hold is left out, as their own
    community stands for it. So a stranger's community never replaces, hides or joins together the reader's own."""
    if instance_details_shown():
        return which_community
    members = _member_ids()
    own_ids = {community_id for section, choices in which_community.items() if section != others
               for community_id, _label in choices}
    own_keys = {key for community_id in own_ids for key in _keys(*facts[community_id][:2])}
    strangers = [community_id for community_id, _label in which_community.get(others, [])
                 if community_id not in own_ids and not own_keys & set(_keys(*facts[community_id][:2]))]
    winners = {}
    for population in (own_ids, strangers):
        groups = _duplicate_groups([(community_id, *facts[community_id][:2]) for community_id in population])
        best = {}
        for community_id in population:
            _name, _title, is_local, subscriber_count = facts[community_id]
            rank = _rank(is_local, community_id in members, subscriber_count, community_id)
            group = groups[community_id]
            if group not in best or rank < best[group][0]:
                best[group] = (rank, community_id)
        winners.update({community_id: best[groups[community_id]][1] for community_id in population})
    seen = set()
    plain = {}
    for section, choices in which_community.items():
        kept = []
        for community_id, _label in choices:
            winner = winners.get(community_id)
            if winner is not None and winner not in seen:
                seen.add(winner)
                kept.append((winner, facts[winner][1]))
        if kept:
            plain[section] = kept
    return plain


def _keys(name: str, title: str) -> tuple:
    """The keys `_duplicate_groups` groups by."""
    return ('title', name_key(title)), ('name', name_key(name))


# A link a peer made for a community mention: its text is `!name@domain`.
_MENTION_LINK_RE = re.compile(r'(<a\b[^>]*>)\s*!([a-zA-Z0-9_.-]+)@([a-zA-Z0-9_.-]+)\s*</a>')
_HREF_RE = re.compile(r'''\bhref\s*=\s*(["'])[^"']*\1''')
# A mention in text: `!name@domain`, or a bare `!name` (a sentence's closing dot is not part of the name).
_TEXT_MENTION_RE = re.compile(r'(?<![\w/<!@&])!([a-zA-Z0-9_]+(?:[.-][a-zA-Z0-9_]+)*)(@[a-zA-Z0-9_.-]+\b)?')


def plain_community_mentions(html: str) -> str:
    """Community mentions in rendered `html` for a reader without instance details. A handle, `!name@domain` as text
    or a peer's link, is shown as `!name` but still links to the community it names, never to another of that name:
    /c/<handle> for a community we know, else the lookup (text) or the peer's own href (link). A bare `!name` that
    resolves links to /c/name, as the name is resolved there. Code is left alone."""
    if not html:
        return html
    code_placeholder = '\x00' + utils.gibberish(10)
    link_placeholder = '\x00' + utils.gibberish(10)
    tag_placeholder = '\x00' + utils.gibberish(10)

    code_snippets, html = utils.stash_code_html(html, code_placeholder, utils._ANY_CODE_HTML_RE)
    html = _MENTION_LINK_RE.sub(_link_mention, html)
    link_snippets, html = utils.stash_link_html(html, link_placeholder, utils._ANY_LINK_HTML_RE)
    tag_snippets, html = utils.stash_code_html(html, tag_placeholder, utils._ANY_TAG_HTML_RE)

    html = _TEXT_MENTION_RE.sub(_text_mention, html)

    html = utils.pop_code(code_snippets=tag_snippets, text=html, placeholder=tag_placeholder)
    html = utils.pop_link(link_snippets=link_snippets, text=html, placeholder=link_placeholder)
    return utils.pop_code(code_snippets=code_snippets, text=html, placeholder=code_placeholder)


def _text_mention(match) -> str:
    name, domain = match.group(1), match.group(2)
    if domain is not None:
        known = _known_handle(name, domain[1:])
        href = f'/c/{known}' if known else f'/community/lookup/{name}/{domain[1:]}'
        return f'<a href="{escape(href)}">{escape("!" + name)}</a>'
    if not _mention_lookup_allowed(name.lower()) or resolve_community_name(name) is None:
        return match.group(0)   # an exclamation, not a mention (or past the lookup budget)
    return f'<a href="/c/{escape(name.lower())}">{escape("!" + name)}</a>'


def _link_mention(match) -> str:
    opening, name, host = match.group(1), match.group(2), match.group(3)
    known = _known_handle(name, host)
    if known:
        opening = _HREF_RE.sub(lambda _m: f'href="/c/{escape(known)}"', opening, count=1)
    return f'{opening}{escape("!" + name)}</a>'


def _known_handle(name: str, host: str) -> str | None:
    """The /c/ path of the community `name@host` names exactly (a hosted domain's own, or the remote one), else
    None. Never resolved by name alone: a handle means that community. Memoized for the request, and None without a
    query once the request's mention lookups are spent."""
    handle = f'{name}@{host}'.lower()
    memo = g.setdefault('_plain_handles', {})
    if handle not in memo:
        if not _mention_lookup_allowed(handle):
            return None
        community = Community.query.filter(named_by_actor(Community, handle), Community.banned.is_(False)).first()
        memo[handle] = None if community is None or hidden_here(community) else community.link()
    return memo[handle]


def _mention_lookup_allowed(key: str) -> bool:
    """Whether a mention may look up `key` (a bare name or a handle): always if it was looked up already this request,
    else while MENTION_LOOKUPS lasts."""
    if key in g.get('_plain_resolved', {}) or key in g.get('_plain_handles', {}):
        return True
    spent = g.get('_plain_mention_lookups', 0)
    if spent >= MENTION_LOOKUPS:
        return False
    g._plain_mention_lookups = spent + 1
    return True


def community_links(html: str, server_name_override: str | None = None) -> str:
    """The `community_links` template filter: today's lookup links for a reader with instance details, plain
    mentions otherwise."""
    if instance_details_shown():
        return utils.community_link_to_href(html, server_name_override)
    return plain_community_mentions(html)
