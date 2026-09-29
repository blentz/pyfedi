"""`actor_contains_blocked_words` and `actor_profile_contains_blocked_words`: the
admin's blocked-word lists.

D1388. Both split the setting on newlines, lowercase and strip each entry, and
test it with `in` -- and neither skipped an **empty** entry. `'' in anything` is
True, so one blank line blocked every actor. A textarea submits `'spam\\r\\n'` for
one word and an Enter, and `'\\r'.strip()` is `''`, so the most ordinary way to
fill the setting was enough. Measured against an actor named `innocent` with an
ordinary bio:

    'spam'          not blocked   (correct)
    'spam\\n'        BLOCKED
    'spam\\r\\n'      BLOCKED
    '\\nspam'        BLOCKED
    'spam\\n\\nscam'  BLOCKED
    '  \\n spam'     BLOCKED

WHY THAT WAS A SILENT OUTAGE. Three callers:

  app/activitypub/actor.py:72    `find_actor_or_create` -- no remote actor
                                 resolves at all, so federation stops
  app/activitypub/actor.py:80    the same for any User with a bio
  app/auth/util.py:219           no local registration succeeds
  app/auth/oauth_util.py:277     no OAuth signup succeeds

None of them logs the reason, so the symptom is "federation and signups quietly
stopped working" after an admin typed one word into a settings box.

`blocked_phrases()` in the same module already skipped empty entries with
`if phrase != ''`, so the correct idiom was one screen away. The sweep for this
shape found fourteen `split('\\n')` sites; the other twelve are safe and this file
records why, so a later round does not re-derive it: the two country checks use
`==` rather than `in` and guard on a truthy input, `auto_decline_referrers` and
`Filter.keywords_string` filter empties already, and the admin-form sites build
rows rather than making a match decision.
"""
import pytest

from app import db
from app.models import Site
from app.utils import (actor_contains_blocked_words,
                       actor_profile_contains_blocked_words, set_setting)
from tests.factories import make_instance, make_user

pytestmark = pytest.mark.usefixtures('site')

# Every spelling of "one word, plus whatever a textarea adds".
EMPTY_ENTRY_SETTINGS = ['spam\n', 'spam\r\n', '\nspam', 'spam\n\nscam',
                        '  \n spam', 'spam\n   \n', '\n\nspam\n\n']


@pytest.fixture
def actor(app, db_session):
    instance = make_instance('peer.blocked.test')
    user = make_user(instance, 'innocent')
    user.about_html = '<p>a perfectly ordinary bio</p>'
    db.session.commit()
    return user


def block(names=None, bios=None):
    if names is not None:
        set_setting('actor_blocked_words', names)
    if bios is not None:
        set_setting('actor_bio_blocked_words', bios)


# --------------------------------------------------------------------------
# D1388: an empty entry must not match
# --------------------------------------------------------------------------


class TestAnEmptyEntryInTheNameList:
    @pytest.mark.parametrize('setting', EMPTY_ENTRY_SETTINGS)
    def test_an_innocent_actor_is_not_blocked(self, app, actor, setting):
        block(names=setting)

        assert actor_contains_blocked_words('innocent@peer.blocked.test') is False

    @pytest.mark.parametrize('setting', EMPTY_ENTRY_SETTINGS)
    def test_the_real_entries_still_block(self, app, actor, setting):
        """The other direction, and the one that makes this a fix rather than a
        removal: `spam` is in every setting above and must still match."""
        block(names=setting)

        assert actor_contains_blocked_words('spammer@peer.blocked.test') is True

    def test_a_list_of_only_blank_lines_blocks_nobody(self, app, actor):
        """A setting that is ALL whitespace.

        Two guards now cover this: the outer `blocked_words.strip() != ''`, which
        was always there, and the per-entry skip added by D1388. Mutating either
        one away leaves the other answering correctly -- verified by the mutation
        pass, where both survived -- so the outer guard became defence in depth
        the moment empty entries were skipped. Asserted once, without claiming
        which line is doing the work.
        """
        block(names='\n\n   \n')

        assert actor_contains_blocked_words('anyone@peer.blocked.test') is False


class TestAnEmptyEntryInTheBioList:
    @pytest.mark.parametrize('setting', EMPTY_ENTRY_SETTINGS)
    def test_an_ordinary_bio_is_not_blocked(self, app, actor, setting):
        block(bios=setting)

        assert actor_profile_contains_blocked_words(actor) is False

    @pytest.mark.parametrize('setting', EMPTY_ENTRY_SETTINGS)
    def test_a_matching_bio_still_blocks(self, app, actor, setting):
        block(bios=setting)
        actor.about_html = '<p>buy cheap SPAM here</p>'
        db.session.commit()

        assert actor_profile_contains_blocked_words(actor) is True


# --------------------------------------------------------------------------
# What each function does the rest of the time
# --------------------------------------------------------------------------


class TestTheNameCheck:
    def test_no_setting_blocks_nobody(self, app, actor):
        assert actor_contains_blocked_words('anyone@peer.blocked.test') is False

    def test_it_is_a_substring_match(self, app, actor):
        """`blocked_word in actor` -- a word anywhere in the handle counts, which
        is the point: `spam` blocks `notspammer` too."""
        block(names='spam')

        assert actor_contains_blocked_words('notspammer@peer.blocked.test') is True

    def test_it_ignores_case_on_both_sides(self, app, actor):
        block(names='SPAM')

        assert actor_contains_blocked_words('Spammer@Peer.Blocked.Test') is True

    def test_padding_around_an_entry_is_ignored(self, app, actor):
        block(names='   spam   ')

        assert actor_contains_blocked_words('spammer@peer.blocked.test') is True

    def test_several_entries_are_all_checked(self, app, actor):
        block(names='spam\nscam\nphish')

        assert actor_contains_blocked_words('phisher@peer.blocked.test') is True

    def test_a_word_that_does_not_appear_does_not_block(self, app, actor):
        block(names='spam\nscam')

        assert actor_contains_blocked_words('honest@peer.blocked.test') is False


class TestTheBioCheck:
    def test_no_setting_blocks_nobody(self, app, actor):
        assert actor_profile_contains_blocked_words(actor) is False

    def test_a_user_with_no_bio_is_not_blocked(self, app, actor):
        """`if user.about_html and ...` -- an actor who has written nothing
        cannot have written a blocked word.

        Also defence in depth after D1388: with empty entries skipped, no
        remaining word can be found in `''` either, so removing this `and` changes
        nothing observable. Its mutant survives for that reason.
        """
        block(bios='spam')
        actor.about_html = None
        db.session.commit()

        assert actor_profile_contains_blocked_words(actor) is False

    @pytest.mark.parametrize('value', [None, 'a string', 5, []])
    def test_anything_that_is_not_a_user_is_not_blocked(self, app, value):
        """`if user is None or not isinstance(user, User)` -- the caller passes
        whatever `find_actor_or_create` resolved, which may be a Community, a
        Feed, or None."""
        assert actor_profile_contains_blocked_words(value) is False

    def test_a_community_is_not_blocked_by_the_bio_list(self, app, actor):
        """The isinstance guard's realistic case: this function is called from
        `find_actor_or_create`, which resolves communities and feeds too, and
        `Community.about_html` is a different column with its own setting."""
        from tests.factories import make_community

        block(bios='spam')
        community = make_community('somecommunity')
        db.session.commit()

        assert actor_profile_contains_blocked_words(community) is False

    def test_it_ignores_case(self, app, actor):
        block(bios='SPAM')
        actor.about_html = '<p>buy cheap spam</p>'
        db.session.commit()

        assert actor_profile_contains_blocked_words(actor) is True

    def test_it_matches_inside_markup(self, app, actor):
        """The column is HTML, and the check runs over it verbatim -- so a word
        split by a tag is not found, and one inside an attribute is. Pinned as
        current behaviour rather than endorsed: it is why the setting is a blunt
        instrument."""
        block(bios='spam')
        actor.about_html = '<p>sp<em>am</em></p>'
        db.session.commit()

        assert actor_profile_contains_blocked_words(actor) is False
