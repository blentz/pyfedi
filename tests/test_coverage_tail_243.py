"""Round 243: the un-moderated escalation, an Accept this instance sends, and the site dict.

Three clusters in `app/activitypub/util.py`.

    4152-4166   a reported COMMENT in a local community nobody moderates escalates to the
                site admins. The post branch above it has rows; the reply branch's
                escalation did not.
    4182-4205   `process_quote_boost` -- when a peer asks to quote one of our posts, this
                instance signs an Accept and sends it back. It is an outbound activity
                signed with a local user's key, so who it goes to and who signs it are the
                two things worth pinning.
    4527-4560   the site dict's language and custom-emoji lists, which every peer and every
                client reads.

The escalation matters because `un_moderated` is the flag that says nobody is watching a
community: without it a report there is filed and notified to an empty list of moderators,
and nothing else happens.
"""
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from flask import g

from app import db
from app.activitypub.util import process_quote_boost, process_report
from app.constants import NOTIF_REPORT
from app.models import (Community, Emoji, Instance, Language, Notification, Post,
                        PostReply, Report, Site, User)
from tests.factories import (make_community, make_community_member, make_instance,
                             make_post, make_post_reply, make_user)


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    g.site.private_instance = False
    return SimpleNamespace(app=app, site=g.site, baseline=api_baseline)


# --------------------------------------------------------------------------
# A report in a community nobody moderates
# --------------------------------------------------------------------------


class TestReportingACommentNobodyModerates:
    """`un_moderated` marks a local community with no active moderators. A comment reported
    there notifies the SITE admins as well, because the moderator loop above has nobody to
    notify.
    """

    @pytest.fixture
    def seeded(self, env):
        community = make_community('quietland')
        community.un_moderated = True
        author = make_user(env.baseline.instance_local, 'commentauthor', local=True)
        admin = make_user(env.baseline.instance_local, 'siteadmin', local=True)
        reporter = make_user(env.baseline.instance_remote, 'reporter')
        reporter.ap_id = 'reporter@remote.test'
        db.session.commit()
        make_community_member(author, community)
        post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        reply = make_post_reply(post, author, body='the reported comment')
        reply.ap_id = 'https://test.piefed.local/comment/1'
        db.session.commit()
        g.admin_ids = [admin.id]
        env.community = community
        env.post = post
        env.reply = reply
        env.admin = admin
        env.reporter = reporter
        return env

    def _report(self, env, reported=None):
        return process_report(env.reporter, reported or env.reply,
                              {'summary': 'spam'}, db.session)

    def test_the_admins_are_told_about_a_comment_nobody_moderates(self, seeded):
        """The escalation. The subtype is `comment_reported`, not `user_reported` -- D1392
        fixed that mislabelling here, and the row pins it, because the notification template
        renders a different block per subtype."""
        assert self._report(seeded) is True

        notification = Notification.query.filter_by(user_id=seeded.admin.id).one()
        assert notification.subtype == 'comment_reported'
        assert notification.notif_type == NOTIF_REPORT
        assert notification.url.startswith('/admin/report')

    def test_the_admin_notification_counter_goes_up(self, seeded):
        """`admin.unread_notifications += 1`. The badge is what an admin actually sees, so
        a notification row without the counter is a report nobody notices."""
        before = seeded.admin.unread_notifications or 0

        self._report(seeded)

        db.session.refresh(seeded.admin)
        assert seeded.admin.unread_notifications == before + 1

    def test_a_moderated_community_does_not_escalate(self, seeded):
        """The `and un_moderated` half. A community with moderators has already been
        notified through the loop above, and telling the admins as well would make every
        report a site-wide alert."""
        seeded.community.un_moderated = False
        db.session.commit()

        self._report(seeded)

        assert Notification.query.filter_by(user_id=seeded.admin.id).count() == 0

    def test_a_remote_community_does_not_escalate(self, seeded):
        """The `is_local()` half. A remote community's moderators are on its own instance;
        escalating to OUR admins would make them responsible for content they do not
        host."""
        seeded.community.ap_id = 'quietland@remote.test'
        seeded.community.ap_profile_id = 'https://remote.test/c/quietland'
        db.session.commit()

        self._report(seeded)

        assert Notification.query.filter_by(user_id=seeded.admin.id).count() == 0

    def test_the_report_is_filed_and_counted_either_way(self, seeded):
        """The lines below the escalation. `reported.reports += 1` is what the moderation
        UI sorts on, so it must happen whether or not anybody was notified."""
        seeded.community.un_moderated = False
        db.session.commit()

        assert self._report(seeded) is True

        db.session.refresh(seeded.reply)
        assert seeded.reply.reports == 1
        assert Report.query.filter_by(suspect_post_reply_id=seeded.reply.id).count() == 1

    def test_a_comment_exempt_from_reports_records_nothing(self, seeded):
        """`reports == -1` marks a target as exempt. The caller must not log a success
        either, which is what the False return is for (D1401)."""
        seeded.reply.reports = -1
        db.session.commit()

        assert self._report(seeded) is False
        assert Report.query.count() == 0


# --------------------------------------------------------------------------
# Accepting a quote request
# --------------------------------------------------------------------------


class TestAcceptingAQuoteRequest:
    """FEP-044f: a peer that wants to quote one of our posts sends a QuoteRequest, and this
    instance answers with a signed Accept. The Accept is an outbound, signed activity, so
    the rows pin who signs it, where it goes, and when none is sent at all.
    """

    @pytest.fixture
    def seeded(self, env):
        community = make_community('quoteland')
        author = make_user(env.baseline.instance_local, 'quotedauthor', local=True)
        author.private_key = 'a private key'
        peer = make_instance('quoter.example')
        peer.inbox = 'https://quoter.example/inbox'
        quoter = make_user(peer, 'thequoter')
        quoter.ap_id = 'thequoter@quoter.example'
        quoter.ap_profile_id = 'https://quoter.example/u/thequoter'
        db.session.commit()
        make_community_member(author, community)
        post = make_post(community, author, ap_id='https://test.piefed.local/p/1')
        db.session.commit()
        env.post = post
        env.author = author
        env.quoter = quoter
        env.peer = peer
        env.community = community
        return env

    def _activity(self, seeded):
        return {'type': 'QuoteRequest',
                'id': 'https://quoter.example/activities/1',
                'actor': seeded.quoter.ap_profile_id,
                'object': seeded.post.ap_id,
                'instrument': {'id': 'https://quoter.example/p/9'}}

    def _boost(self, seeded, post_ap=None):
        sent = []
        with patch('app.activitypub.util.find_actor_or_create_cached',
                   return_value=seeded.quoter), \
                patch('app.activitypub.util.send_post_request',
                      side_effect=lambda *args, **kwargs: sent.append((args, kwargs))):
            process_quote_boost(self._activity(seeded),
                                post_ap if post_ap is not None else seeded.post.ap_id,
                                'https://quoter.example/p/9')
        return sent

    def test_an_accept_is_sent_to_the_quoters_inbox_signed_by_the_author(self, seeded):
        """Three claims in one row, because they are one decision: the Accept goes to the
        quoter's INSTANCE inbox, it is signed with the quoted post's author's private key,
        and the key is theirs rather than the instance's."""
        sent = self._boost(seeded)

        assert len(sent) == 1
        args, _kwargs = sent[0]
        assert args[0] == 'https://quoter.example/inbox'
        assert args[2] == 'a private key'

    def test_the_accept_names_the_request_it_answers(self, seeded):
        """`"object": core_activity`. An Accept that did not carry the original activity
        would leave the peer unable to tell which request was granted."""
        args, _kwargs = self._boost(seeded)[0]
        accept = args[1]

        assert accept['type'] == 'Accept'
        assert accept['object'] == self._activity(seeded)
        assert accept['actor'] == seeded.author.public_url()
        assert accept['to'] == seeded.quoter.ap_profile_id

    def test_the_accept_carries_a_stamped_authorisation_url(self, seeded):
        """`result` is the FEP-044f authorisation link, and it is built by percent-encoding
        a stamp -- so it must be a URL on THIS instance rather than the peer's."""
        args, _kwargs = self._boost(seeded)[0]

        assert args[1]['result'].startswith(
            f"{seeded.app.config['SERVER_URL']}/quote_boost_auth?stamp=")

    def test_a_quote_of_a_reply_is_answered_too(self, seeded):
        """`Post.get_by_ap_id` then `PostReply.get_by_ap_id`: the thing being quoted may be
        a comment, and the second lookup is what finds it."""
        reply = make_post_reply(seeded.post, seeded.author, body='a quoted comment')
        reply.ap_id = 'https://test.piefed.local/comment/1'
        db.session.commit()

        sent = self._boost(seeded, post_ap=reply.ap_id)

        assert len(sent) == 1

    def test_nothing_is_sent_for_a_post_this_instance_does_not_have(self, seeded):
        """Both lookups miss. The id comes from the peer, so this is the ordinary case of a
        QuoteRequest naming something we never stored."""
        assert self._boost(seeded, post_ap='https://quoter.example/p/nothing') == []

    def test_nothing_is_sent_for_a_remote_authors_post(self, seeded):
        """`post.author.is_local()`. Only the author can grant a quote of their own post,
        so a post this instance merely mirrors is not ours to accept."""
        seeded.author.ap_id = 'quotedauthor@remote.test'
        seeded.author.ap_profile_id = 'https://remote.test/u/quotedauthor'
        db.session.commit()

        assert self._boost(seeded) == []

    def test_nothing_is_sent_when_the_quoters_instance_has_no_inbox(self, seeded):
        """`if to and to.instance.inbox`. An Instance row with no inbox is a peer this
        instance has never successfully fetched, and `send_post_request(None, ...)` would
        be a request to nowhere."""
        seeded.peer.inbox = None
        db.session.commit()

        assert self._boost(seeded) == []


# --------------------------------------------------------------------------
# The site dict's languages and emojis
# --------------------------------------------------------------------------


class TestWhatTheSiteDictPublishes:
    """`lemmy_site_data()` builds the `/api/v3/site` payload every Lemmy-compatible client
    reads. Two of its lists had no rows.
    """

    @pytest.fixture
    def seeded(self, env):
        for code, name in (('und', 'Undetermined'), ('en', 'English'),
                           ('fr', 'French')):
            if Language.query.filter_by(code=code).first() is None:
                db.session.add(Language(code=code, name=name))
        db.session.commit()
        return env

    def _dict(self):
        """`lemmy_site_data()` -- the `/api/v3/site` payload, which is what a Lemmy client
        reads to learn this instance's languages and emojis."""
        from app.activitypub.util import lemmy_site_data

        return lemmy_site_data()

    def test_every_language_is_listed_but_only_two_are_discussion_languages(self, seeded):
        """`all_languages` is the whole table; `discussion_languages` is hardcoded to
        `und` and `en` with a comment saying it should be an admin setting. The row pins
        both, so the day it becomes a setting this says what changed."""
        data = self._dict()

        codes = {entry['code'] for entry in data['all_languages']}
        assert {'und', 'en', 'fr'} <= codes

        discussion = set(data['discussion_languages'])
        english = Language.query.filter_by(code='en').one()
        french = Language.query.filter_by(code='fr').one()
        assert english.id in discussion
        assert french.id not in discussion

    def test_a_local_emoji_is_published_with_its_shortcode_and_keywords(self, seeded):
        """The colons come off the token, and the space-separated aliases become one
        keyword object each -- which is the shape clients read."""
        db.session.add(Emoji(token=':partyparrot:', url='https://test.piefed.local/e.png',
                             category='fun', aliases='party parrot', instance_id=1))
        db.session.commit()

        emojis = self._dict()['custom_emojis']

        entry = next(e for e in emojis
                     if e['custom_emoji']['shortcode'] == 'partyparrot')
        assert entry['custom_emoji']['image_url'] == 'https://test.piefed.local/e.png'
        assert entry['custom_emoji']['category'] == 'fun'
        assert entry['keywords'] == [{'keyword': 'party'}, {'keyword': 'parrot'}]

    def test_an_emoji_with_no_aliases_or_category_still_publishes(self, seeded):
        """Both `if`s. `category` is rendered as an empty string rather than None, and
        `None.split()` would be an AttributeError on a field an admin need not fill in."""
        db.session.add(Emoji(token=':plain:', url='https://test.piefed.local/p.png',
                             instance_id=1))
        db.session.commit()

        entry = next(e for e in self._dict()['custom_emojis']
                     if e['custom_emoji']['shortcode'] == 'plain')

        assert entry['keywords'] == []
        assert entry['custom_emoji']['category'] == ''

    def test_a_remote_emoji_is_not_published_as_ours(self, seeded):
        """`filter_by(instance_id=1)`. Emojis arrive from peers with their posts, and
        publishing them in our own site dict would claim somebody else's."""
        peer = make_instance('emoji.example')
        db.session.commit()
        db.session.add(Emoji(token=':theirs:', url='https://emoji.example/e.png',
                             instance_id=peer.id))
        db.session.commit()

        shortcodes = {e['custom_emoji']['shortcode']
                      for e in self._dict()['custom_emojis']}

        assert 'theirs' not in shortcodes
