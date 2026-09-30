"""The flash messages that carry markup: `community_link_markup` and its four
call sites.

D1376. Four places built the same link by hand and handed it to `Markup`:

    flash(Markup(_('You joined %(community_name)s',
                   community_name=f'<a href="/c/{community.link()}">'
                                  f'{community.display_name()}</a>')))

`Markup` marks a string safe and Jinja renders a safe string verbatim, so
`{{ message }}` in base.html put both interpolated values straight into the DOM.
Neither is safe to put there. `display_name()` is `title` for a local community
and `title@ap_domain` for a remote one; `link()` is `name` or `ap_id`. For a
remote community every one of those comes from the peer's actor document, which
restricts no characters -- `actor_name_from_ap` strips a name and cuts it to its
column and does not filter it, because an actor name is not a slug (fact 797).

Measured. A community whose peer set `"name": "</a><img src=x onerror=alert(1)>"`,
joined by a local user, produced this as the rendered flash:

    You joined <a href="/c/memes@peer.example"></a><img src=x
    onerror=alert(1)>@peer.example</a>

`RAW_IMG_IN_DOM=True`. The local path measured the same way, through a title a
local admin sets.

REACHABILITY. `/c/<actor>/subscribe` is a **GET** route ("POST is used by htmx,
GET when JS is disabled"), so following one link joins the community and renders
the message. The victim needs no form and no token.

THE FIX IS A HELPER, NOT FOUR EDITS. `Markup.format` escapes its arguments, and
one function means the four sites cannot be half-fixed -- which is how D1375's
third instance survived two earlier rounds at the same shape.

WHY THE MARKUP STAYS. The message is meant to contain a link: it is what takes
the user to the community they just joined. Escaping the whole message would
show them raw `<a href=...>` text. The values are escaped, the structure is not.
"""
import pytest
from flask import g, render_template_string, session
from flask_wtf.csrf import generate_csrf
from unittest.mock import patch

from app import db
from app.models import Site
from app.utils import community_link_markup
from tests.factories import make_community, make_instance, make_user

PAYLOADS = [
    '</a><img src=x onerror=alert(1)>',
    '<script>alert(1)</script>',
    '" onmouseover="alert(1)',
    "' onmouseover='alert(1)",
    '<b>bold</b>',
]


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    db.session.commit()
    return site


def rendered(message):
    """What base.html's `{{ message }}` produces. A `Markup` is rendered
    verbatim, which is the whole point -- so this is the DOM the user gets."""
    return render_template_string('{{ m }}', m=message)


# --------------------------------------------------------------------------
# The helper
# --------------------------------------------------------------------------


class TestCommunityLinkMarkup:
    @pytest.mark.parametrize('payload', PAYLOADS)
    def test_a_remote_title_cannot_leave_the_element(self, app, env, payload):
        instance = make_instance('peer.flash.test')
        community = make_community(name='memes', host='peer.flash.test')
        community.title = payload
        community.ap_domain = 'peer.flash.test'
        community.ap_id = 'memes@peer.flash.test'
        community.instance_id = instance.id
        db.session.commit()

        html = rendered(community_link_markup(community))

        assert '<img' not in html
        assert '<script' not in html
        assert '<b>' not in html
        # One anchor, opened and closed: the payload closing it early is the
        # measured defect, so counting is the assertion.
        assert html.count('<a ') == 1 and html.count('</a>') == 1

    @pytest.mark.parametrize('payload', PAYLOADS)
    def test_a_remote_name_in_the_href_cannot_leave_the_attribute(self, app, env,
                                                                  payload):
        """`link()` is `ap_id.lower()` for a remote community, and `ap_id` is
        built from the peer's `preferredUsername`. It lands inside `href="..."`,
        so a `"` in it is its own escape."""
        instance = make_instance('peer.flash.test')
        community = make_community(name='memes', host='peer.flash.test')
        community.ap_id = f'{payload}@peer.flash.test'.lower()
        community.ap_domain = 'peer.flash.test'
        community.instance_id = instance.id
        db.session.commit()

        html = rendered(community_link_markup(community))

        assert html.count('"') == 2, html          # exactly the href's own pair
        assert '<img' not in html and '<script' not in html

    def test_a_local_title_is_escaped_too(self, app, env):
        """`display_name()` is the bare `title` for a local community. Set by an
        admin rather than a peer, so lower severity and the same treatment --
        the helper does not ask where the value came from."""
        community = make_community(name='localmemes')
        community.title = '</a><img src=x onerror=alert(1)>'
        db.session.commit()

        html = rendered(community_link_markup(community))

        assert '<img' not in html

    def test_an_ordinary_community_still_gets_a_working_link(self, app, env):
        """The message is meant to contain a link -- it is how the user reaches
        the community they just joined. Escaping the structure as well would show
        them raw `<a href=...>` text."""
        community = make_community(name='ordinarymemes')
        community.title = 'Ordinary Memes'
        db.session.commit()

        html = rendered(community_link_markup(community))

        assert html == '<a href="/c/ordinarymemes">Ordinary Memes</a>'

    def test_a_remote_community_is_linked_by_its_handle(self, app, env):
        instance = make_instance('peer.flash.test')
        community = make_community(name='memes', host='peer.flash.test')
        community.title = 'Memes'
        community.ap_id = 'memes@peer.flash.test'
        community.ap_domain = 'peer.flash.test'
        community.instance_id = instance.id
        db.session.commit()

        html = rendered(community_link_markup(community))

        assert html == ('<a href="/c/memes@peer.flash.test">'
                        'Memes@peer.flash.test</a>')

    def test_an_ampersand_is_escaped_without_being_doubled(self, app, env):
        """`Markup.format` escapes each argument exactly once. A title with `&`
        is the case where escaping twice would show `&amp;amp;` to the user."""
        community = make_community(name='ampmemes')
        community.title = 'Cats & Dogs'
        db.session.commit()

        assert rendered(community_link_markup(community)) == \
            '<a href="/c/ampmemes">Cats &amp; Dogs</a>'


# --------------------------------------------------------------------------
# The call sites
# --------------------------------------------------------------------------


def _payload_community(local=False):
    payload = '</a><img src=x onerror=alert(1)>'
    if local:
        community = make_community(name='localmemes')
        community.title = payload
        db.session.commit()
        return community
    instance = make_instance('peer.flash.test')
    community = make_community(name='memes', host='peer.flash.test')
    community.title = payload
    community.ap_domain = 'peer.flash.test'
    community.ap_id = 'memes@peer.flash.test'
    community.instance_id = instance.id
    db.session.commit()
    return community


def _flashes(app, user, call):
    """Run `call` in a request context with `user` signed in and return what
    reached `flash`, rendered as base.html would render it."""
    from flask_login import login_user

    with app.test_request_context('/'):
        login_user(user)
        with patch('app.community.routes.flash') as flash, \
                patch('app.community.routes.send_post_request'):
            call()
        return [rendered(c.args[0]) for c in flash.call_args_list]


class TestJoiningACommunity:
    def test_the_remote_join_message_carries_no_tag(self, app, env):
        """The measured path: `do_subscribe`'s remote arm, which is what a peer
        can reach -- it controls the title."""
        from app.community.routes import do_subscribe

        community = _payload_community()
        alice = make_user(make_instance('local.flash.test'), 'alice', local=True)
        db.session.commit()

        messages = _flashes(app, alice,
                            lambda: do_subscribe(community.ap_id, alice.id))

        assert messages, 'no flash at all -- the harness, not the fix'
        assert all('<img' not in m for m in messages), messages
        assert any('You joined' in m for m in messages), messages

    def test_the_local_join_message_carries_no_tag(self, app, env):
        from app.community.routes import do_subscribe

        community = _payload_community(local=True)
        alice = make_user(make_instance('local.flash.test'), 'alice2', local=True)
        db.session.commit()

        messages = _flashes(app, alice,
                            lambda: do_subscribe('localmemes', alice.id))

        assert messages, 'no flash at all -- the harness, not the fix'
        assert all('<img' not in m for m in messages), messages

    def test_the_escaped_form_is_what_the_user_sees(self, app, env):
        """Not merely "the tag is absent": the title is still shown, as text.
        Without this the fix could be "drop the community name" and pass."""
        from app.community.routes import do_subscribe

        community = _payload_community(local=True)
        alice = make_user(make_instance('local.flash.test'), 'alice3', local=True)
        db.session.commit()

        messages = _flashes(app, alice,
                            lambda: do_subscribe('localmemes', alice.id))

        assert any('&lt;img' in m for m in messages), messages


class TestTheOtherThreeCallSites:
    """One helper, four callers. Each is reached separately, because a fix
    applied to three of four is exactly what D1375's third instance was."""

    def _signed_in(self, app, user):
        client = app.test_client()
        with client.session_transaction() as sess:
            sess['_user_id'] = str(user.id)
            sess['_fresh'] = True
        return client

    def _token(self, app, client):
        """unsubscribe and join_then_add are POST-only since D994, and
        login_required validates the CSRF token on every POST."""
        with app.test_request_context():
            token = generate_csrf()
            raw = session['csrf_token']
        with client.session_transaction() as sess:
            sess['csrf_token'] = raw
        return token

    def test_leaving_a_community(self, app, env, api_baseline):
        """`unsubscribe`'s no-JS (non-HTMX) arm. Leaving renders the
        same link, and the community is the one being left -- so the peer's
        title reaches the DOM on the way out as well as the way in.

        `api_baseline.user2` rather than a fresh user: `make_community` sets
        `user_id=1`, so the first user a test mints is the community's OWNER,
        and `subscription != SUBSCRIPTION_OWNER` means an owner never reaches
        this flash.
        """
        from app.models import CommunityMember

        community = _payload_community(local=True)
        member = api_baseline.user2
        db.session.add(CommunityMember(user_id=member.id,
                                       community_id=community.id))
        db.session.commit()

        client = self._signed_in(app, member)
        response = client.post('/community/localmemes/unsubscribe',
                               data={'csrf_token': self._token(app, client)},
                               follow_redirects=True)

        assert response.status_code == 200
        assert b'You left' in response.data, 'the flash never fired'
        assert b'<img src=x' not in response.data
        assert b'&lt;img' in response.data

    def test_join_then_add(self, app, env, api_baseline):
        """`join_then_add` -- the "post to a community you are not in yet" path,
        which joins and then redirects to the compose form. It is
        `@validation_required` and `@approval_required`, so the user has to be
        one the instance has actually admitted."""
        community = _payload_community(local=True)
        joiner = api_baseline.user2
        # `validation_required` wants `verified`, and `approval_required` refuses
        # an account with no `private_key` while the instance is 'Closed' or
        # 'RequireApplication' -- a local account has one, and without these the
        # route answers 200 with 'Account under review' and no flash at all.
        joiner.verified = True
        joiner.private_key = 'a-local-key'
        db.session.commit()

        client = self._signed_in(app, joiner)

        # Not followed: this route redirects to the compose form, which cannot be
        # rendered under the test config (`csrf_token` is absent from the form).
        # The flash is read out of the session instead, which is where it waits
        # for the next request either way.
        response = client.post('/community/localmemes/join_then_add',
                               data={'csrf_token': self._token(app, client)})

        assert response.status_code == 302
        with client.session_transaction() as sess:
            messages = [str(m) for _, m in sess.get('_flashes', [])]
        assert messages, 'the flash never fired'
        assert any('You joined' in m for m in messages), messages
        assert all('<img src=x' not in m for m in messages), messages
        assert any('&lt;img' in m for m in messages), messages

    def test_the_join_task(self, app, env):
        """`join_community` in app/shared/tasks/follows.py, on its `SRC_WEB`
        arm. A fourth copy of the same construction, in a different module --
        the one a grep of app/community/routes.py alone would miss."""
        from app.constants import SRC_WEB
        from app.shared.tasks.follows import join_community

        community = _payload_community(local=True)
        alice = make_user(make_instance('local.flash.test'), 'alicetask',
                          local=True)
        db.session.commit()
        cid, uid = community.id, alice.id

        with app.test_request_context('/'):
            from flask_login import login_user
            login_user(db.session.get(type(alice), uid))
            with patch('app.shared.tasks.follows.flash') as flash:
                join_community(None, uid, cid, SRC_WEB)

        messages = [rendered(c.args[0]) for c in flash.call_args_list]
        assert messages, 'the flash never fired'
        assert all('<img src=x' not in m for m in messages), messages
        assert any('&lt;img' in m for m in messages), messages
