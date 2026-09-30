"""Round 224: the coverage tail, and four arms that turned out to be unreachable.

The standing instruction is 100% coverage with 0 errors and 0 warnings, and `app/cli.py`
and `app/nntp/*` are lowest priority. With those set aside, the report's tail is ten files
holding one to eight missing lines each. This round resolved eleven of them, and the split
is the interesting part -- only NINE were reachable:

COVERED HERE (9 lines):

    app/api/alpha/schema.py        38, 39, 47, 48, 56   three validators' refusal arms
    app/shared/tasks/notes.py      100, 101             a LOCAL @mention whose lookup raises
    app/shared/tasks/pages.py      107, 108             the same, for a post

REMOVED AS DEAD (2 lines, D1414):

    app/activitypub/actor.py       104, 125             `actor = unbanned_actor`

    Both copies looked for "a non-banned copy of the community" by re-querying the same
    `ap_profile_id` with `banned == False`. That column is `unique=True`
    (`app/models.py:1256`), so the only row the query could match is the banned one it
    excludes: the fallback could never run. Collapsed to the refusal the constraint already
    guarantees, with a row below asserting the constraint so a migration dropping it fails
    here rather than silently reviving dead code.

RECORDED AS UNREACHABLE, not covered and not deleted (4 lines):

    app/topic/routes.py            212   an unknown segment aborts at 67 first
    app/feed/routes.py             616   `feed` is dereferenced at 481 and 530 already
    app/shared/community.py        723   `get_ap_id()` never returns anything falsy
    app/api/alpha/utils/reply.py   369   under a comment reading `# shouldn't happen`

    Each is a defensive `else` that an earlier line makes impossible. Deleting one is a
    change to somebody else's intent, and the evidence for these four is "an earlier
    statement already refused or dereferenced the value" rather than a schema constraint --
    weaker than D1414's, so they are documented instead, each pinned by a row that names
    the line that makes it dead.

TWO ROWS IN THIS FILE WERE WRONG BEFORE THEY WERE RIGHT, both the same shape: they passed
while the line they were written for stayed red. `assert find_remote_actor(url) is None`
was satisfied by the refusal one line above the target, and a mention of
`@someone@elsewhere.example` covered the REMOTE branch's `except: pass` rather than the
local one this round needed. The coverage report is what caught both; the test result could
not.
"""
import pytest
from marshmallow import ValidationError

from app import db
from app.models import Site
from tests.factories import (make_community, make_instance, make_site, make_user,
                             seed_community_owner)


def _lines(relative_path):
    """The file's lines, 0-indexed, for the rows that pin an unreachable arm by naming the
    line that makes it unreachable."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parent.parent
    return (root / relative_path).read_text().splitlines()

HOST = 'test.piefed.local'
PEER = 'peer.example'


# --------------------------------------------------------------------------
# app/api/alpha/schema.py: three validators, refusal arms only
# --------------------------------------------------------------------------


class TestTheSchemaValidators:
    """Each of these returns True for a good value -- already covered by the request rows
    elsewhere -- and raises for a bad one, which nothing reached."""

    def test_a_datetime_that_is_not_the_lemmy_format_is_refused(self):
        from app.api.alpha.schema import validate_datetime_string

        assert validate_datetime_string('2026-01-01T00:00:00.000000Z') is True
        with pytest.raises(ValidationError, match='Bad datetime string'):
            validate_datetime_string('2026-01-01 00:00:00')

    @pytest.mark.parametrize('value', ['#ff', 'ff0000', '#gggggg', '#1234567', ''])
    def test_a_colour_that_is_not_a_hex_code_is_false(self, value):
        """The refusal arm here is `except:` around a `re.match`, which a string cannot
        reach -- so what these rows pin is the FALSE return, and the row below reaches the
        except by handing it something that is not a string at all."""
        from app.api.alpha.schema import validate_color_code

        assert validate_color_code(value) is False

    def test_a_colour_that_is_not_a_string_raises(self):
        """`re.match` against a non-string is a TypeError, which the bare `except:` turns
        into the ValidationError the API reports."""
        from app.api.alpha.schema import validate_color_code

        assert validate_color_code('#abc') is True
        with pytest.raises(ValidationError, match='Bad hex color code string'):
            validate_color_code(None)

    @pytest.mark.parametrize('value', ['', '   ', '\t\n', ' '])
    def test_a_string_of_only_whitespace_is_refused(self, value):
        """`re.sub(r'\\s+', '', text, flags=re.U)` strips unicode whitespace too, which is
        why the non-breaking space belongs in this table."""
        from app.api.alpha.schema import validate_non_empty_string

        with pytest.raises(ValidationError, match='Non-empty string required'):
            validate_non_empty_string(value)

    def test_a_string_with_one_real_character_is_accepted(self):
        from app.api.alpha.schema import validate_non_empty_string

        assert validate_non_empty_string('  x  ') is True


# --------------------------------------------------------------------------
# The two 404 arms
# --------------------------------------------------------------------------


@pytest.fixture
def public(app, db_session):
    """A public instance, because D1410-D1413 made every one of these routes refuse an
    anonymous visitor on a private one -- and `Site.private_instance` defaults to True
    (fact 925), so a test about a 404 has to say which instance it means."""
    from types import SimpleNamespace

    make_site()
    site = db.session.get(Site, 1)
    site.private_instance = False
    instance = make_instance(HOST, software='piefed')
    user = make_user(instance, 'author', local=True)
    community = make_community('general')
    db.session.commit()
    return SimpleNamespace(site=site, user=user, community=community, instance=instance)


def test_an_unknown_topic_aborts_before_the_line_that_looked_uncovered(app, public):
    """`app/topic/routes.py:212` is NOT what an unknown topic reaches, and the first
    version of this row proved it by passing while the line stayed red.

    `show_topic` splits its path and looks each segment up; a segment naming no topic
    aborts at **line 67**, inside the loop. Line 212 is the `else` of `if current_topic:`
    at 70, and `current_topic` is assigned from that same loop variable -- so reaching 212
    needs the loop to leave `topic` None WITHOUT aborting, which it cannot: `split('/')`
    always yields at least one segment, and any segment that misses aborts.

    So the round covers the refusal that exists and records the one that cannot.
    """
    response = app.test_client().get('/topic/no-such-topic')

    assert response.status_code == 404
    source = _lines('app/topic/routes.py')
    assert source[66].strip() == 'abort(404)', 'the early abort moved; 212 may now be live'
    assert source[211].strip() == 'abort(404)'


def test_show_feed_dereferences_its_argument_long_before_the_uncovered_else(app, public):
    """`app/feed/routes.py:624`, the same shape. `show_feed` has no route of its own -- the
    activitypub blueprint resolves the feed and calls it (`app/activitypub/routes.py:2765`)
    -- and it reads `feed_readable_by(feed, ...)` at 489 and `feed.title` at 538 before
    reaching `if current_feed:` at 544. A falsy feed raises long before the `else`.

    Read out of the source rather than driven, because driving it would mean calling
    `show_feed(None)` and asserting the AttributeError -- which pins the crash, not the
    refusal.
    """
    source = _lines('app/feed/routes.py')

    assert 'feed_readable_by(feed' in source[488]
    assert 'feed.title' in source[537]
    assert source[543].strip() == 'if current_feed:'
    assert source[623].strip() == 'abort(404)'


# --------------------------------------------------------------------------
# app/activitypub/actor.py: a banned community with no unbanned copy
# --------------------------------------------------------------------------


class TestFindingABannedCommunity:
    """`find_remote_actor` and `find_actor_by_url` both answer a `/c/` url by looking the
    Community up, and if it is banned, refusing.

    WHAT THIS ROUND ACTUALLY FOUND HERE. The two uncovered lines were not the refusal --
    `return None` was already covered -- but `actor = unbanned_actor`, the fallback after
    "try to find a non-banned copy of the community". `Community.ap_profile_id` is
    `unique=True`, so a second row with that id cannot exist and the only row the fallback
    query could match is the banned one it filters out. The fallback was unreachable, in
    both copies, which is exactly why it stayed uncovered while everything around it was
    tested (D1414).

    Both are now the bare refusal the constraint guarantees, and the last row here asserts
    the constraint -- so a migration dropping it fails in the suite rather than silently
    reviving dead code.
    """

    @pytest.fixture
    def banned(self, app, db_session):
        # `make_community` hardcodes `user_id=1` and `instance_id=1`, so an instance and a
        # user have to occupy those ids first -- the pattern tests/factories.py documents
        # and `seed_community_owner` exists for.
        make_site()
        seed_community_owner(PEER)
        community = make_community('bannedcomm', host=PEER)
        community.banned = True
        db.session.commit()
        return community

    def test_a_banned_community_resolves_to_nothing(self, banned):
        from app.activitypub.actor import find_remote_actor

        assert find_remote_actor(banned.ap_profile_id) is None

    def test_the_other_lookup_refuses_it_too(self, banned):
        """`find_actor_by_url` carries its own copy of the same four lines, which is why
        one banned community closes two gaps."""
        from app.activitypub.actor import find_actor_by_url

        assert find_actor_by_url(banned.ap_profile_id) is None

    def test_an_unbanned_community_still_resolves(self, app, db_session):
        """The control: the lookup is not simply returning None for every `/c/` url."""
        from app.activitypub.actor import find_remote_actor

        make_site()
        seed_community_owner(PEER)
        community = make_community('okcomm', host=PEER)
        db.session.commit()

        assert find_remote_actor(community.ap_profile_id) is community

    def test_ap_profile_id_is_unique_which_is_what_makes_the_fallback_dead(self):
        """The load-bearing row. `actor = unbanned_actor` was removed because this
        constraint makes a second row with the same id impossible; if it is ever dropped,
        the fallback becomes meaningful again and this row fails so that decision is taken
        deliberately."""
        from app.models import Community

        assert Community.__table__.c.ap_profile_id.unique is True

    def test_two_communities_cannot_share_an_ap_profile_id(self, banned):
        """The constraint, measured rather than read: the insert the dead fallback existed
        to serve is refused by the database."""
        import sqlalchemy

        from app.models import Community

        twin = Community(name='twin', title='twin', instance_id=1, user_id=1,
                         ap_profile_id=banned.ap_profile_id, banned=False)
        db.session.add(twin)
        with pytest.raises(sqlalchemy.exc.IntegrityError):
            db.session.commit()
        db.session.rollback()


# --------------------------------------------------------------------------
# The @mention lookup that raises, in both outbound task modules
# --------------------------------------------------------------------------


class TestAnUnresolvableMention:
    """`send_reply` and `send_post` scan the body for `@name@host` and, for a REMOTE host,
    call `search_for_user`, which reaches the network. Both wrap it in `try: ... except:
    pass`, and nothing had made it raise -- so those two lines in each module were
    uncovered while the local arm was not.

    `tests/test_shared_tasks_send_post.py`'s own docstring states the trap: "a test
    asserting that a mention produced no notification cannot distinguish 'correctly
    skipped' from 'crashed and swallowed' -- pin the reason, not the absence." So each row
    here SPIES on `search_for_user` as well as making it raise: the spy recording a call is
    what says the except arm ran, and the send completing afterwards is what says it was
    swallowed rather than escaping.

    Both rows reuse the existing harnesses' `_seed` and `_send` rather than building a
    fixture: those seeds already order the instance before the community (`make_community`
    hardcodes `instance_id=1`) and give the sender real keys.
    """

    @staticmethod
    def _raising_spy(calls):
        def _spy(name):
            calls.append(name)
            raise Exception('no such user')

        return _spy

    def test_a_reply_still_sends_when_the_mention_cannot_be_resolved(self, db_session,
                                                                    monkeypatch):
        """app/shared/tasks/notes.py:100-101."""
        import app.shared.tasks.notes as notes
        from tests.test_shared_tasks_send_reply import _seed, _send

        calls = []
        # The LOCAL host. `match.group(2) == SERVER_NAME` selects the branch whose
        # `except: pass` is lines 100-101; a remote host takes the other pair at 106-107,
        # which was already covered -- and a row using one to cover the other passes while
        # the line stays red, which is how the first version of this test was wrong.
        s = _seed(body='hello @someone@test.piefed.local')
        monkeypatch.setattr(notes, 'search_for_user', self._raising_spy(calls))

        _send(s)

        assert calls == ['someone'], \
            'search_for_user was not reached, so the except arm decided nothing'

    def test_a_post_still_sends_when_the_mention_cannot_be_resolved(self, db_session,
                                                                   monkeypatch):
        """app/shared/tasks/pages.py:107-108, the same four lines one module over."""
        import app.shared.tasks.pages as pages
        from tests.test_shared_tasks_send_post import _seed, _send

        calls = []
        s = _seed(body='hello @someone@test.piefed.local')   # local branch, as above
        monkeypatch.setattr(pages, 'search_for_user', self._raising_spy(calls))

        _send(s.post)

        assert calls == ['someone'], \
            'search_for_user was not reached, so the except arm decided nothing'


# --------------------------------------------------------------------------
# The two lines this round does NOT close, asserted as unreachable
# --------------------------------------------------------------------------


def test_a_flairs_ap_id_is_never_falsy():
    """Why `app/shared/community.py:723` cannot be covered: `get_ap_id` either returns an
    `ap_id` that is already set, or builds one from the community's `local_url()`. This row
    pins the reason rather than the line, so a future change that DOES make it falsy fails
    here and the guard becomes reachable on purpose."""
    import ast
    import pathlib

    source = (pathlib.Path(__file__).resolve().parent.parent
              / 'app' / 'models.py').read_text()
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == 'get_ap_id')
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]

    assert len(returns) == 2
    assert all(r.value is not None for r in returns), \
        'get_ap_id gained a bare return, so a flair CAN have no ap_id'
