"""`main.add_post`: the "Create post" button outside any community.

It used to pick a community for the user -- the `cross_post_community_id` cookie's, else the first joined one -- and
redirect to its compose form. That form's post-type buttons are built from the community it opened on (Poll and Event
are left out for a Lemmy community, Audio shows only for a podcast), so the user posted with the wrong community's
choices. The route now shows the main menu's list of communities (favorites, moderating, joined) with its search box,
none preselected (owner ruling 2026-10-17), plus links to find or create another; `?community=<id>` sends the user on
to that community's form, keeping `&type=` when the compose form's own picker comes back through here.

A HARNESS NOTE. `client.set_cookie(key, value)` defaults the domain to `localhost`, while this app's `SERVER_NAME` is
`test.piefed.local`, so a cookie set without `domain=HOST` is never sent.
"""
import re
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.activitypub.util import find_microblogging_community
from app.models import Community, CommunityFavorite, CommunityMember, Language, Post, Role, Site, User
from tests.factories import (make_community, make_community_member,
                             make_instance, make_user)

pytestmark = pytest.mark.usefixtures('site')
HOST = 'test.piefed.local'


@pytest.fixture
def env(app, db_session):
    """Alice has joined `general`; `elsewhere` exists and she has not."""
    site = db.session.get(Site, 1)
    site.private_instance = False
    site.community_creation_admin_only = False
    db.session.commit()
    local = make_instance(HOST)
    make_user(local, 'founder', local=True)          # id 1, the admin trap
    alice = make_user(local, 'alice', local=True)
    alice.verified = True
    joined = make_community('general')
    elsewhere = make_community('elsewhere')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(alice, joined)
    g.admin_ids = []
    client = app.test_client()
    with client.session_transaction() as sess:
        sess['_user_id'] = str(alice.id)
        sess['_fresh'] = True
    return client, joined, elsewhere


def picks(html):
    """The community ids the picker links to, in page order."""
    return [int(found) for found in re.findall(r'href="/add_post\?community=(\d+)', html)]


def sections(html):
    return re.findall(r'data-community-section="(\w+)"', html)


class TestTheChooser:
    def test_the_button_shows_the_joined_communities_instead_of_opening_one(self, app, env):
        client, joined, elsewhere = env

        response = client.get('/add_post')

        assert response.status_code == 200
        assert picks(response.get_data(as_text=True)) == [joined.id]

    def test_the_communities_are_grouped_like_the_main_menu(self, app, env):
        client, joined, elsewhere = env
        alice = User.query.filter_by(user_name='alice').one()
        modded = make_community('modded')
        make_community_member(alice, modded, is_moderator=True)
        db.session.add(CommunityFavorite(user_id=alice.id, community_id=joined.id))
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        assert sections(html) == ['favorites', 'moderating', 'joined']
        assert picks(html) == [joined.id, modded.id, joined.id]

    def test_it_has_the_main_menus_search_box(self, app, env):
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'id="community-picker-filter"' in html
        assert f'data-community-name="{joined.title.lower()}"' in html

    def test_no_community_is_preselected(self, app, env):
        """Owner ruling: the user picks every time, so a stale guess cannot send a post to the wrong place."""
        client, joined, elsewhere = env
        client.set_cookie('cross_post_community_id', str(joined.id), domain=HOST)

        html = client.get('/add_post').get_data(as_text=True)

        assert 'dropdown-item active' not in html

    def test_it_links_to_finding_a_community_that_is_not_listed(self, app, env):
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/communities"' in html

    def test_it_offers_to_create_a_community(self, app, env):
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/community/add_local"' in html

    def test_it_does_not_offer_creation_when_only_admins_may_create(self, app, env):
        client, joined, elsewhere = env
        db.session.get(Site, 1).community_creation_admin_only = True
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/community/add_local"' not in html

    def test_an_admin_is_offered_creation_when_only_admins_may_create(self, app, env):
        client, joined, elsewhere = env
        db.session.get(Site, 1).community_creation_admin_only = True
        db.session.commit()
        User.query.filter_by(user_name='alice').one().roles.append(Role(name='Admin', built_in=True))
        db.session.commit()

        html = client.get('/add_post').get_data(as_text=True)

        assert 'href="/community/add_local"' in html

    def test_a_user_who_has_joined_nothing_is_offered_finding_or_creating_one(self, app, env):
        """This was a bare 204, which a browser shows as the button doing nothing."""
        client, joined, elsewhere = env
        CommunityMember.query.delete()
        db.session.commit()

        response = client.get('/add_post')

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert picks(html) == [] and 'id="community_picker"' not in html
        assert 'href="/communities"' in html and 'href="/community/add_local"' in html


class TestChoosingACommunity:
    def test_a_chosen_community_opens_its_compose_form(self, app, env):
        client, joined, elsewhere = env

        response = client.get(f'/add_post?community={elsewhere.id}')

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    @pytest.mark.parametrize('kind', ['link', 'discussion', 'image', 'video', 'audio', 'poll', 'event'])
    def test_the_type_the_compose_form_was_on_is_kept(self, app, env, kind):
        """The compose form's community select sends its current type, so switching community keeps it."""
        client, joined, elsewhere = env

        response = client.get(f'/add_post?community={elsewhere.id}&type={kind}')

        assert response.status_code == 302
        assert response.headers['Location'] == f'/community/elsewhere/submit/{kind}'

    def test_a_type_the_compose_form_does_not_have_is_dropped(self, app, env):
        client, joined, elsewhere = env

        response = client.get(f'/add_post?community={elsewhere.id}&type=../../admin')

        assert response.status_code == 302
        assert response.headers['Location'] == '/community/elsewhere/submit'

    @pytest.mark.parametrize('value', ['', 'abc', '1.5', '-1', '0', '999999', '٢', ' '])
    def test_a_value_naming_no_community_shows_the_chooser_again(self, app, env, value):
        """`'٢'` is an Arabic-Indic digit: str.isdigit() accepts it, so it pins that no int() parse can raise."""
        client, joined, elsewhere = env

        response = client.get('/add_post', query_string={'community': value})

        assert response.status_code == 200
        assert picks(response.get_data(as_text=True)) == [joined.id]

    def test_a_private_community_the_user_is_not_in_is_not_a_destination(self, app, env):
        """possible_communities() leaves it out of the chooser, so the redirect must not reach it by id either."""
        client, joined, elsewhere = env
        secret = make_community('secret')
        secret.private = True
        db.session.commit()

        response = client.get(f'/add_post?community={secret.id}')

        assert response.status_code == 200
        assert secret.id not in picks(response.get_data(as_text=True))


class TestWhoMayUseIt:
    def test_an_anonymous_visitor_is_sent_to_the_login(self, app, env):
        """`@login_required` -- the route reads `possible_communities()`, which needs a user."""
        response = app.test_client().get('/add_post')

        assert response.status_code in (302, 401)
        if response.status_code == 302:
            assert '/auth/login' in response.headers['Location']


@pytest.fixture
def poster(app, env):
    """Alice, able to post (validation_required, approval_required and can_create_post want a key), who has joined
    the local microblogs community, so the picker would otherwise list it."""
    client, joined, elsewhere = env
    alice = User.query.filter_by(user_name='alice').one()
    alice.private_key = 'a private key'
    db.session.commit()
    microblogs = find_microblogging_community()
    make_community_member(alice, microblogs)
    return client, joined, microblogs


def csrf(app, client):
    """The app's login_required checks a POST's CSRF token itself, even with WTF_CSRF_ENABLED off."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def quick_post(app, client, body):
    with patch('app.shared.post.notify_about_post'):
        return client.post('/add_post/quick', data={'body': body, 'csrf_token': csrf(app, client)})


class TestQuickPost:
    """A quick post box for the local microblogs community sits above the chooser: a 250-character box, a Post
    button and a link to that community's full compose form. Microblogs leaves the picker, since the box stands
    for it."""

    def test_the_box_is_above_the_chooser_and_microblogs_leaves_it(self, app, poster):
        client, joined, microblogs = poster

        html = client.get('/add_post').get_data(as_text=True)

        assert html.index('id="quick_post"') < html.index('Choose the community to post in.')
        assert 'action="/add_post/quick"' in html
        assert 'maxlength="250"' in html
        assert html.index('value="Post"') < html.index('Additional Microblogging Options')
        assert 'href="/community/microblogs/submit">Additional Microblogging Options</a>' in html
        assert picks(html) == [joined.id]

    def test_a_user_who_may_not_post_there_gets_no_box(self, app, env):
        """`env`'s alice has no key, so can_create_post refuses her: no box."""
        client, joined, elsewhere = env

        html = client.get('/add_post').get_data(as_text=True)

        assert 'id="quick_post"' not in html

    def test_a_quick_post_is_a_microblog_post_as_a_mastodon_one_is(self, app, poster):
        """Stored as an incoming Note is: microblog, titled from its text ('[Microblog] ' when that is short)."""
        client, joined, microblogs = poster
        alice = User.query.filter_by(user_name='alice').one()

        response = quick_post(app, client, 'Hello fediverse.\n\nA second paragraph.')

        post = Post.query.filter_by(user_id=alice.id).one()
        assert response.status_code == 302 and response.headers['Location'].endswith(post.slug)
        assert post.community_id == microblogs.id
        assert post.microblog is True and post.is_microblog()
        assert post.title == '[Microblog] Hello fediverse'   # the incoming Note's rule drops the full stop
        assert 'A second paragraph.' in post.body

    def test_a_longer_first_sentence_is_the_title_alone(self, app, poster):
        client, joined, microblogs = poster

        quick_post(app, client, 'This first sentence is long enough to stand alone. Then more.')

        assert Post.query.one().title == 'This first sentence is long enough to stand alone'

    def test_its_note_is_its_text_alone(self, app, poster):
        """A Mastodon post has no title, so the derived one is not sent."""
        from app.activitypub.serializers.microblog import note_content
        client, joined, microblogs = poster

        quick_post(app, client, 'Hi')

        content = note_content(Post.query.one())
        assert '[Microblog]' not in content and 'Hi' in content

    def test_a_before_post_create_plugin_sees_and_can_rewrite_it(self, app, poster):
        """The quick post fires the hook community.add_post and the API fire, so a content-filter plugin is not
        bypassed by posting through the box."""
        client, joined, microblogs = poster
        seen = []

        def rewrite(hook_name, data=None, **kwargs):
            if hook_name != 'before_post_create':
                return data
            seen.append(data)
            return {**data, 'content': 'filtered text'}

        with patch('app.plugins.fire_hook', side_effect=rewrite):
            quick_post(app, client, 'original text')

        assert seen and seen[0]['content'] == 'original text' and seen[0]['community_id'] == microblogs.id
        assert Post.query.one().body == 'filtered text'

    @pytest.mark.parametrize('body', ['   ', 'x' * 251])
    def test_an_empty_or_overlong_post_is_refused_on_the_chooser(self, app, poster, body):
        client, joined, microblogs = poster

        response = quick_post(app, client, body)

        assert response.status_code == 200
        assert 'id="quick_post"' in response.get_data(as_text=True)
        assert Post.query.count() == 0

    def test_a_user_who_may_not_post_there_is_refused(self, app, env):
        """`env`'s alice may not post in microblogs (no key): the box is absent, and posting to it anyway is refused."""
        client, joined, elsewhere = env

        response = quick_post(app, client, 'hello')

        assert response.status_code in (302, 403) and '/post/' not in response.headers.get('Location', '')
        assert Post.query.count() == 0


class TestTheMicroblogsComposeForm:
    """`/community/microblogs/submit`, the "Additional Microblogging Options": the title is optional there, and a
    post without one is a microblog post, as the quick post is."""

    def payload(self, app, client, microblogs, **overrides):
        english = Language.query.filter_by(code='en').first()
        if english is None:   # languages_for_form offers no 'und'
            english = Language(code='en', name='English')
            db.session.add(english)
            db.session.commit()
        data = {'title': '', 'body': 'Composed in full.', 'communities': str(microblogs.id),
                'language_id': str(english.id), 'notify_author': 'y', 'timezone': 'Europe/London',
                'submit': 'Publish', 'csrf_token': csrf(app, client)}
        data.update(overrides)
        return data

    def post(self, app, client, microblogs, actor='microblogs', **overrides):
        """The form as it would be redisplayed, or None when the post was made. The compose page needs a CSRF
        token field that the test config leaves out, so its rendering is captured, as test_community_add_post does."""
        with patch('app.shared.post.notify_about_post'), \
                patch('app.community.routes.render_template', return_value='rendered') as rendered:
            client.post(f'/community/{actor}/submit/discussion', data=self.payload(app, client, microblogs, **overrides))
        return rendered.call_args.kwargs['form'] if rendered.called else None

    def test_the_title_is_optional_there(self, app, poster):
        client, joined, microblogs = poster

        with patch('app.community.routes.render_template', return_value='rendered') as rendered:
            client.get('/community/microblogs/submit/discussion')

        title = rendered.call_args.kwargs['form'].title
        assert title.label.text == 'Title (optional)' and not title.flags.required

    def test_an_untitled_post_is_a_microblog_post(self, app, poster):
        client, joined, microblogs = poster

        redisplayed = self.post(app, client, microblogs)

        assert redisplayed is None, redisplayed and redisplayed.errors
        post = Post.query.one()
        assert post.microblog is True and post.title == '[Microblog] Composed in full'

    def test_a_titled_post_keeps_its_title(self, app, poster):
        client, joined, microblogs = poster

        assert self.post(app, client, microblogs, title='A titled post') is None

        post = Post.query.one()
        assert post.microblog is False and post.title == 'A titled post'

    def test_an_untitled_post_with_no_text_is_refused(self, app, poster):
        client, joined, microblogs = poster

        redisplayed = self.post(app, client, microblogs, body='')

        assert redisplayed.body.errors and Post.query.count() == 0

    def test_another_community_still_requires_a_title(self, app, poster):
        client, joined, microblogs = poster
        redisplayed = self.post(app, client, joined, actor=joined.name)

        assert redisplayed.title.errors and Post.query.count() == 0
