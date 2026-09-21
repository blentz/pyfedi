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
    'community.unsubscribe', 'community.join_then_add',
    'community.community_wiki_revert_revision',
    'feed.feed_notification', 'feed.feed_unsubscribe',
    'topic.topic_notification',
    'user.remove_avatar', 'user.remove_cover',
    'user.notification_goto', 'user.notification_delete',
    'user.notifications_all_read',
}

MUTATIONS = ('db.session.add(', 'db.session.delete(', 'db.session.commit()',
             'db.session.execute')


def _blueprint_name(path):
    """`app/community/routes.py` -> `community`; `app/main/routes.py` -> `main`."""
    return path.parent.name


def _get_mutating_routes():
    found = set()
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
            if not any(marker in body for marker in MUTATIONS):
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


def test_the_three_routes_this_campaign_fixed_are_not_in_the_set():
    """D955, D976 and D987 were each this shape and each is now POST-only.
    Naming them here is what stops a later change quietly reintroducing one --
    the ratchet above would accept it again as a new entry, but this row will
    not."""
    found = _get_mutating_routes()

    for endpoint in ('community.community_unban_user',
                     'community.community_moderate_report_ignore',
                     'post.post_instance_sticky'):
        assert endpoint not in found, (
            f'{endpoint} mutates on a GET again; it was fixed once already')


@pytest.mark.parametrize('endpoint', sorted(KNOWN_GET_MUTATORS))
def test_every_listed_route_still_exists(app, endpoint):
    """A frozen set of endpoint names goes stale silently when a route is
    renamed or deleted, and a stale entry makes the ratchet above weaker
    without saying so."""
    assert endpoint in app.view_functions, (
        f'{endpoint} is in KNOWN_GET_MUTATORS but is not a registered route')
