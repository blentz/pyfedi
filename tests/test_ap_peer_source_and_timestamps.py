"""Two things a peer puts in a document that were read as if they had to be there.

`markdown_source` and the five `parse_ap_timestamp` calls in
`app/activitypub/util.py`, plus the one in `app/models.py`'s `Post.new`.

D1346. Eleven sites preferred the markdown in an object's `source` over the HTML
in its `content`, in three spellings, and every one subscripted `content`
outright. `source` is optional in ActivityPub and its shape is the peer's choice,
so each read was a way to lose the whole object. Measured against `Post.new`:

    source={}                                KeyError: 'mediaType'
    source={'content': 'x'}                  KeyError: 'mediaType'
    source={'mediaType': 'text/markdown'}    KeyError: 'content'
    source={'mediaType': 'text/markdown',
            'content': 5}                    TypeError: expected string or
                                             bytes-like object, got 'int'

D1347. Five columns took a peer's `published` or `updated` string directly:
`User.created`, `Community.created_at`, `Community.last_active`,
`Feed.created_at`, `Feed.last_edit`. Same shape as D1330 (a poll's endTime) and
D1340 (a resolved post's published): a `DataError` at commit that also poisons
the transaction, so an actor whose document says `published: "whenever"` could
never be created here -- and nothing they ever posted could land either.
"""
from datetime import datetime

import pytest

from app import db
from app.models import Community, Feed, Site, User, markdown_source
from tests.app_source import app_trees


class TestTheMarkdownAPeerOffers:
    """The helper directly. It is the whole of the repair, so it is asserted here
    as well as through the paths that use it."""

    def test_a_proper_source(self):
        assert markdown_source({'source': {'mediaType': 'text/markdown',
                                           'content': '**hi**'}}) == '**hi**'

    @pytest.mark.parametrize('source', [
        {},
        {'content': 'x'},
        {'mediaType': 'text/markdown'},
        {'mediaType': 'text/html', 'content': 'x'},
        {'mediaType': 'text/markdown', 'content': None},
        {'mediaType': 'text/markdown', 'content': 5},
        {'mediaType': 'text/markdown', 'content': {'a': 1}},
        {'mediaType': 'text/markdown', 'content': ['x']},
        {'mediaType': ['text/markdown'], 'content': 'x'},
        {'mediaType': 5, 'content': 'x'},
        'a string',
        ['a list'],
        5,
        None,
    ])
    def test_what_offers_no_usable_markdown(self, source):
        assert markdown_source({'content': '<p>html</p>', 'source': source}) is None

    def test_an_object_with_no_source_at_all(self):
        assert markdown_source({'content': '<p>html</p>'}) is None

    @pytest.mark.parametrize('document', ['a string', ['a list'], 5, None])
    def test_something_that_is_not_an_object(self, document):
        assert markdown_source(document) is None

    def test_an_empty_string_is_markdown_the_peer_sent(self):
        """Not the same as absent: the peer said the body is empty, and `None`
        would make the caller fall back to the HTML instead."""
        assert markdown_source({'source': {'mediaType': 'text/markdown',
                                           'content': ''}}) == ''

    def test_a_missing_media_type_can_be_accepted_where_there_is_no_fallback(self):
        """`Feed(description=...)` has no HTML to fall back to -- its html comes
        from `summary` -- and accepted a `source` with no `mediaType` before this
        helper existed. The relaxation is a named argument rather than a second
        reading of the same field."""
        document = {'source': {'content': 'the *source* only'}}

        assert markdown_source(document) is None
        assert markdown_source(document, require_media_type=False) == 'the *source* only'

    def test_the_relaxation_still_refuses_a_stated_other_type(self):
        """`require_media_type=False` means "unstated is fine", not "any type is
        markdown"."""
        document = {'source': {'mediaType': 'text/html', 'content': '<p>x</p>'}}

        assert markdown_source(document, require_media_type=False) is None

    def test_the_relaxation_still_refuses_a_content_that_is_not_a_string(self):
        document = {'source': {'content': 5}}

        assert markdown_source(document, require_media_type=False) is None


@pytest.fixture
def env(app, api_baseline):
    from flask import g

    g.admin_ids = []
    g.site = db.session.get(Site, 1)
    return api_baseline


class TestAPostWhoseSourceIsUnusable:
    """Through `Post.new`, which is where the shape was measured. The post has to
    arrive: the peer offered markdown this instance cannot read, and the HTML in
    `content` is the fallback every one of these sites already had.
    """

    AUTHOR = 'https://remote.test/u/someone'
    PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'

    @pytest.fixture
    def author_and_community(self, env):
        from types import SimpleNamespace

        from tests.factories import (make_community, make_community_member,
                                     make_user)

        community = make_community('sourceland')
        author = make_user(env.instance_remote, 'sourcer')
        author.ap_id = 'sourcer@remote.test'
        author.ap_profile_id = self.AUTHOR
        author.ap_public_url = self.AUTHOR
        db.session.commit()
        make_community_member(author, community)
        return SimpleNamespace(community=community, author=author)

    def post(self, author_and_community, source, counter=[0]):
        from app.models import Post

        counter[0] += 1
        document = {'id': f'https://remote.test/p/{counter[0]}', 'type': 'Page',
                    'name': 'a post', 'attributedTo': self.AUTHOR,
                    'to': [self.PUBLIC], 'published': '2026-01-01T00:00:00Z',
                    'content': '<p>the html body</p>'}
        if source is not None:
            document['source'] = source
        return Post.new(author_and_community.author, author_and_community.community,
                        {'id': 'https://remote.test/c/1', 'type': 'Create',
                         'to': [self.PUBLIC], 'object': document})

    @pytest.mark.parametrize('source', [
        {},
        {'content': 'x'},
        {'mediaType': 'text/markdown'},
        {'mediaType': 'text/markdown', 'content': 5},
        {'mediaType': 'text/markdown', 'content': {'a': 1}},
        'a string',
        5,
    ])
    def test_the_post_arrives_with_the_html_body(self, app, author_and_community,
                                                 source):
        post = self.post(author_and_community, source)

        assert post is not None
        assert post.body == 'the html body'
        assert post.body_html == '<p>the html body</p>'

    def test_a_usable_source_is_still_preferred(self, app, author_and_community):
        post = self.post(author_and_community,
                         {'mediaType': 'text/markdown', 'content': 'the **markdown**'})

        assert post.body == 'the **markdown**'
        assert '<strong>markdown</strong>' in post.body_html

    def test_no_source_at_all(self, app, author_and_community):
        post = self.post(author_and_community, None)

        assert post.body == 'the html body'


class TestWhenAPeerSaysItWasCreated:
    """D1347, asserted on the three constructors through the models rather than
    through a fetch: the columns are what the DataError came out of.
    """

    def a_user(self, published):
        return User(user_name='someone', email='someone@remote.test',
                    created=published, ap_id='someone@remote.test')

    @pytest.mark.parametrize('published, expected', [
        ('2024-01-01T00:00:00Z', datetime(2024, 1, 1, 0, 0)),
        ('2024-01-01T00:00:00+05:00', datetime(2023, 12, 31, 19, 0)),
        ('2024-01-01T00:00:00-05:00', datetime(2024, 1, 1, 5, 0)),
    ])
    def test_a_readable_published_is_stored_as_utc(self, app, published, expected):
        from app.models import parse_ap_timestamp

        assert parse_ap_timestamp(published) == expected

    @pytest.mark.parametrize('published', ['whenever', '', '2024-13-45T99:99:99Z',
                                           5, [], {}, None, True])
    def test_an_unreadable_published_falls_back_to_now(self, app, published):
        """`parse_ap_timestamp(...) or utcnow()` is what each constructor does
        now, and this is the value it hands over."""
        from app.models import parse_ap_timestamp, utcnow

        before = utcnow()
        value = parse_ap_timestamp(published) or utcnow()

        assert value >= before

    def test_the_column_refuses_the_raw_string_which_is_the_defect(self, app,
                                                                   db_session):
        """What the constructors used to pass. Asserted rather than described,
        because the whole of D1347 is that this reaches the database.
        """
        import sqlalchemy.exc

        db.session.add(self.a_user('whenever'))
        with pytest.raises(sqlalchemy.exc.DataError):
            db.session.commit()
        db.session.rollback()

    def test_a_readable_string_is_accepted_by_the_column_but_loses_its_offset(
            self, app, db_session):
        """The other half of the defect, and the reason parsing is not merely
        defensive: PostgreSQL casts an offset-bearing string into `timestamp
        without time zone` by DISCARDING the offset, so a peer in +05:00 got a
        row five hours wrong. `parse_ap_timestamp` converts instead.
        """
        user = self.a_user('2024-01-01T00:00:00+05:00')
        db.session.add(user)
        db.session.commit()
        db.session.refresh(user)

        assert user.created == datetime(2024, 1, 1, 0, 0)  # the offset is gone

        from app.models import parse_ap_timestamp
        assert parse_ap_timestamp('2024-01-01T00:00:00+05:00') == \
            datetime(2023, 12, 31, 19, 0)


def code_lines(source, tree):
    """Every line of a source file that is really code, numbered from 1.

    Lines inside a string literal and lines carrying a comment are left out.
    Both matter for the two scans below: the production comments explaining these
    repairs quote the patterns they forbid, and so do the docstrings of the tests
    that do the forbidding (fact 706, extended to comments by a `#` in
    `app/utils.py:2633` that the AST walk alone did not cover).
    """
    import ast
    import io
    import tokenize

    skip = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            skip.update(range(node.lineno, node.end_lineno + 1))
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type == tokenize.COMMENT:
            skip.add(token.start[0])
    return [(number, line) for number, line in enumerate(source.splitlines(), 1)
            if number not in skip]


class TestNoPeerStringReachesADateTimeColumnUnparsed:
    """The property. D1330, D1340 and D1347 are the same defect found three
    times, so this is the scan that says whether a fourth is waiting.

    It looks for a peer document's key being assigned to a column in a
    constructor call or an attribute assignment without going through
    `parse_ap_timestamp`. String literals are excluded by walking the AST, not by
    skipping lines that start with `#`: the production comments and this
    docstring both quote the pattern they forbid (fact 706).
    """

    TIMESTAMP_KEYS = ('published', 'updated', 'endTime', 'startTime')
    SOURCES = ('activity_json', 'request_json', 'post_data', 'reply_data',
               'document')


    def test_nothing_assigns_a_peer_timestamp_without_parsing_it(self):
        from pathlib import Path

        offenders = []
        for path, source, tree in app_trees():
            for number, line in code_lines(source, tree):
                if 'parse_ap_timestamp' in line:
                    continue
                for name in self.SOURCES:
                    for key in self.TIMESTAMP_KEYS:
                        if f"{name}['{key}']" in line:
                            offenders.append(f'{path}:{number}: {line.strip()}')
        assert offenders == []


class TestNoPeerSourceIsSubscriptedDirectly:
    """The companion property for D1346. The eleven sites are gone; this is what
    says an twelfth has not appeared."""

    def test_nothing_reads_a_source_content_by_subscript(self):
        from pathlib import Path

        offenders = []
        for path, source, tree in app_trees():
            for number, line in code_lines(source, tree):
                if "['source'][" not in line:
                    continue
                # Writing INTO our own outgoing document is not reading a peer's:
                # `comment_model_to_json` builds `source` itself a few lines above.
                if line.strip().startswith("reply_data['source']['content'] ="):
                    continue
                offenders.append(f'{path}:{number}: {line.strip()}')
        assert offenders == []


class TestACommunityAndAFeedGetTheSameTreatment:
    """The two constructors that take two timestamps each, so an unreadable
    `updated` cannot be masked by a readable `published`."""

    def test_a_community_takes_both(self, app, db_session):
        from app.models import parse_ap_timestamp, utcnow

        before = utcnow()
        community = Community(
            name='news', title='News',
            created_at=parse_ap_timestamp('2024-01-01T00:00:00Z') or utcnow(),
            last_active=parse_ap_timestamp('whenever') or utcnow(),
            ap_id='news@remote.test')
        db.session.add(community)
        db.session.commit()

        assert community.created_at == datetime(2024, 1, 1, 0, 0)
        assert community.last_active >= before

    def test_a_feed_takes_both(self, app, db_session, api_baseline):
        from app.models import parse_ap_timestamp, utcnow

        before = utcnow()
        feed = Feed(name='newsfeed', title='News', machine_name='newsfeed',
                    user_id=api_baseline.user1.id, num_communities=0,
                    created_at=parse_ap_timestamp('not a date') or utcnow(),
                    last_edit=parse_ap_timestamp('2024-01-01T00:00:00Z') or utcnow(),
                    ap_id='newsfeed@remote.test')
        db.session.add(feed)
        db.session.commit()

        assert feed.created_at >= before
        assert feed.last_edit == datetime(2024, 1, 1, 0, 0)


class TestTheThreeConstructorsThemselves:
    """D1347 and D1346 through `actor_json_to_model`, the function that holds all
    five timestamp reads and two of the eleven source reads.

    Asserted through this function rather than through the columns alone because
    the defect is that the constructor is never reached: the DataError comes out
    of the commit that follows, and the actor, community or feed is simply never
    created. A remote instance whose actor document says `published: "whenever"`
    could not federate with this one at all.
    """

    PEER = 'peer.example'

    @pytest.fixture
    def peer(self, app, db_session, http_mock):
        from tests.factories import peer_instance

        peer_instance(self.PEER)
        return self.PEER

    def person(self, published=None, **fields):
        from tests.factories import peer_actor_json

        merged = {'inbox': f'https://{self.PEER}/u/alice/inbox'}
        if published is not None:
            merged['published'] = published
        merged.update(fields)
        return peer_actor_json('Person', name='alice', server=self.PEER,
                               fields=merged)

    def group(self, published=None, updated=None, **fields):
        from tests.factories import peer_actor_json

        merged = {}
        if published is not None:
            merged['published'] = published
        if updated is not None:
            merged['updated'] = updated
        merged.update(fields)
        return peer_actor_json('Group', name='memes', server=self.PEER,
                               fields=merged)

    @pytest.mark.parametrize('published', ['whenever', '', '2024-13-45T99:99:99Z',
                                           5, [], {}, None])
    def test_an_actor_whose_published_is_unreadable_is_still_created(
            self, peer, http_mock, published):
        from app.activitypub.util import actor_json_to_model

        document = self.person()
        document['published'] = published

        user = actor_json_to_model(document, 'alice', self.PEER)

        assert isinstance(user, User)
        assert user.created is not None

    def test_an_actors_readable_published_is_converted_to_utc(self, peer, http_mock):
        from app.activitypub.util import actor_json_to_model

        user = actor_json_to_model(self.person(published='2024-01-01T00:00:00+05:00'),
                                   'alice', self.PEER)

        assert user.created == datetime(2023, 12, 31, 19, 0)

    @pytest.mark.parametrize('published, updated', [
        ('whenever', '2024-01-01T00:00:00Z'),
        ('2024-01-01T00:00:00Z', 'whenever'),
        ('whenever', 'whenever'),
        (5, []),
    ])
    def test_a_community_survives_either_timestamp_being_unreadable(
            self, peer, http_mock, published, updated):
        """Two columns, so a readable `published` must not mask an unreadable
        `updated`: `last_active` is NOT NULL and orders every listing."""
        from app.activitypub.util import actor_json_to_model

        community = actor_json_to_model(self.group(published=published,
                                                   updated=updated),
                                        'memes', self.PEER)

        assert isinstance(community, Community)
        assert community.created_at is not None
        assert community.last_active is not None

    def test_a_communitys_readable_timestamps_are_converted(self, peer, http_mock):
        from app.activitypub.util import actor_json_to_model

        community = actor_json_to_model(
            self.group(published='2024-01-01T00:00:00+05:00',
                       updated='2024-06-01T12:00:00+02:00'),
            'memes', self.PEER)

        assert community.created_at == datetime(2023, 12, 31, 19, 0)
        assert community.last_active == datetime(2024, 6, 1, 10, 0)

    def test_an_actors_source_markdown_wins_over_its_summary_html(self, peer,
                                                                 http_mock):
        """The `about` half of D1346: a peer sends both, and the markdown is what
        this instance edits and re-serves, so it is preferred."""
        from app.activitypub.util import actor_json_to_model

        document = self.person(summary='<p>the html</p>',
                               source={'mediaType': 'text/markdown',
                                       'content': 'the **markdown**'})

        user = actor_json_to_model(document, 'alice', self.PEER)

        assert user.about == 'the **markdown**'
        assert '<strong>markdown</strong>' in user.about_html

    @pytest.mark.parametrize('source', [
        {},
        {'content': 'x'},
        {'mediaType': 'text/markdown'},
        {'mediaType': 'text/markdown', 'content': 5},
        'a string',
    ])
    def test_an_actor_whose_source_is_unusable_keeps_the_summary(self, peer,
                                                                http_mock, source):
        """The fallback, which is what a document with no `source` has always
        done -- and what the eleven hand-written reads raised instead of doing."""
        from app.activitypub.util import actor_json_to_model

        document = self.person(summary='<p>the html</p>', source=source)

        user = actor_json_to_model(document, 'alice', self.PEER)

        assert isinstance(user, User)
        assert user.about_html == '<p>the html</p>'
        assert user.about == 'the html'

    def test_a_communitys_source_markdown_wins_over_its_summary_html(self, peer,
                                                                    http_mock):
        from app.activitypub.util import actor_json_to_model

        document = self.group(summary='<p>the html</p>',
                              source={'mediaType': 'text/markdown',
                                      'content': 'the **markdown**'})

        community = actor_json_to_model(document, 'memes', self.PEER)

        assert community.description == 'the **markdown**'

    @pytest.mark.parametrize('source', [{}, {'content': 'x'},
                                        {'mediaType': 'text/markdown'},
                                        {'mediaType': 'text/markdown', 'content': 5}])
    def test_a_community_whose_source_is_unusable_keeps_the_summary(self, peer,
                                                                   http_mock, source):
        from app.activitypub.util import actor_json_to_model

        document = self.group(summary='<p>the html</p>', source=source)

        community = actor_json_to_model(document, 'memes', self.PEER)

        assert isinstance(community, Community)
        assert community.description_html == '<p>the html</p>'
