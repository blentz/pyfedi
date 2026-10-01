"""Round 251: the URL a post gets, and what it publishes about itself.

Four clusters on `Post` in `app/models.py`.

    generate_ap_id / generate_slug   a post's ActivityPub id and its local path. The id is
                                     what every peer stores as the post's identity, so it is
                                     written once and never rewritten.
    tags_for_activitypub             the flair, hashtags and custom emojis a post sends out
    blurred                          whether a post's image is hidden behind a click
    posted_at_localized              the relative time, with a locale fallback

`generate_ap_id` matters most: two communities can ask for different URL shapes
(`post_url_type`), the title may not slugify at all, and the guard `len(self.ap_id) == 10`
exists because a placeholder of that length is written before the id is known. Getting any of
that wrong changes a post's identity after peers have already stored it.
"""
from types import SimpleNamespace

import pytest
from flask import g

from app import db
from app.models import Community, CommunityFlair, Emoji, Post, Site, Tag, utcnow
from tests.factories import (make_community, make_community_flair, make_community_member,
                             make_post, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    community = make_community('slugland')
    author = make_user(api_baseline.instance_local, 'slugauthor', local=True)
    db.session.commit()
    make_community_member(author, community)
    post = make_post(community, author, ap_id='https://test.piefed.local/p/1',
                     title='A Post With A Title')
    db.session.commit()
    return SimpleNamespace(app=app, community=community, author=author, post=post,
                           baseline=api_baseline)


# --------------------------------------------------------------------------
# The id and the path a post gets
# --------------------------------------------------------------------------


class TestTheUrlAPostGets:

    def _fresh(self, env, title='A Post With A Title', ap_id=None, slug=None):
        post = make_post(env.community, env.author,
                         ap_id='https://test.piefed.local/p/placeholder', title=title)
        post.ap_id = ap_id
        post.slug = slug
        db.session.commit()
        return post

    def test_a_friendly_community_gets_a_readable_id_and_path(self, env):
        """The default shape: `/c/<name>@<domain>/p/<id>/<slug>`, which is what a reader sees
        and what peers store. The community's URL type is unset, and `not
        community.post_url_type` is what makes that the default rather than an error."""
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env)

        post.generate_ap_id(env.community)

        assert post.ap_id == (f'{env.app.config["SERVER_URL"]}/c/{env.community.name}'
                              f'@{env.community.ap_domain}/p/{post.id}/'
                              'a-post-with-a-title')
        assert post.slug == (f'/c/{env.community.name}@{env.community.ap_domain}'
                             f'/p/{post.id}/a-post-with-a-title')

    def test_a_community_asking_for_the_old_shape_gets_it(self, env):
        """`post_url_type` anything other than `friendly`: `/post/<id>`. Some communities
        want the short form, and the two shapes must not be mixed -- the id and the slug are
        built together in each arm."""
        env.community.post_url_type = 'legacy'
        db.session.commit()
        post = self._fresh(env)

        post.generate_ap_id(env.community)

        assert post.ap_id == f'{env.app.config["SERVER_URL"]}/post/{post.id}'
        assert post.slug == f'/post/{post.id}'

    def test_a_title_that_cannot_be_slugified_falls_back(self, env):
        """`if slug:` -- a title of punctuation or of a script `slugify` strips entirely
        produces an empty slug, and `/p/<id>/` with nothing after it is not a URL. The
        fallback is the old shape."""
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env, title='!!! ???')

        post.generate_ap_id(env.community)

        assert post.ap_id == f'{env.app.config["SERVER_URL"]}/post/{post.id}'
        assert post.slug == f'/post/{post.id}'

    def test_an_existing_id_is_never_rewritten(self, env):
        """The guard. A post that already has an ap_id has been published under it, and
        peers have stored it as the post's identity -- rewriting it orphans every reply and
        vote that referenced it."""
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env, ap_id='https://peer.example/p/999',
                           slug='/post/whatever')

        post.generate_ap_id(env.community)

        assert post.ap_id == 'https://peer.example/p/999'
        assert post.slug == '/post/whatever'

    @pytest.mark.parametrize('placeholder', ['', 'abcdefghij'])
    def test_a_placeholder_id_is_replaced(self, env, placeholder):
        """`self.ap_id == '' or len(self.ap_id) == 10`. The ten-character case is a
        `gibberish(10)` placeholder written before the row has an id, and it has to be
        recognised as "no id yet" rather than as somebody's real one."""
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env, ap_id=placeholder)

        post.generate_ap_id(env.community)

        assert post.ap_id == (f'{env.app.config["SERVER_URL"]}/c/{env.community.name}'
                              f'@{env.community.ap_domain}/p/{post.id}/'
                              'a-post-with-a-title')

    def test_generate_slug_sets_the_path_without_touching_the_id(self, env):
        """`generate_slug` is the incoming-post half: a remote post arrives WITH an ap_id and
        needs only a local path, so this function must leave the id alone."""
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env, ap_id='https://peer.example/p/999')

        post.generate_slug(env.community)

        assert post.ap_id == 'https://peer.example/p/999'
        assert post.slug == f'/c/{env.community.link()}/p/{post.id}/a-post-with-a-title'

    def test_generate_slug_honours_the_old_shape_too(self, env):
        env.community.post_url_type = 'legacy'
        db.session.commit()
        post = self._fresh(env, ap_id='https://peer.example/p/999')

        post.generate_slug(env.community)

        assert post.slug == f'/post/{post.id}'

    def test_generate_slug_leaves_an_existing_path_alone(self, env):
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env, ap_id='https://peer.example/p/999',
                           slug='/post/already-here')

        post.generate_slug(env.community)

        assert post.slug == '/post/already-here'

    def test_a_title_that_cannot_be_slugified_still_gets_a_path(self, env):
        env.community.post_url_type = None
        db.session.commit()
        post = self._fresh(env, title='!!! ???', ap_id='https://peer.example/p/999')

        post.generate_slug(env.community)

        assert post.slug == f'/post/{post.id}'


# --------------------------------------------------------------------------
# What a post publishes about itself
# --------------------------------------------------------------------------


class TestTheTagsAPostPublishes:
    """`tags_for_activitypub` builds the `tag` array every peer receives: the community's
    flair, the post's hashtags, and any custom emoji used in the body.
    """

    def test_flair_is_published_with_its_colours(self, env):
        """Lemmy reads `lemmy:CommunityTag`, including `blur_images` -- so the flair that
        hides an image here asks other instances to hide it too."""
        flair = make_community_flair(env.community, 'Spoilers')
        flair.text_color = '#ffffff'
        flair.background_color = '#000000'
        flair.blur_images = True
        env.post.flair.append(flair)
        db.session.commit()

        tags = env.post.tags_for_activitypub()

        entry = next(t for t in tags if t['type'] == 'lemmy:CommunityTag')
        assert entry['display_name'] == 'Spoilers'
        assert entry['text_color'] == '#ffffff'
        assert entry['background_color'] == '#000000'
        assert entry['blur_images'] is True

    def test_a_hashtag_is_published_with_a_link_to_this_instance(self, env):
        tag = Tag(name='books', display_as='Books')
        db.session.add(tag)
        db.session.commit()
        env.post.tags.append(tag)
        db.session.commit()

        tags = env.post.tags_for_activitypub()

        entry = next(t for t in tags if t['type'] == 'Hashtag')
        assert entry['name'] == '#books'
        assert entry['href'] == f'{env.app.config["SERVER_URL"]}/tag/books'

    def test_an_emoji_used_in_the_body_is_published(self, env):
        """A peer that receives `:partyparrot:` in the body needs the image URL to render it,
        so the emoji travels with the post."""
        db.session.add(Emoji(token=':partyparrot:',
                             url='https://test.piefed.local/e.png', instance_id=1))
        env.post.body = 'hello :partyparrot: world'
        db.session.commit()

        tags = env.post.tags_for_activitypub()

        entry = next(t for t in tags if t['type'] == 'Emoji')
        assert entry['name'] == ':partyparrot:'
        assert entry['icon']['url'] == 'https://test.piefed.local/e.png'
        assert entry['icon']['mediaType'] == 'image/png'

    def test_the_token_is_matched_case_insensitively_and_published_lower_cased(self, env):
        """`re.IGNORECASE` on the scan and `.lower()` on the token: somebody typing
        `:PartyParrot:` means the same emoji, and the stored token is lower case."""
        db.session.add(Emoji(token=':partyparrot:',
                             url='https://test.piefed.local/e.png', instance_id=1))
        env.post.body = 'hello :PartyParrot:'
        db.session.commit()

        tags = env.post.tags_for_activitypub()

        assert any(t.get('name') == ':partyparrot:' for t in tags)

    def test_an_unknown_token_publishes_nothing(self, env):
        """The body is full of colons in ordinary use -- times, URLs, ratios -- so a token
        that matches no Emoji row must produce no tag rather than a broken one."""
        env.post.body = 'at 12:30 we saw :nosuchemoji: and http://x/y'
        db.session.commit()

        tags = env.post.tags_for_activitypub()

        assert [t for t in tags if t['type'] == 'Emoji'] == []

    def test_a_body_with_no_colon_is_not_scanned(self, env):
        """`if self.body and ':' in self.body`. The regex and the query are skipped for the
        common case, which is every post that does not use an emoji."""
        db.session.add(Emoji(token=':partyparrot:',
                             url='https://test.piefed.local/e.png', instance_id=1))
        env.post.body = 'no emoji here'
        db.session.commit()

        assert env.post.tags_for_activitypub() == []

    def test_a_post_with_no_body_publishes_no_emoji(self, env):
        """The other half of the same guard: `None` has no `in`, and a link post has no
        body."""
        env.post.body = None
        db.session.commit()

        assert env.post.tags_for_activitypub() == []


# --------------------------------------------------------------------------
# Whether a post is hidden behind a click
# --------------------------------------------------------------------------


class TestWhetherAPostIsBlurred:
    """`blurred` decides whether an image is shown or hidden behind a click. The anonymous
    arm is unconditional -- NSFW, NSFL or spoiler flair all blur -- while a signed-in viewer's
    own preferences decide, where the value `2` means "blur" as opposed to `1`, "hide
    entirely".
    """

    def test_an_anonymous_viewer_has_nsfw_blurred(self, env):
        env.post.nsfw = True
        db.session.commit()

        assert env.post.blurred(None) is True

    def test_an_anonymous_viewer_has_nsfl_blurred(self, env):
        env.post.nsfl = True
        db.session.commit()

        assert env.post.blurred(None) is True

    def test_an_ordinary_post_is_not_blurred_for_anybody(self, env):
        assert env.post.blurred(None) is False
        assert env.post.blurred(env.author) is False

    @pytest.mark.parametrize('field,flag', [('hide_nsfw', 'nsfw'),
                                            ('hide_nsfl', 'nsfl')])
    def test_a_viewer_who_asked_for_blurring_gets_it(self, env, field, flag):
        """`== 2` exactly. `1` means hide the post completely and is handled elsewhere, so a
        truthiness test here would blur for viewers who asked not to see the post at all --
        and show nothing for the ones who did."""
        setattr(env.author, field, 2)
        setattr(env.post, flag, True)
        db.session.commit()

        assert env.post.blurred(env.author) is True

    @pytest.mark.parametrize('value', [0, 1, 3])
    def test_any_other_preference_does_not_blur(self, env, value):
        env.author.hide_nsfw = value
        env.post.nsfw = True
        db.session.commit()

        assert env.post.blurred(env.author) is False

    def test_a_bot_post_is_blurred_for_a_viewer_who_asked(self, env):
        env.author.ignore_bots = 2
        env.post.from_bot = True
        db.session.commit()

        assert env.post.blurred(env.author) is True

    def test_spoiler_flair_blurs_for_everybody(self, env):
        """The one condition in BOTH arms: a flair marked `blur_images` is the community's
        decision rather than the viewer's, so no preference switches it off."""
        flair = make_community_flair(env.community, 'Spoilers')
        flair.blur_images = True
        env.post.flair.append(flair)
        env.author.hide_nsfw = 0
        env.author.hide_nsfl = 0
        env.author.ignore_bots = 0
        db.session.commit()

        assert env.post.blurred(None) is True
        assert env.post.blurred(env.author) is True

    def test_flair_without_the_blur_flag_does_not(self, env):
        flair = make_community_flair(env.community, 'Discussion')
        flair.blur_images = False
        env.post.flair.append(flair)
        db.session.commit()

        assert env.post.spoiler_flair() is False
        assert env.post.blurred(None) is False


class TestTheRelativeTimeOnAPost:

    def test_an_unknown_locale_falls_back_to_english(self, env):
        """Some locales have no definition for 'weeks', and pendulum raises `ValueError` for
        them -- on a page that was merely rendering a timestamp. The fallback is English."""
        env.post.last_active = utcnow()
        db.session.commit()

        assert env.post.posted_at_localized('active', 'not-a-locale') == \
            env.post.posted_at_localized('active', 'en')

    def test_the_active_sort_reads_last_active_rather_than_posted_at(self, env):
        """`sort == 'active'` picks a different column, which is what makes a bumped thread
        show as recent."""
        from datetime import timedelta

        env.post.posted_at = utcnow() - timedelta(days=400)
        env.post.last_active = utcnow()
        db.session.commit()

        assert env.post.posted_at_localized('active', 'en') != \
            env.post.posted_at_localized('hot', 'en')


class TestThePeertubeEmbedUrl:
    """A PeerTube post links to `/videos/watch/<id>`; the player is at
    `/videos/embed/<id>`. The template needs the second, and the only difference is one word.
    """

    def test_watch_becomes_embed(self, env):
        env.post.url = 'https://video.example/videos/watch/abc-123'
        db.session.commit()

        assert env.post.peertube_embed() == 'https://video.example/videos/embed/abc-123'

    def test_a_post_with_no_url_embeds_nothing(self, env):
        """`if self.url:` -- the function is called from a template branch chosen by post
        type, and `None.replace` would be a 500 on the page rather than a missing player."""
        env.post.url = None
        db.session.commit()

        assert env.post.peertube_embed() is None

    def test_only_the_videos_watch_segment_is_replaced(self, env):
        """R251, fixed (owner ruling). The replacement is anchored to the `/videos/watch/` path
        segment: it was `str.replace('watch', 'embed')`, which also rewrote a host or any other
        part of the URL carrying the word, pointing the player at a different server."""
        env.post.url = 'https://watch.example/videos/watch/abc?t=watch'

        assert env.post.peertube_embed() == 'https://watch.example/videos/embed/abc?t=watch'
