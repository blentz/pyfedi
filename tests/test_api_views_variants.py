"""What the alpha API serialises, variant by variant.

Sub-project 91 -- `app/api/alpha/views.py`. Every response the API sends is
built here, so this is the file that decides what leaves the instance. The
routes above it were covered in sub-project 85; what was left uncovered were
the variants and the optional keys: the media a post carries, the fields an
account chooses to show, the flair on a community, and -- the reason this
file is worth the effort -- the three places access is decided, two for a
private community and one for a private feed.

One defect: the private-feed branch built its `actor_id` as
`feed.public_url() + "/" + feed.name.rsplit("/", 1)[1]`, and `feed.name` is
the url slug the feed was created with, which holds no "/". So the branch
raised `IndexError` every time, and every private feed was a 500 for the
owner who asked for it and for an admin (D1276).

These are driven as functions rather than over HTTP: a view's variant is
picked by its caller, and several variants have no route of their own.
"""
import pytest
from flask import current_app, g

from app import db
from app.api.alpha.views import (blocked_communities_view,
                                 blocked_instances_view, blocked_people_view,
                                 cached_modlist_for_community,
                                 cached_modlist_for_user, calculate_child_count,
                                 calculate_path, community_view,
                                 federated_instances_view, flair_view,
                                 instance_view, joined_communities_view,
                                 moderating_communities_view, post_view,
                                 reply_view, site_view, user_view, users_total)
from app.constants import (POST_TYPE_ARTICLE, POST_TYPE_IMAGE, POST_TYPE_LINK,
                           POST_TYPE_VIDEO)
from app.models import (Community, CommunityFlair, File, Instance, Post,
                        PostReply, Site, User)
from tests.factories import (make_community, make_community_flair,
                             make_community_member, make_file, make_post,
                             make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    from types import SimpleNamespace

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('probeland')
    author = api_baseline.user2
    reader = api_baseline.user3
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/v/1')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author,
                           reader=reader, post=post, baseline=api_baseline,
                           server=current_app.config['SERVER_NAME'])


def promote(user, community):
    """Make an existing member a moderator; `env` has already joined them."""
    from app.models import CommunityMember
    membership = CommunityMember.query.filter_by(
        user_id=user.id, community_id=community.id).one()
    membership.is_moderator = True
    db.session.commit()
    return membership


def an_image(env, **columns):
    image = make_file(**{k: v for k, v in columns.items()
                         if k in ('file_path', 'source_url')})
    for key, value in columns.items():
        if key not in ('file_path', 'source_url'):
            setattr(image, key, value)
    db.session.commit()
    return image


class TestThePostAsItIsSerialised:
    def test_a_link_post_carries_its_url(self, env):
        env.post.type = POST_TYPE_LINK
        env.post.url = 'https://example.test/an-article'
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert view['url'] == 'https://example.test/an-article'
        assert view['post_type'] == 'Link'

    def test_a_link_post_with_no_url_at_all(self, env):
        env.post.type = POST_TYPE_LINK
        env.post.url = None
        db.session.commit()
        assert 'url' not in post_view(env.post, variant=1)

    def test_a_link_post_carries_its_thumbnails(self, env):
        env.post.type = POST_TYPE_LINK
        env.post.url = 'https://example.test/an-article'
        env.post.image = an_image(env, file_path='https://cdn.test/medium.png',
                                  thumbnail_path='https://cdn.test/small.png',
                                  alt_text='a picture of something')
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert view['thumbnail_url'] == 'https://cdn.test/medium.png'
        assert view['small_thumbnail_url'] == 'https://cdn.test/small.png'
        assert view['alt_text'] == 'a picture of something'

    def test_a_video_post_is_treated_the_same_way(self, env):
        env.post.type = POST_TYPE_VIDEO
        env.post.url = 'https://example.test/a-video'
        env.post.image = an_image(env, file_path='https://cdn.test/medium.png')
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert view['url'] == 'https://example.test/a-video'
        assert view['thumbnail_url'] == 'https://cdn.test/medium.png'

    def test_an_image_whose_urls_are_all_empty_adds_no_keys(self, env):
        """`medium_url()` falls back to `thumbnail_url()`, which is '' when
        there is neither a thumbnail nor a source."""
        env.post.type = POST_TYPE_LINK
        env.post.url = 'https://example.test/an-article'
        env.post.image = an_image(env)
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert 'thumbnail_url' not in view
        assert 'small_thumbnail_url' not in view
        assert 'alt_text' not in view

    def test_an_image_post_carries_the_image_itself(self, env):
        env.post.type = POST_TYPE_IMAGE
        env.post.image = an_image(env, source_url='https://cdn.test/full.png',
                                  file_path='https://cdn.test/medium.png',
                                  thumbnail_path='https://cdn.test/small.png',
                                  alt_text='a photograph')
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert view['url'] == 'https://cdn.test/full.png'
        assert view['thumbnail_url'] == 'https://cdn.test/medium.png'
        assert view['small_thumbnail_url'] == 'https://cdn.test/small.png'
        assert view['alt_text'] == 'a photograph'

    def test_an_image_post_with_no_image_row(self, env):
        env.post.type = POST_TYPE_IMAGE
        env.post.image_id = None
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert 'url' not in view or view.get('url') is None

    def test_an_image_with_dimensions_reports_them(self, env):
        env.post.type = POST_TYPE_IMAGE
        env.post.image = an_image(env, source_url='https://cdn.test/full.png',
                                  width=800, height=600)
        db.session.commit()
        view = post_view(env.post, variant=1)
        assert view['image_details'] == {'width': 800, 'height': 600}

    def test_an_image_with_no_dimensions_reports_none(self, env):
        env.post.type = POST_TYPE_IMAGE
        env.post.image = an_image(env, source_url='https://cdn.test/full.png')
        db.session.commit()
        assert 'image_details' not in post_view(env.post, variant=1)


class TestCrossPosts:
    @pytest.fixture
    def crossed(self, env):
        other = make_post(env.community, env.author,
                          ap_id='https://test.piefed.local/v/2',
                          title='the same thing elsewhere')
        db.session.commit()
        env.post.cross_posts = [other.id]
        db.session.commit()
        return other

    def test_they_are_listed_with_their_community(self, env, crossed):
        view = post_view(env.post, variant=1)
        assert view['cross_posts'] == [
            {'post_id': crossed.id, 'reply_count': crossed.reply_count,
             'community_name': env.community.title}]

    def test_a_post_with_none_lists_none(self, env):
        env.post.cross_posts = None
        db.session.commit()
        assert post_view(env.post, variant=1)['cross_posts'] == []

    def test_one_that_has_since_been_deleted_is_skipped(self, env, crossed):
        """Variant 3 builds each cross-post as a view of its own, and the id
        can outlive the row."""
        env.post.cross_posts = [crossed.id, 999999]
        db.session.commit()
        view = post_view(env.post, variant=3, user_id=env.author.id)
        assert len(view['cross_posts']) == 1


class TestAPrivateCommunitysPost:
    @pytest.fixture
    def private(self, env):
        env.community.private = True
        db.session.commit()
        return env.community

    def test_a_stranger_is_refused_the_detail(self, env, private):
        with pytest.raises(Exception, match='Private community'):
            post_view(env.post, variant=3, user_id=env.reader.id)

    def test_an_anonymous_caller_is_refused_too(self, env, private):
        with pytest.raises(Exception, match='Private community'):
            post_view(env.post, variant=3, user_id=None)

    def test_a_member_is_not(self, env, private):
        view = post_view(env.post, variant=3, user_id=env.author.id)
        assert view['post_view']['post']['id'] == env.post.id


class TestTheAccountAsItIsSerialised:
    def test_the_about_text_is_included_in_full(self, env):
        env.author.about = 'a paragraph'
        env.author.about_html = '<p>a paragraph</p>'
        db.session.commit()
        view = user_view(env.author, variant=1)
        assert view['about'] == 'a paragraph'
        assert view['about_html'] == '<p>a paragraph</p>'

    def test_a_stub_leaves_the_about_text_out(self, env):
        env.author.about = 'a paragraph'
        env.author.about_html = '<p>a paragraph</p>'
        db.session.commit()
        view = user_view(env.author, variant=1, stub=True)
        assert 'about' not in view
        assert 'about_html' not in view

    def test_an_avatar_is_included_even_in_a_stub(self, env):
        env.author.avatar = an_image(env, file_path='https://cdn.test/me.png')
        db.session.commit()
        assert user_view(env.author, variant=1,
                         stub=True)['avatar'] == 'https://cdn.test/me.png'

    def test_a_banner_is_not(self, env):
        env.author.cover = an_image(env, file_path='https://cdn.test/wide.png')
        db.session.commit()
        assert user_view(env.author, variant=1)['banner'] == \
            'https://cdn.test/wide.png'
        assert 'banner' not in user_view(env.author, variant=1, stub=True)

    def test_an_account_with_no_display_name_falls_back_to_its_username(
            self, env):
        env.author.title = None
        db.session.commit()
        view = user_view(env.author, variant=1)
        assert view['title'] == view['user_name']

    def test_the_flair_it_wears_in_one_community(self, env):
        from app.models import UserFlair
        db.session.add(UserFlair(user_id=env.author.id,
                                 community_id=env.community.id,
                                 flair='regular'))
        db.session.commit()
        view = user_view(env.author, variant=1,
                         flair_community_id=env.community.id)
        assert view['flair'] == 'regular'

    def test_no_flair_in_a_community_it_has_none_in(self, env):
        view = user_view(env.author, variant=1,
                         flair_community_id=env.community.id)
        assert 'flair' not in view


class TestTheCommunityAsItIsSerialised:
    def test_its_flair_is_listed(self, env):
        make_community_flair(env.community, name='discussion')
        db.session.commit()
        view = community_view(env.community, variant=2)
        assert [f['flair_title'] for f in view['flair_list']] == ['discussion']

    def test_a_community_with_no_flair(self, env):
        assert community_view(env.community, variant=2)['flair_list'] == []

    def test_the_moderator_list_comes_with_the_detail(self, env):
        promote(env.author, env.community)
        view = community_view(env.community, variant=3, user_id=env.author.id)
        assert env.author.id in [m['moderator']['id'] for m in
                                 view['moderators']]

    def test_the_follow_response(self, env):
        view = community_view(env.community, variant=4, user_id=env.author.id)
        assert view['community_view']['community']['id'] == env.community.id
        assert view['discussion_languages'] == []

    def test_the_block_response_says_whether_it_is_blocked(self, env):
        from app.models import CommunityBlock
        db.session.add(CommunityBlock(user_id=env.reader.id,
                                      community_id=env.community.id))
        db.session.commit()
        assert community_view(env.community, variant=5,
                              user_id=env.reader.id)['blocked'] is True

    def test_and_when_it_is_not(self, env):
        assert community_view(env.community, variant=5,
                              user_id=env.reader.id)['blocked'] is False

    def test_the_resolve_object_shape(self, env):
        view = community_view(env.community, variant=6, user_id=env.author.id)
        assert view['community']['community']['id'] == env.community.id


class TestAPrivateCommunity:
    @pytest.fixture
    def private(self, env):
        env.community.private = True
        db.session.commit()
        return env.community

    def test_the_detail_is_refused_to_a_stranger(self, env, private):
        with pytest.raises(Exception, match='Private community'):
            community_view(private, variant=3, user_id=env.reader.id)

    def test_and_to_an_anonymous_caller(self, env, private):
        with pytest.raises(Exception, match='Private community'):
            community_view(private, variant=3, user_id=None)

    def test_a_member_is_shown_it(self, env, private):
        view = community_view(private, variant=3, user_id=env.author.id)
        assert view['community_view']['community']['id'] == private.id

    def test_resolving_it_is_refused_too(self, env, private):
        with pytest.raises(Exception, match='Private community'):
            community_view(private, variant=6, user_id=env.reader.id)

    def test_but_not_for_a_member(self, env, private):
        assert community_view(private, variant=6, user_id=env.author.id)


class TestFlair:
    def test_a_flair_can_be_named_by_its_id(self, env):
        flair = make_community_flair(env.community, name='announcement')
        db.session.commit()
        assert flair_view(flair.id)['flair_title'] == 'announcement'

    def test_an_id_nobody_holds(self, env):
        from sqlalchemy.exc import NoResultFound
        with pytest.raises(NoResultFound):
            flair_view(999999)


class TestAnInstanceAsItIsSerialised:
    def test_it_carries_its_software_and_version(self, env):
        instance = env.baseline.instance_remote
        instance.software = 'lemmy'
        instance.version = '0.19.3'
        db.session.commit()
        view = instance_view(instance, variant=1)
        assert view['software'] == 'lemmy'
        assert view['version'] == '0.19.3'

    def test_an_instance_that_has_never_said_its_version(self, env):
        """Clients parse the field, so an empty one is answered with the
        lowest version there is rather than left blank."""
        instance = env.baseline.instance_remote
        instance.version = None
        db.session.commit()
        assert instance_view(instance, variant=1)['version'] == '0.0.1'

    def test_it_can_be_named_by_its_id(self, env):
        instance = env.baseline.instance_remote
        assert instance_view(instance.id, variant=1)['id'] == instance.id


class TestTheReplyPath:
    def test_a_top_level_reply(self, env):
        reply = make_post_reply(env.post, env.author)
        db.session.commit()
        calculate_path(reply)
        assert reply.path == [0, reply.id]

    def test_a_reply_one_deep(self, env):
        parent = make_post_reply(env.post, env.author)
        db.session.commit()
        child = make_post_reply(env.post, env.author, body='a child')
        child.parent_id = parent.id
        child.depth = 1
        db.session.commit()
        calculate_path(child)
        assert child.path == [0, parent.id, child.id]

    def test_a_reply_further_down(self, env):
        first = make_post_reply(env.post, env.author)
        db.session.commit()
        second = make_post_reply(env.post, env.author, body='second')
        second.parent_id = first.id
        second.depth = 1
        db.session.commit()
        third = make_post_reply(env.post, env.author, body='third')
        third.parent_id = second.id
        third.depth = 2
        db.session.commit()
        calculate_path(third)
        assert third.path[0] == 0
        assert third.path[-1] == third.id
        assert second.id in third.path

    def test_counting_the_children_of_a_reply(self, env):
        parent = make_post_reply(env.post, env.author)
        db.session.commit()
        child = make_post_reply(env.post, env.author, body='a child')
        child.parent_id = parent.id
        child.depth = 1
        child.path = [0, parent.id, child.id]
        db.session.commit()
        calculate_child_count(parent)
        assert parent.child_count == 1

    def test_a_reply_nobody_answered(self, env):
        lonely = make_post_reply(env.post, env.author)
        lonely.path = [0, lonely.id]
        db.session.commit()
        calculate_child_count(lonely)
        assert lonely.child_count == 0


class TestTheListsOnAnAccount:
    def test_the_communities_it_moderates(self, env):
        promote(env.author, env.community)
        listed = moderating_communities_view(env.author)
        assert env.community.id in [entry['community']['id']
                                    for entry in listed]

    def test_the_communities_it_has_joined(self, env):
        listed = joined_communities_view(env.author)
        assert env.community.id in [entry['community']['id']
                                    for entry in listed]

    def test_the_people_it_blocks(self, env):
        from app.models import UserBlock
        db.session.add(UserBlock(blocker_id=env.author.id,
                                 blocked_id=env.reader.id))
        db.session.commit()
        listed = blocked_people_view(env.author)
        assert [entry['target']['id'] for entry in listed] == [env.reader.id]

    def test_the_communities_it_blocks(self, env):
        from app.models import CommunityBlock
        db.session.add(CommunityBlock(user_id=env.author.id,
                                      community_id=env.community.id))
        db.session.commit()
        listed = blocked_communities_view(env.author)
        assert [entry['community']['id'] for entry in listed] == \
            [env.community.id]

    def test_the_instances_it_blocks(self, env):
        from app.models import InstanceBlock
        db.session.add(InstanceBlock(
            user_id=env.author.id,
            instance_id=env.baseline.instance_remote.id))
        db.session.commit()
        listed = blocked_instances_view(env.author)
        assert env.baseline.instance_remote.id in \
            [entry['instance']['id'] for entry in listed]

    def test_an_account_that_blocks_nobody(self, env):
        assert blocked_people_view(env.reader) == []
        assert blocked_communities_view(env.reader) == []


class TestTheModeratorLists:
    def test_a_community_with_no_moderators(self, env):
        assert cached_modlist_for_community(env.community.id) == []

    def test_the_communities_one_account_moderates(self, env):
        promote(env.author, env.community)
        assert env.community.id in [entry['community']['id']
                                    for entry in
                                    cached_modlist_for_user(env.author)]

    def test_an_account_that_moderates_nothing(self, env):
        assert cached_modlist_for_user(env.reader) == []


class TestTheInstanceItself:
    def test_how_many_accounts_there_are(self, env):
        assert users_total() >= 1

    def test_the_site_as_an_anonymous_caller_sees_it(self, env):
        view = site_view(user=None)
        assert 'site' in view

    def test_the_site_as_a_signed_in_caller_sees_it(self, env):
        view = site_view(user=env.author)
        assert 'my_user' in view

    def test_the_instances_this_one_federates_with(self, env):
        view = federated_instances_view()
        assert 'federated_instances' in view


class TestTheSiteAsItIsSerialised:
    def test_a_sidebar_written_as_plain_text(self, env):
        """When the markdown and the rendered HTML are the same string there
        is nothing to send twice."""
        g.site.sidebar = 'the rules'
        g.site.sidebar_html = 'the rules'
        db.session.commit()
        site = site_view(user=None)['site']
        assert site['sidebar'] == 'the rules'
        assert 'sidebar_md' not in site

    def test_a_sidebar_written_in_markdown(self, env):
        g.site.sidebar = '**the rules**'
        g.site.sidebar_html = '<p><strong>the rules</strong></p>'
        db.session.commit()
        site = site_view(user=None)['site']
        assert site['sidebar_md'] == '**the rules**'
        assert site['sidebar'] == '<p><strong>the rules</strong></p>'

    def test_a_sidebar_that_was_never_rendered(self, env):
        g.site.sidebar = 'the rules'
        g.site.sidebar_html = None
        db.session.commit()
        assert site_view(user=None)['site']['sidebar'] == 'the rules'

    def test_no_sidebar_at_all(self, env):
        g.site.sidebar = None
        g.site.sidebar_html = None
        db.session.commit()
        site = site_view(user=None)['site']
        assert 'sidebar' not in site
        assert 'sidebar_md' not in site

    def test_an_announcement(self, env):
        from app.utils import set_setting
        set_setting('announcement', '**closing early**')
        set_setting('announcement_html', '<p><strong>closing early</strong></p>')
        site = site_view(user=None)['site']
        assert site['announcement_md'] == '**closing early**'
        assert site['announcement'] == '<p><strong>closing early</strong></p>'

    def test_no_announcement(self, env):
        site = site_view(user=None)['site']
        assert 'announcement' not in site
        assert 'announcement_md' not in site

    def test_a_description(self, env):
        g.site.description = 'a place for things'
        db.session.commit()
        assert site_view(user=None)['site']['description'] == \
            'a place for things'

    def test_the_languages_it_offers(self, env):
        from app.models import Language
        db.session.add(Language(code='en', name='English'))
        db.session.commit()
        codes = [language['code']
                 for language in site_view(user=None)['site']['all_languages']]
        assert 'en' in codes


class TestTheInstanceChooser:
    """What a brand-new instance shows about itself to a directory."""

    def test_an_instance_that_has_chosen_a_language(self, env):
        from app.models import Language
        language = Language(code='en', name='English')
        db.session.add(language)
        db.session.commit()
        g.site.language_id = language.id
        db.session.commit()
        from app.api.alpha.views import site_instance_chooser_view
        assert site_instance_chooser_view()['language']['code'] == 'en'

    def test_an_instance_that_has_not(self, env):
        g.site.language_id = None
        db.session.commit()
        from app.api.alpha.views import site_instance_chooser_view
        assert site_instance_chooser_view()['language'] is None

    @pytest.mark.parametrize('settings,expected', [
        ({}, 'Embryonic'),
        ({'financial_stability': True}, 'Low'),
        ({'financial_stability': True, 'daily_backups': True}, 'Medium'),
        ({'financial_stability': True, 'daily_backups': True,
          'number_of_admins': 2}, 'High'),
    ])
    def test_how_grown_up_it_says_it_is(self, env, settings, expected):
        from app.api.alpha.views import site_instance_chooser_view
        from app.utils import set_setting
        for key, value in settings.items():
            set_setting(key, value)
        assert site_instance_chooser_view()['maturity'] == expected

    def test_it_reports_who_it_refuses_to_talk_to(self, env):
        from app.api.alpha.views import site_instance_chooser_view
        from app.models import BannedInstances
        db.session.add(BannedInstances(domain='lemmygrad.ml'))
        db.session.commit()
        assert 'lemmygrad.ml' in site_instance_chooser_view()['defederation']

    def test_and_who_it_trusts(self, env):
        from app.api.alpha.views import site_instance_chooser_view
        env.baseline.instance_remote.trusted = True
        db.session.commit()
        assert env.baseline.instance_remote.domain in \
            site_instance_chooser_view()['trusts']

    def test_an_instance_with_no_logo_of_its_own(self, env):
        from app.api.alpha.views import site_instance_chooser_view
        g.site.logo = None
        db.session.commit()
        assert site_instance_chooser_view()['logo_url'].endswith(
            '/static/images/piefed_logo_icon_t_75.png')

    def test_one_that_has_set_a_logo(self, env):
        from app.api.alpha.views import site_instance_chooser_view
        g.site.logo = '/static/images/ours.png'
        db.session.commit()
        assert site_instance_chooser_view()['logo_url'].endswith(
            '/static/images/ours.png')


class TestPrivateMessages:
    @pytest.fixture
    def message(self, env):
        from tests.factories import make_chat_message
        message = make_chat_message(env.author, env.reader,
                                    ap_id='https://test.piefed.local/pm/1')
        db.session.commit()
        return message

    def test_a_message_between_two_local_accounts(self, env, message):
        from app.api.alpha.views import private_message_view
        view = private_message_view(message, variant=1)
        assert view['private_message']['creator_id'] == env.author.id
        assert view['private_message']['recipient_id'] == env.reader.id

    def test_one_the_author_deleted(self, env, message):
        from app.api.alpha.views import private_message_view
        message.deleted = True
        db.session.commit()
        assert private_message_view(message, variant=1)[
            'private_message']['content'] == 'Deleted by author'

    def test_an_old_local_message_with_no_ap_id(self, env, message):
        """Messages predating the column have none, and the schema requires
        one, so it is derived rather than left out."""
        from app.api.alpha.views import private_message_view
        message.ap_id = None
        db.session.commit()
        view = private_message_view(message, variant=1)
        assert view['private_message']['ap_id'].endswith(
            f'/private_message/{message.id}')

    def test_an_old_remote_message_with_no_ap_id(self, env, message):
        from app.api.alpha.views import private_message_view
        message.ap_id = None
        env.author.instance_id = env.baseline.instance_remote.id
        db.session.commit()
        view = private_message_view(message, variant=1)
        assert view['private_message']['ap_id'].endswith(
            f'/message/{message.id}')

    def test_a_message_whose_deleted_flag_was_never_set(self, env, message):
        from app.api.alpha.views import private_message_view
        message.deleted = None
        db.session.commit()
        assert private_message_view(message, variant=1)[
            'private_message']['deleted'] is False


class TestConversations:
    @pytest.fixture
    def conversation(self, env):
        from tests.factories import make_conversation
        conversation = make_conversation(env.author, env.reader)
        db.session.commit()
        return conversation

    def test_who_is_in_it(self, env, conversation):
        from app.api.alpha.views import conversation_information_view
        view = conversation_information_view(conversation)
        assert view['creator_id'] == env.author.id
        assert {member['id'] for member in view['members']} == \
            {env.author.id, env.reader.id}

    def test_it_can_be_named_by_its_id(self, env, conversation):
        from app.api.alpha.views import conversation_information_view
        assert conversation_information_view(conversation.id)['id'] == \
            conversation.id

    def test_a_report_about_something_that_is_not_a_conversation(self, env):
        from app.api.alpha.views import conversation_report_view
        from app.models import Report
        report = Report(reporter_id=env.reader.id, reasons='spam',
                        suspect_post_id=env.post.id)
        db.session.add(report)
        db.session.commit()
        with pytest.raises(Exception, match='not for a conversation'):
            conversation_report_view(report)


class TestRegistrations:
    @pytest.fixture
    def registration(self, env):
        from tests.factories import make_user_registration
        applicant = make_user(env.baseline.instance_local, 'hopeful',
                              local=True)
        db.session.commit()
        registration = make_user_registration(applicant, answer='because')
        db.session.commit()
        return registration

    def test_what_an_admin_is_shown_about_an_applicant(self, env,
                                                       registration):
        from app.api.alpha.views import registration_view
        view = registration_view(registration)
        assert view['answer'] == 'because'
        assert view['email'] == registration.user.email
        assert view['user_id'] == registration.user.id

    def test_throwaway_addresses_are_not_checked_when_the_flag_is_off(
            self, env, registration, monkeypatch):
        from app.api.alpha.views import registration_view
        monkeypatch.setitem(current_app.config, 'FLAG_THROWAWAY_EMAILS', False)
        assert registration_view(registration)['answer'] == 'because'


class TestAFeedAsItIsSerialised:
    """A feed that is not public is its owner's alone, and `feed_view` is
    where that is decided."""

    @pytest.fixture
    def feed(self, env):
        from tests.factories import make_local_feed
        feed = make_local_feed('news', public=True)
        feed.user_id = env.author.id
        feed.last_edit = feed.created_at
        db.session.commit()
        return feed

    def view(self, feed, user_id=None, include_communities=False,
             subscribed=None, banned_from=None, blocked_community_ids=None,
             blocked_instance_ids=None, variant=1):
        from app.api.alpha.views import feed_view
        return feed_view(feed, variant=variant, user_id=user_id,
                         subscribed=subscribed or [],
                         include_communities=include_communities,
                         communities_moderating=[],
                         banned_from=banned_from or [],
                         communities_joined=[],
                         blocked_community_ids=blocked_community_ids or [],
                         blocked_instance_ids=blocked_instance_ids or [])

    def test_a_public_feed_is_shown_to_anybody(self, env, feed):
        view = self.view(feed)
        assert view['id'] == feed.id
        assert view['actor_id'] == feed.public_url()

    def test_a_private_feed_is_refused_to_a_stranger(self, env, feed):
        feed.public = False
        db.session.commit()
        with pytest.raises(Exception, match='insufficient permissions'):
            self.view(feed, user_id=env.reader.id)

    def test_and_to_an_anonymous_caller(self, env, feed):
        feed.public = False
        db.session.commit()
        with pytest.raises(Exception, match='insufficient permissions'):
            self.view(feed, user_id=None)

    def test_its_owner_is_shown_it(self, env, feed):
        """D1276. `feed.name.rsplit('/', 1)[1]` was `IndexError: list index
        out of range`: `name` is the url slug the feed was created with and
        holds no '/', so this branch had never once returned. Every private
        feed was a 500 for the one account entitled to see it."""
        feed.public = False
        db.session.commit()
        view = self.view(feed, user_id=env.author.id)
        assert view['id'] == feed.id
        assert view['actor_id'] == feed.public_url()

    def test_an_admin_is_shown_it_too(self, env, feed):
        feed.public = False
        db.session.commit()
        admin = env.baseline.user1
        g.admin_ids = [admin.id]
        assert self.view(feed, user_id=admin.id)['id'] == feed.id

    def test_it_says_whether_the_caller_owns_it(self, env, feed):
        assert self.view(feed, user_id=env.author.id)['owner'] is True
        assert self.view(feed, user_id=env.reader.id)['owner'] is False

    def test_it_says_whether_the_caller_follows_it(self, env, feed):
        assert self.view(feed, subscribed=[feed.id])['subscribed'] is True
        assert self.view(feed, subscribed=[])['subscribed'] is False

    def test_its_icon_and_banner(self, env, feed):
        feed.icon = an_image(env, file_path='https://cdn.test/icon.png')
        feed.image = an_image(env, file_path='https://cdn.test/wide.png')
        db.session.commit()
        view = self.view(feed)
        assert view['icon'] == 'https://cdn.test/icon.png'
        assert view['banner'] == 'https://cdn.test/wide.png'

    def test_the_communities_in_it(self, env, feed):
        from tests.factories import make_feed_item
        make_feed_item(feed, env.community)
        db.session.commit()
        view = self.view(feed, include_communities=True)
        assert [c['id'] for c in view['communities']] == [env.community.id]

    def test_a_private_community_is_not_listed_in_it(self, env, feed):
        from tests.factories import make_feed_item
        env.community.private = True
        make_feed_item(feed, env.community)
        db.session.commit()
        assert self.view(feed, include_communities=True)['communities'] == []

    def test_nor_a_banned_one(self, env, feed):
        from tests.factories import make_feed_item
        env.community.banned = True
        make_feed_item(feed, env.community)
        db.session.commit()
        assert self.view(feed, include_communities=True)['communities'] == []

    def test_nor_one_the_caller_has_blocked(self, env, feed):
        from tests.factories import make_feed_item
        make_feed_item(feed, env.community)
        db.session.commit()
        assert self.view(feed, include_communities=True,
                         blocked_community_ids=[env.community.id]
                         )['communities'] == []

    def test_nor_one_on_an_instance_the_caller_has_blocked(self, env, feed):
        from tests.factories import make_feed_item
        make_feed_item(feed, env.community)
        db.session.commit()
        assert self.view(feed, include_communities=True,
                         blocked_instance_ids=[env.community.instance_id]
                         )['communities'] == []

    def test_nor_one_the_caller_is_banned_from(self, env, feed):
        from tests.factories import make_feed_item
        make_feed_item(feed, env.community)
        db.session.commit()
        assert self.view(feed, include_communities=True,
                         banned_from=[env.community.id])['communities'] == []

    def test_the_resolve_object_shape(self, env, feed):
        view = self.view(feed, variant=2)
        assert view['feed']['id'] == feed.id
        assert view['feed']['children'] == []

    def test_it_can_be_named_by_its_id(self, env, feed):
        assert self.view(feed.id)['id'] == feed.id


class TestATopicAsItIsSerialised:
    @pytest.fixture
    def topic(self, env):
        from app.models import Topic
        topic = Topic(name='Things', machine_name='things',
                      num_communities=0)
        db.session.add(topic)
        db.session.commit()
        return topic

    def view(self, topic, include_communities=False):
        from app.api.alpha.views import topic_view
        return topic_view(topic, variant=1, communities_moderating=[],
                          banned_from=[], communities_joined=[],
                          blocked_community_ids=[], blocked_instance_ids=[],
                          include_communities=include_communities)

    def test_its_fields_are_renamed_for_the_api(self, env, topic):
        view = self.view(topic)
        assert view['title'] == 'Things'
        assert view['name'] == 'things'
        assert view['communities_count'] == 0

    def test_it_can_be_named_by_its_id(self, env, topic):
        assert self.view(topic.id)['id'] == topic.id
