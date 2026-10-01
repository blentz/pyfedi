"""A ratchet over routes that accept GET and change state without a form.

`app/utils.py`'s `login_required` validates the CSRF token **only for POST**:

    if request.method == 'POST' and csrf:
        validate_csrf(...)

So a route that accepts GET and mutates unconditionally has no CSRF protection
at all on that path -- loading it in an `<img>` tag is enough. The campaign has
now found that shape three times by hand, each in a different blueprint:

* D955 `community_unban_user` -- a moderator who loaded the link unbanned that
  user;
* D976 `community_moderate_report_ignore` -- one slice later, in the same
  blueprint, discarding a report;
* D987 `post_instance_sticky` -- an ADMIN action, stickying a post across the
  whole instance.

Three by hand in three blueprints means the next one is found the same way, or
not at all. This test enumerates them instead.

**WHAT THE FROZEN SET MEANS.** `KNOWN_GET_MUTATORS` is the inventory of routes
that accept GET and reach a `db.session` write with no `validate_on_submit()`
gating them. It is **not** a list of routes that are safe: most entries are
fine -- a listing page that updates a counter, an OAuth callback that must be a
GET because the provider redirects there, an unsubscribe link from an email
that carries its own token. The set exists so that

* a NEW route of this shape fails the test and has to be justified, and
* a route that is FIXED fails the test too, and has to be removed from the set,

which is the same "floors only rise" discipline as `coverage_floors.ini`. It
cannot claim the listed routes are correct, only that the list has been looked
at. D973 is the finding about a ratchet that claimed more than it checked.
"""
import ast
import pathlib

import pytest

APP_ROOT = pathlib.Path(__file__).resolve().parent.parent / 'app'

# Routes that accept GET and write to the database without a form. Grouped by
# why they are here; the grouping is the triage, not an assertion of safety.
KNOWN_GET_MUTATORS = {
    # Read paths that also write -- view counters, last-seen stamps, cached
    # aggregates. A forged GET achieves nothing the visitor could not do by
    # following the link.
    'admin.admin_activities', 'admin.admin_home',
    'chat.chat_conversation', 'instance.list_instances',
    'main.about_page', 'main.bot_challenge_result', 'main.find_voters',
    'main.index_rss', 'main.modlog', 'main.random', 'main.random_nsfw',
    'main.test', 'tag.show_tag', 'tag.tag_cloud', 'tag.tag_posts',
    'topic.show_topic', 'topic.show_topic_rss', 'user.user_hidden_posts',

    # GET by protocol: an external service redirects the browser here, so the
    # method is not ours to choose. Each carries its own single-use token.
    'auth.verify_email', 'auth.google_connect_callback',
    'auth.mastodon_connect_callback', 'auth.discord_connect_callback',
    'user.connect_oauth', 'user.user_newsletter_unsubscribe',
    'user.user_email_notifs_unsubscribe',

    # A form does gate the write, but it is reached through a helper this
    # detector cannot follow -- a nested call, or a second form object.
    'admin.admin_federation_preload', 'admin.admin_federation_remote_scan',
    'admin.admin_permissions', 'dev.tools', 'post.add_reply_inline',
    'search.run_search', 'user.notifications', 'user.user_files',

    # NOT YET FIXED. These are the ones that match D955's shape and change
    # something at the caller's direction. Each belongs to a blueprint this
    # campaign has not finished; the entry is removed when its slice lands.
    'community.community_wiki_revert_revision',
    'topic.topic_notification',
    'user.notification_goto', 'user.notification_delete',
    # Found once helpers in app/shared/ were derived rather than listed: the
    # bell on a profile toggles a subscription to that user through
    # `subscribe_user` on a GET. Not yet ruled on.
    'user.user_notification',
}

MUTATIONS = ('db.session.add(', 'db.session.delete(', 'db.session.commit()',
             'db.session.execute')

# Writes that reach the database through a shared helper instead of through
# `db.session` in the view itself. Without these the detector is blind to an
# entire class of this defect, and that blindness was not theoretical: D1018
# (`community_add_moderator`) and D1021 (`post_sticky`, `post_vote`) all
# accepted GET and all mutated, and all four passed this test, because the
# write happens one call deeper. Each name here is a function in
# `app/shared/` that commits.
MUTATING_HELPERS = (
    'add_mod_to_community(', 'remove_mod_from_community(', 'do_subscribe(',
    'community_ban_user(', 'unsubscribe_from_everything_then_delete(',
    'make_post(', 'make_reply(', 'edit_post(', 'delete_post(', 'restore_post(',
    'vote_for_post(', 'vote_for_reply(', 'bookmark_post(', 'subscribe_post(',
    'subscribe_reply(', 'sticky_post(', 'lock_post(', 'mark_post_read(',
    'block_another_user(', 'toggle_post_notification(', 'report_post(',
    'purge_user_then_delete(',
)

SHARED_ROOT = APP_ROOT / 'shared'


def _calls_by_name(node):
    return {call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}


def _shared_mutating_helpers():
    """Every function in `app/shared/` that writes, directly or through
    another `app/shared/` function, found rather than listed.

    `MUTATING_HELPERS` above is a hand-kept list, and a hand-kept list misses
    whatever nobody added: `feed.subscribe` mutated on a bare GET through
    `join_feed`, which was never on it, so the ratchet passed it. Deriving
    the set closes that class; following calls only within `app/shared/`
    keeps it to the helpers the list was always meant to hold (following
    every function in `app/` matches by bare name and flags most read paths).
    """
    functions = {}
    for path in SHARED_ROOT.rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                functions.setdefault(node.name, []).append(node)
    mutating = {name for name, nodes in functions.items()
                if any(marker in ast.unparse(node) for node in nodes for marker in MUTATIONS)}
    calls = {name: set().union(*(_calls_by_name(node) for node in nodes))
             for name, nodes in functions.items()}
    grew = True
    while grew:
        grew = False
        for name, called in calls.items():
            if name not in mutating and called & mutating:
                mutating.add(name)
                grew = True
    return mutating


def _blueprint_name(path):
    """`app/community/routes.py` -> `community`; `app/main/routes.py` -> `main`."""
    return path.parent.name


def _get_mutating_routes():
    found = set()
    shared_helpers = _shared_mutating_helpers()
    for path in sorted(APP_ROOT.rglob('routes.py')):
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef):
                continue
            methods = None
            for decorator in node.decorator_list:
                if not isinstance(decorator, ast.Call):
                    continue
                if not ast.unparse(decorator.func).endswith('bp.route'):
                    continue
                methods = ['GET']
                for keyword in decorator.keywords:
                    if keyword.arg == 'methods':
                        methods = [element.value for element in keyword.value.elts]
            if methods is None or 'GET' not in methods:
                continue
            body = ast.unparse(node)
            if not any(marker in body for marker in MUTATIONS + MUTATING_HELPERS) and \
                    not _calls_by_name(node) & shared_helpers:
                continue
            if 'validate_on_submit' in body:
                continue
            found.add(f'{_blueprint_name(path)}.{node.name}')
    return found


def test_no_new_route_mutates_on_a_bare_get():
    """The ratchet. A new route of this shape has to be justified by adding it
    to `KNOWN_GET_MUTATORS` with a reason, and a fixed one has to be removed."""
    found = _get_mutating_routes()

    new = sorted(found - KNOWN_GET_MUTATORS)
    assert new == [], (
        'these routes accept GET and write to the database with no form '
        'gating them, and login_required validates CSRF only for POST -- so '
        'a forged GET reaches the write:\n  ' + '\n  '.join(new) +
        '\nEither make the route POST-only (see D955, D976, D987 and the '
        '`send_post` pattern in app/static/js/scripts.js) or add it to '
        'KNOWN_GET_MUTATORS with a comment saying why GET is correct.')

    fixed = sorted(KNOWN_GET_MUTATORS - found)
    assert fixed == [], (
        'these are listed in KNOWN_GET_MUTATORS but no longer match the '
        'shape, so the list is stale -- remove them:\n  ' + '\n  '.join(fixed))


def test_the_routes_this_campaign_fixed_are_not_in_the_set():
    """D955, D976, D987, D1018, D1021, D1041 and D1044 were each this shape and
    each is now POST-only.
    Naming them here is what stops a later change quietly reintroducing one --
    the ratchet above would accept it again as a new entry, but this row will
    not."""
    found = _get_mutating_routes()

    for endpoint in ('community.community_unban_user',
                     'community.community_moderate_report_ignore',
                     'post.post_instance_sticky',
                     # D1018 and D1021, found only after MUTATING_HELPERS was
                     # added: promoting a moderator, stickying a post in a
                     # community, and casting a vote all mutated on a bare GET.
                     'community.community_add_moderator',
                     'post.post_sticky', 'post.post_vote',
                     # D1041 and D1044, the last of D988's eleven in this
                     # blueprint: removing your own avatar or banner, and
                     # marking every notification read.
                     'user.remove_avatar', 'user.remove_cover',
                     'user.notifications_all_read',
                     # Owner ruling: joining a feed, missed by the hand-kept
                     # MUTATING_HELPERS because join_feed was not on it.
                     'feed.subscribe'):
        assert endpoint not in found, (
            f'{endpoint} mutates on a GET again; it was fixed once already')


@pytest.mark.parametrize('endpoint', sorted(KNOWN_GET_MUTATORS))
def test_every_listed_route_still_exists(app, endpoint):
    """A frozen set of endpoint names goes stale silently when a route is
    renamed or deleted, and a stale entry makes the ratchet above weaker
    without saying so."""
    assert endpoint in app.view_functions, (
        f'{endpoint} is in KNOWN_GET_MUTATORS but is not a registered route')
