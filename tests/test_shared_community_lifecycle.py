r"""`edit_community`'s source fork and field extraction
(app/shared/community.py:294-319), the last uncovered group in this module.

STEP 1'S ORACLE CHECK, CONFIRMED BEFORE WRITING ANYTHING ELSE:

    for f in make_community edit_community; do
        printf "%s: " $f; /usr/bin/grep -rln "$f" tests/ --include=*.py
    done
    /usr/bin/grep -rn "edit_community(\|make_community(" app/ --include=*.py \
        | /usr/bin/grep -v "^app/shared/community.py"

`make_community` matches 97 files under tests/. EVERY ONE OF THEM is the
FACTORY of the same name (tests/factories.py:124), confirmed by reading the
import line rather than trusting the string match: every hit checked
(tests/test_shared_community_membership.py:54, tests/test_shared_community_
moderation.py:91, tests/test_shared_community_invites.py:326, and a blanket
`grep -rn "from app.shared.community import" tests/ | grep -E
"make_community|edit_community"`, which returns NOTHING across the whole
suite) imports it from `tests.factories`, never from `app.shared.community`.
This is the SAME naming trap sub-project 47 registered against
`delete_community`: a widely-used name that is not the production function
at all.

`edit_community` matches exactly ONE file, tests/test_shared_tasks_groups.py,
and reading its import (`from app.shared.tasks.groups import edit_community`,
:48) shows it is a COMPLETELY DIFFERENT function -- `app/shared/tasks/
groups.py:52`'s `def edit_community(send_async, user_id, community_id)`, the
AP Update sender for a community's Group actor (that file's own docstring
says so in its first line), unrelated to this module's `edit_community`
other than sharing a name.

CONCLUSION: before this task, NEITHER `app.shared.community.make_community`
NOR `app.shared.community.edit_community` was exercised by any existing test
file. This file is the first.

THE SECOND GREP, PRODUCTION CALLERS (confirmed by reading each import, not
the string):

    app/api/alpha/utils/community.py:221:  make_community(input, SRC_API, auth)
    app/api/alpha/utils/community.py:261:  edit_community(input, community, SRC_API, auth)

`app/api/alpha/utils/community.py:14` imports both names `from
app.shared.community import ...`, confirming these ARE the production
functions. `make_community` has exactly ONE production caller, and it always
passes `SRC_API`. `edit_community` has exactly TWO: the direct call above
(`SRC_API`, `from_scratch` defaulted to `False`) and `app/shared/
community.py:282`, `make_community`'s own `from_scratch=True` pass-through of
its own `src` parameter.

THIS IS A STRONGER CLAIM THAN "the web arm is reachable only through
make_community": since make_community's one production caller always passes
`SRC_API`, the `src` that reaches `edit_community:282`'s pass-through is
ALSO always `SRC_API`. So `edit_community`'s `:294` else/web arm (:307-317)
has NO production caller at ANY value of `from_scratch` -- not just the
`from_scratch=False` combination the brief singles out. The web UI's own
community-edit form is a separate implementation at
`app/community/routes.py:1216` that never calls this function at all.
`_web_input`'s docstring below repeats this, and so does every test that
uses it, per this round's disclosure requirement.

SCOPE, PRECISELY: this task's target is `:294-319` -- the two-arm source
fork and the `icon_url_changed = banner_url_changed = False` initialiser
that follows it. `:321-346` (the ownership check and icon/banner-changed
detection) is Task 2's territory; `:348-391` (image persistence, the final
field writes, the language block, and the return) is Task 3's and Task 5's.
Every test below passes `from_scratch=True`, which is what lets a direct
call to `edit_community` skip straight over `:321-346` without needing a
`CommunityMember` row granting ownership -- `if not from_scratch:` (:321)
gates that whole block. Execution still runs on through `:348-391` after the
fork (Python does not stop at line 319), so those lines are incidentally
exercised here too, but this file makes no claim on them and asserts nothing
that depends on their correctness beyond "the call completes and the four
fields this task's fork sets land on the community row."

THE id-1 ADMIN TRAP: `app/models.py:1259-1261` makes user id 1 an admin
unconditionally, and tests/conftest.py:131-132 resets every sequence between
tests, so the first user any test creates is silently id 1. `make_community`
the FACTORY (tests/factories.py:124, not the production function under test)
hardcodes `instance_id=1, user_id=1`, so `_burn_a_seed()` below exists to
supply the row those foreign keys point at, and `_seed()` mints a bystander
community before the real one so the community under test is never id 1
either -- a mutant hardcoding `community_id=1` anywhere in this round's
later tasks would otherwise be invisible to every test built on this seed.

NETWORK HAZARD, WHY icon_url/banner_url STAY FALSY THROUGHOUT THIS FILE:
`edit_community:348`'s `is_image_url(icon_url)` (reached only via Task 3's
territory, but reached all the same on every call this file makes) calls
`mime_type_using_head(url)` for any truthy `url` -- a real HTTP HEAD request.
This suite's session-scoped `block_outbound_http` fixture (tests/conftest.py:
263) turns any unmocked request into a hard failure rather than letting it
reach the network. Every builder and every patched `process_upload` in this
file is therefore built to keep `icon_url`/`banner_url` at a falsy value
(`None`) on every path, so `:348`'s guard short-circuits before
`is_image_url` is ever called.

RULING 1 (progress.md), BINDING OVER THE BRIEF'S SAMPLE CODE WHERE THEY
DIFFER: `edit_community` reads ten keys/attributes; `make_community` reads a
DIFFERENT SEVEN, not a subset (it reads `name`, which edit_community never
touches, and skips `description`, `rules`, `icon_url`, `banner_url`). Rather
than let Task 4 build a second, competing builder pair for make_community,
`_api_input`/`_web_input` below carry the UNION of both key sets, documented
per-key in their own docstrings, so Task 4 consumes these unchanged.

MEASUREMENT BASIS: every count in this file's task report is measured
THIS-FILE-ALONE (`--cov=app.shared.community` run against only this test
module). The full-suite figure is Task 6's; per this round's plan, no other
test file names `make_community` or `edit_community` as production
functions (the oracle check above), so this file's coverage of `:294-319`
should not shift between the two bases the way `test_shared_community_
invites.py`'s `get_comm_flair_list` did.
"""
from types import SimpleNamespace

import pytest
from slugify import slugify
from sqlalchemy import event, text

from app import db
from app.constants import SRC_API, SRC_WEB
from app.models import Community, CommunityMember, File, Language, User
from app.shared.community import edit_community, make_community
from app.utils import markdown_to_html, piefed_markdown_to_lemmy_markdown
# `make_community` (tests/factories.py:124) is renamed on import here, per
# this file's own module docstring's naming-trap note: Task 4 needs the
# BARE name `make_community` for `app.shared.community.make_community`,
# the production function under test, so the row-builder of the same name
# is aliased instead. `_seed()`'s two call sites below are updated to
# match; nothing else in this file (Tasks 1-3) called the factory by name.
from tests.factories import bearer, make_community as make_community_factory, make_community_member, \
    make_instance, make_user, web_ctx


def _burn_a_seed():
    """Mint and discard user id 1.

    app/models.py:1259-1261 makes id 1 an admin unconditionally, and
    conftest.py:131-132 resets every sequence between tests, so without this
    the first user a test creates is silently an admin. Neither this task's
    tests nor `edit_community`'s `from_scratch=True` path check admin status,
    but `make_community` the FACTORY (tests/factories.py:124) hardcodes
    `instance_id=1, user_id=1`, so the burn is still load-bearing: it
    supplies the row those foreign keys point at.
    """
    inst = make_instance('burn.test')
    burn = make_user(inst, 'burn')
    assert burn.id == 1, f'expected the burn user at id 1, got {burn.id}'
    return inst


def _seed():
    """An instance, a non-admin local user, a real community, and a
    bystander community, with id 1 burned.

    The bystander is minted FIRST, purely to consume Community id 1, so the
    community under test is never id 1 -- see the module docstring's id-1
    trap note. It is returned as `.bystander` so Tasks 2-5 can reuse it for
    their own cross-community negative controls without minting a second
    community themselves, matching this round's sibling file's convention
    (tests/test_shared_community_invites.py's `_seed`).
    """
    _burn_a_seed()
    instance = make_instance('test.piefed.local')
    user = make_user(instance, 'alice', local=True)
    bystander = make_community_factory('bystander', host='bystander.example')
    community = make_community_factory()
    return SimpleNamespace(instance=instance, user=user, community=community, bystander=bystander)


def _api_input(**overrides):
    """The dict shape edit_community's SRC_API arm (:295-304) reads, UNIONED
    with make_community's SRC_API arm (:217-223) per RULING 1 (progress.md)
    so Task 4 consumes this builder unchanged rather than building a second,
    competing one.

    `edit_community` reads all ten keys below at :295-304: title,
    description, rules, icon_url, banner_url, nsfw, restricted_to_mods,
    local_only, discussion_languages, question_answer.

    `make_community` additionally reads `name` (:217) -- the one key
    edit_community never touches -- and does NOT read description, rules,
    icon_url, or banner_url; those four exist here solely for
    edit_community's own tests. `discussion_languages` is read by both
    functions but only ACTED on when `from_scratch=False` (edit_community
    :371-380) or unconditionally inside make_community's own body
    (:272-279) -- every test in this file passes `from_scratch=True`, which
    skips edit_community's own use of it entirely; the key is still read at
    :303 regardless (a plain dict-index, not a branch), so a missing key
    would raise `KeyError` there even under `from_scratch=True`.

    `icon_url`/`banner_url` default to `None` deliberately: see the module
    docstring's NETWORK HAZARD note. A truthy value on either key reaches
    `edit_community:348`'s `is_image_url(icon_url)`, a real HTTP HEAD
    request this suite's `block_outbound_http` fixture turns into a hard
    failure. `None` short-circuits `:348`'s `if icon_url` before that call.
    """
    data = {
        'name': 'newcommunity',
        'title': 'A Title',
        'description': 'A description',
        'rules': 'Be nice',
        'icon_url': None,
        'banner_url': None,
        'nsfw': False,
        'restricted_to_mods': False,
        'local_only': False,
        'discussion_languages': [],
        'question_answer': False,
    }
    data.update(overrides)
    return data


class _Field:
    """A single WTForms-field stand-in: production reads `field.data`, never
    the field object itself, on every attribute `edit_community`'s and
    `make_community`'s web arms touch.
    """

    def __init__(self, data):
        self.data = data


def _web_input(**overrides):
    """The form-object shape edit_community's web/else arm (:307-316) reads,
    UNIONED with make_community's web arm (:226-236) per RULING 1
    (progress.md), so Task 4 consumes this builder unchanged.

    `edit_community` reads eight attributes below, each through `.data`:
    community_name, description, rules, nsfw, restricted_to_mods,
    local_only, languages, question_answer. `icon_url`/`banner_url` are NOT
    read from this object on that arm -- they come from
    `process_upload(uploaded_icon_file / uploaded_banner_file, ...)`, passed
    to `edit_community` as separate keyword arguments, never through `input`.

    `make_community` additionally reads `url` (:226-228, mutated in place
    before being re-read as `name` at :230) and does NOT read `description`
    or `rules`; `url` exists here solely for Task 4's make_community tests.

    UNREACHABILITY, repeated here per this round's disclosure requirement
    (a reader landing on one test must not have to find this builder to
    learn the path is dead): `edit_community` has exactly two production
    callers -- app/api/alpha/utils/community.py:261 (SRC_API,
    from_scratch=False) and app/shared/community.py:282 (make_community's
    own from_scratch=True pass-through of its own `src`). `make_community`
    itself has exactly one production caller, app/api/alpha/utils/
    community.py:221, which always passes SRC_API. So the `src` reaching
    edit_community:282's pass-through is ALWAYS SRC_API too -- this
    builder's arm (`:294`'s else branch) has NO production caller at either
    value of `from_scratch`. The web UI's own community-edit form is a
    separate implementation at app/community/routes.py:1216 that never
    calls edit_community. Every test below using `_web_input` reaches this
    arm only by calling edit_community directly with a non-API `src`.
    """
    values = {
        'url': 'newcommunity',
        'community_name': 'A Title',
        'description': 'A description',
        'rules': 'Be nice',
        'nsfw': False,
        'restricted_to_mods': False,
        'local_only': False,
        'languages': [],
        'question_answer': False,
    }
    values.update(overrides)
    return SimpleNamespace(**{key: _Field(value) for key, value in values.items()})


# `edit_community` (app/shared/community.py:293-391), the source fork and
# field extraction at :294-319.


def test_edit_community_api_arm_reads_all_ten_keys_and_authorises_user(app, db_session):
    """`:294`'s TRUE arm (`src == SRC_API`): all ten dict keys at :295-304 are
    read into locals, and `:305` resolves the acting user via
    `authorise_api_user(auth, return_type='model')` -- NOT `current_user`,
    the else arm's source, covered by the web-arm tests below.

    `from_scratch=True` is passed here (as in every test in this file)
    purely to skip `:321-346` -- the ownership check and icon/banner-changed
    detection, Task 2's territory -- not to change which of `:295-304`'s ten
    assignments run; those execute unconditionally once `:294` takes the
    SRC_API arm.

    Every boolean key is set to its NON-default value and every string key
    to a value distinct from `_api_input`'s own default, so a mutant that
    dropped one of `:295-304`'s assignments (leaving Python's implicit
    `None`/`False`, or the community's own factory default) is caught by the
    matching field assertion below rather than passing on a coincidental
    default match.

    `description` is deliberately a string containing `\\r\\n` after a
    non-whitespace character -- the exact shape `piefed_markdown_to_
    lemmy_markdown` (called only by the web arm's `:308`, never this one)
    would rewrite. Asserting it lands UNCHANGED is this test's negative
    control proving the SRC_API arm truly skips that call, mirroring the
    sibling web-arm test below which asserts the same raw string DOES change
    under the else arm.
    """
    s = _seed()
    raw_description = 'a paragraph\r\nmore text'
    api_input = _api_input(title='API Title', description=raw_description,
                           rules='API rules', nsfw=True, restricted_to_mods=True,
                           local_only=True, question_answer=True)

    result = edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=True)

    assert result is s.community
    assert s.community.title == 'API Title'
    assert s.community.description == raw_description
    assert s.community.description_html == markdown_to_html(raw_description)
    assert s.community.rules == 'API rules'
    assert s.community.nsfw is True
    assert s.community.restricted_to_mods is True
    assert s.community.local_only is True
    assert s.community.question_answer is True


def test_edit_community_web_arm_reads_all_fields_and_converts_description(
        app, db_session, monkeypatch):
    """`:294`'s FALSE arm (any `src != SRC_API`, here `SRC_WEB`): the eight
    form attributes read at `:307-309` and `:312-316`, plus `user =
    current_user` at `:317`.

    `:308` additionally runs `piefed_markdown_to_lemmy_markdown` on the raw
    description -- the one transform the SRC_API arm never applies. The
    expected value below is produced by calling the real production helper
    independently, not by hand-rolling the substitution, matching this
    suite's convention elsewhere (tests/test_shared_community_invites.py's
    `send_email` tests build their expected body the same way).

    `:310`/`:311`'s `process_upload(...) if uploaded_icon_file else None`
    ternaries both take their FALSE arm here (`uploaded_icon_file` and
    `uploaded_banner_file` are both the default `None` on `edit_community`'s
    own signature, and `_web_input` supplies neither) -- `process_upload` is
    patched with a call-recording spy and asserted NOT called, which is what
    distinguishes this test from the two below where it is. `process_upload`
    is patched on `app.shared.community`, never on its source module
    `app.shared.upload`: `from app.shared.upload import process_upload`
    (app/shared/community.py:21) binds the name into THIS module's globals
    at import time, so a patch on `app.shared.upload.process_upload` would
    leave the already-bound reference here untouched and, worse, a truthy
    return value would reach `:348`'s real network call (see the module
    docstring's NETWORK HAZARD note).

    UNREACHABLE IN PRODUCTION -- see `_web_input`'s docstring for the full
    chain: edit_community's only two production callers both resolve to
    SRC_API (directly, and via make_community, whose own only caller also
    always passes SRC_API), so this arm has no production caller at all,
    at either value of `from_scratch`. This test reaches it only by calling
    edit_community directly with SRC_WEB.
    """
    s = _seed()
    calls = []
    monkeypatch.setattr('app.shared.community.process_upload',
                        lambda *a, **kw: calls.append((a, kw)))
    raw_description = 'a paragraph\r\nmore text'
    web_input = _web_input(community_name='Web Title', description=raw_description,
                           rules='Web rules', nsfw=True, restricted_to_mods=True,
                           local_only=True, question_answer=True)

    with web_ctx(app, s.user):
        result = edit_community(web_input, s.community, SRC_WEB, from_scratch=True)

    assert result is s.community
    assert calls == []
    assert s.community.title == 'Web Title'
    assert s.community.description == piefed_markdown_to_lemmy_markdown(raw_description)
    assert s.community.description != raw_description
    assert s.community.rules == 'Web rules'
    assert s.community.nsfw is True
    assert s.community.restricted_to_mods is True
    assert s.community.local_only is True
    assert s.community.question_answer is True


def test_edit_community_web_arm_processes_uploaded_icon_via_process_upload(
        app, db_session, monkeypatch):
    """`:310`'s TRUE arm: a truthy `uploaded_icon_file` runs
    `process_upload(uploaded_icon_file, destination='communities')`. `:311`'s
    banner ternary takes its FALSE arm in the SAME call (`uploaded_banner_file`
    stays the default `None`), so this test and the sibling banner-only test
    below CROSS the two ternaries' truth values rather than only ever varying
    them together -- a mutant that conjoined the two guards (e.g. requiring
    BOTH files before calling `process_upload` at all) would leave this
    test's `calls` at zero instead of one, and the sibling test's `calls` at
    zero too, catching the conjunction from either side.

    `process_upload` is patched to return `None` regardless of its argument,
    not a URL string: a truthy `icon_url` would reach `:348`'s
    `is_image_url(icon_url)`, a real network call this suite's
    `block_outbound_http` fixture turns into a hard failure (see the module
    docstring's NETWORK HAZARD note, and `_api_input`'s docstring for the
    same hazard on the SRC_API arm's `icon_url`/`banner_url` keys). Patched
    on `app.shared.community`, never on `app.shared.upload`, for the same
    import-binding reason as the sibling test above.

    The exact positional argument and the `destination='communities'`
    keyword are asserted, not just the call count: a mutant that passed the
    wrong file (e.g. swapping in `uploaded_banner_file`) or dropped/altered
    the destination string would still make ONE call, but fails one of the
    two assertions below.

    UNREACHABLE IN PRODUCTION -- see `_web_input`'s docstring: this arm has
    no production caller at any `from_scratch` value.
    """
    s = _seed()
    calls = []
    monkeypatch.setattr('app.shared.community.process_upload',
                        lambda *a, **kw: calls.append((a, kw)))
    web_input = _web_input()

    with web_ctx(app, s.user):
        edit_community(web_input, s.community, SRC_WEB, uploaded_icon_file='FAKE_ICON_FILE',
                       from_scratch=True)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ('FAKE_ICON_FILE',)
    assert kwargs == {'destination': 'communities'}


def test_edit_community_web_arm_processes_uploaded_banner_via_process_upload(
        app, db_session, monkeypatch):
    """`:311`'s TRUE arm: a truthy `uploaded_banner_file` runs
    `process_upload(uploaded_banner_file, destination='communities')`.
    `:310`'s icon ternary takes its FALSE arm in the SAME call
    (`uploaded_icon_file` stays the default `None`) -- the mirror-image
    crossing of the sibling icon-only test above, so between the two tests
    both ternaries are exercised at both truth values with the OTHER
    ternary held at the opposite value each time, rather than the two
    always agreeing.

    Same network-avoidance and import-binding reasoning as the sibling icon
    test: `process_upload` returns `None` regardless of its argument, and is
    patched on `app.shared.community`, never `app.shared.upload`.

    UNREACHABLE IN PRODUCTION -- see `_web_input`'s docstring: this arm has
    no production caller at any `from_scratch` value.
    """
    s = _seed()
    calls = []
    monkeypatch.setattr('app.shared.community.process_upload',
                        lambda *a, **kw: calls.append((a, kw)))
    web_input = _web_input()

    with web_ctx(app, s.user):
        edit_community(web_input, s.community, SRC_WEB, uploaded_banner_file='FAKE_BANNER_FILE',
                       from_scratch=True)

    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ('FAKE_BANNER_FILE',)
    assert kwargs == {'destination': 'communities'}


# `edit_community` (app/shared/community.py:321-346), the permission guard
# and icon/banner-changed detection -- this round's other task, following on
# from the source-fork tests above. Task 3 owns :348-391.


def _make_site_admin(user):
    """Give `user` a role named exactly 'Admin'.

    Transcribed from tests/test_shared_community_moderation.py's identical
    helper (itself transcribed from tests/test_shared_reply_moderation.py):
    `User.is_admin()` (app/models.py:1259-1265) checks role NAMES, not
    permissions, so `grant_permission` (tests/factories.py:365) cannot
    produce a site admin however it is called -- the name must be the
    literal string 'Admin'.
    """
    from app.models import Role, user_role
    role = Role(name='Admin', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def _make_site_staff(user):
    """Give `user` a role named exactly 'Staff'.

    `User.is_staff()` (app/models.py:1267-1272) checks role names the same
    way `is_admin()` does. This is the counterpart used to pin `:322`'s
    inconsistency with its four sibling guards: `delete_community:494`,
    `restore_community:523`, `add_mod_to_community:549`, and
    `remove_mod_from_community:617` all call `is_admin_or_staff()`
    (app/models.py:1274-1275), but `edit_community:322` calls the narrower
    `is_admin()` alone, so a Staff-only user is refused here where the four
    siblings would admit them.
    """
    from app.models import Role, user_role
    role = Role(name='Staff', weight=0)
    db.session.add(role)
    db.session.commit()
    db.session.execute(user_role.insert().values(user_id=user.id, role_id=role.id))
    db.session.commit()
    return role


def _seed_und_language():
    """The 'und' Language row `:377-379` unconditionally requires once
    `from_scratch=False` reaches it.

    Every test below that gets past `:322`'s guard passes `from_scratch=
    False` to reach this task's `:321-346` target, and Python does not stop
    at `:346` -- execution runs on through Task 3's `:371-380` too.
    `:377`'s `Language.query.filter(Language.code == 'und').first()` returns
    None on an empty table, and `:378`'s `undetermined.id` then raises
    AttributeError before any of this task's own assertions can run. The
    module docstring's oracle check found no prior test file that ever drove
    `from_scratch=False` through this function, so this seed has no
    precedent to copy; it exists solely to keep Task 3's territory from
    crashing this task's tests and makes no claim on that territory beyond
    that.
    """
    und = Language(code='und', name='Undetermined')
    db.session.add(und)
    db.session.commit()
    return und


# SUBSUMPTION, VERIFIED AT SOURCE, REGISTERED NOT TESTED -- `:322`'s
# `community.is_owner(user) or community.is_moderator(user) or
# user.is_admin()`. `Community.is_owner(user)` (app/models.py:747) checks
# the `is_owner` COLUMN on a CommunityMember row drawn from
# `self.moderators()` (:716-722), which selects every non-banned row where
# `is_owner OR is_moderator` holds. `Community.is_moderator(user)` (:740)
# then only checks MEMBERSHIP of `user.id` in that same list -- it never
# reads the `is_moderator` column. So a row with `is_owner=True` is always
# admitted to `moderators()` by the `OR`, which makes `is_moderator(user)`
# True for the identical user on the identical call; the CommunityMember
# primary key (user_id, community_id) rules out a second, differently-
# shaped row for the same user in the same community that could make the
# two diverge. `community.is_owner(user)` can therefore never be the reason
# `:322`'s guard passes, for any input -- proved by reading the two method
# bodies above, not by any test's failure to kill a mutant that removes it.
#
# This is tests/README.md fact 75, cause 3, "Subsumption", identical in
# shape to `delete_community:494`'s registered instance of the same proof
# (sub-project 46, tests/test_shared_community_moderation.py:94-150), which
# also notes: NOT cause 6 (cause 6 opens "the only cause on this list that
# is not about a clause", and `community.is_owner(user) or` IS a clause --
# a disjunct in a boolean expression); and that this is the DISJUNCTIVE dual
# of cause 3's catalogued conjunctive text -- `A or B` where the EARLIER
# disjunct (`is_owner(user)`) implies the LATER one (`is_moderator(user)`),
# the mirror image of `A and B` where the LATER conjunct implies the
# EARLIER one.
#
# NO TEST ISOLATES OPERAND ONE ALONE, and none should be written to: doing
# so would require a CommunityMember row with `is_owner=True` and
# `is_moderator=False` for the same user in the same community -- a state
# production cannot reach, per the proof above. Monkeypatching
# `Community.is_moderator` to fake that shape is rejected for the same
# reason sub-project 46 rejected it there: it would fabricate a state
# production cannot reach, a false witness of a different shape than this
# campaign hunts. A mutant deleting `community.is_owner(user) or` from
# `:322` is expected to survive every test in this file and should be read
# against this comment, not treated as an uncovered gap.


def test_edit_community_permission_guard_moderator_not_admin_is_admitted(
        app, db_session, monkeypatch):
    """`:322`'s SECOND operand alone: `is_moderator=True`, `is_owner=False`,
    no admin role -- the one operand of the three that is genuinely
    separable from the other two (see the SUBSUMPTION comment above for why
    operand one never is, and the admin-alone test below for operand
    three). The guard passes and execution proceeds past `:323`'s raise.

    `task_selector` is patched on `app.shared.community` (this module's own
    `from app.shared.tasks import task_selector` rebinds the name into ITS
    globals at import time, matching this suite's established convention --
    patching `app.shared.tasks.task_selector` would leave the reference
    already bound here untouched) so `:382`'s call, reached because
    `from_scratch=False` runs on through Task 3's territory, doesn't fire
    the real AP Update body under this test config's eager task execution.

    The return value (`:391`'s `return user.id`, the `from_scratch=False`
    arm) and the title landing are both asserted, so a mutant that let the
    guard pass but skipped the field-write block entirely would still be
    caught.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    api_input = _api_input(title='Modded Title')

    result = edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert result == s.user.id
    assert s.community.title == 'Modded Title'


def test_edit_community_permission_guard_admin_not_member_is_admitted(
        app, db_session, monkeypatch):
    """`:322`'s THIRD operand alone: `s.user` holds no CommunityMember row at
    all -- `Community.moderators()` (app/models.py:716-722) returns an empty
    list for it, so `is_owner`/`is_moderator` are both False on their own --
    and is instead a site admin via `_make_site_admin`, which grants a Role
    named exactly 'Admin' so `User.is_admin()` returns True.
    """
    s = _seed()
    _seed_und_language()
    _make_site_admin(s.user)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    api_input = _api_input(title='Admin Title')

    result = edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert result == s.user.id
    assert s.community.title == 'Admin Title'


def test_edit_community_permission_guard_neither_raises_and_leaves_community_untouched(
        app, db_session):
    """`:322`'s all-three-False case: no CommunityMember row and no admin
    role. `:323` raises before any of this task's own target lines
    (`:325-346`) or Task 3's territory run, so `community.title` must
    survive unchanged from before the call -- a mutant that raised AFTER
    mutating the row would still satisfy a raise-only assertion.
    """
    s = _seed()
    original_title = s.community.title
    api_input = _api_input(title='Should Not Land')

    with pytest.raises(Exception, match='incorrect_login'):
        edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert s.community.title == original_title


def test_edit_community_permission_guard_staff_alone_is_refused_unlike_sibling_guards(
        app, db_session):
    """`:322` reads `user.is_admin()`, NOT `is_admin_or_staff()` -- the only
    guard in this module that uses the narrower form. `delete_community:494`,
    `restore_community:523`, `add_mod_to_community:549`, and
    `remove_mod_from_community:617` all use `is_admin_or_staff()`
    (app/models.py:1274-1275) and would admit a Staff-only user where this
    site refuses them. REGISTERED AS FOUND, NOT FIXED: this test pins
    today's actual, inconsistent behaviour -- a staff user with no
    CommunityMember row and no 'Admin' role is refused here.
    """
    s = _seed()
    _make_site_staff(s.user)
    original_title = s.community.title
    api_input = _api_input(title='Should Not Land Either')

    with pytest.raises(Exception, match='incorrect_login'):
        edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert s.community.title == original_title


# `edit_community` (app/shared/community.py:325-333), the icon block.
#
# `icon_url_changed` is a local variable with no return-value or row
# exposure of its own. Every test below observes it through `:348`'s
# downstream composite `icon_url and (from_scratch or icon_url_changed) and
# is_image_url(icon_url)`: with `from_scratch=False` and `icon_url` held
# truthy and IDENTICAL across all four icon-block tests, `is_image_url` is
# called if and only if `icon_url_changed` is True. `is_image_url` is
# patched to a call-recording spy that always returns False, which both
# makes the observation possible and keeps `:348`'s real branch (a new
# `File` row and `make_image_sizes`, neither this task's territory) from
# running. `banner_url` is held at the input builder's own falsy default
# (`None`) throughout so the banner block's own `is_image_url` call
# (`:354`) never fires and confounds this spy -- the mirror-image banner
# tests below hold `icon_url` at `None` for the same reason.


def test_edit_community_icon_block_no_icon_id_marks_changed_without_delete(
        app, db_session, monkeypatch):
    """`:325`'s FALSE arm (`community.icon_id` is falsy -- the factory never
    sets it) skips straight to `:332`'s TRUE arm, which sets
    `icon_url_changed = True` without ever reaching `:326`'s inner guard or
    `:330`'s `delete_from_disk()`. `File.delete_from_disk` is patched with
    its own call-recording spy (never called on this path, since `:328`'s
    `File.query.get(community.icon_id)` is only reached from inside `:326`'s
    TRUE arm) and `community.icon_id` is asserted to remain None: there was
    nothing to clear.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    assert s.community.icon_id is None
    api_input = _api_input(icon_url='https://icon.example/candidate.png', banner_url=None)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == ['https://icon.example/candidate.png']
    assert delete_calls == []
    assert s.community.icon_id is None


def test_edit_community_icon_block_icon_url_matches_source_url_no_change(
        app, db_session, monkeypatch):
    """`:325`'s FALSE arm via its second conjunct: `community.icon_id` is
    truthy but `icon_url == community.icon.source_url`, so the `and`
    short-circuits false and `:326-331` never run. `:332`'s `not
    community.icon_id` is then also False (the icon row survives untouched),
    so `icon_url_changed` stays at its `:319` initial value of False.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    icon_file = File(source_url='https://icon.example/original.png',
                     file_path='app/static/media/communities/thumb.png')
    db.session.add(icon_file)
    db.session.commit()
    s.community.icon_id = icon_file.id
    db.session.commit()
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    api_input = _api_input(icon_url=icon_file.source_url, banner_url=None)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == []
    assert delete_calls == []
    assert s.community.icon_id == icon_file.id


def test_edit_community_icon_block_icon_url_matches_medium_url_no_change(
        app, db_session, monkeypatch):
    """`:325`'s TRUE arm (`icon_url != source_url`) but `:326`'s FALSE arm
    (`icon_url == community.icon.medium_url()`): `:326-331`'s inner block --
    `icon_url_changed = True`, `delete_from_disk()`,
    `community.icon_id = None` -- never runs. The expected medium URL is
    computed by calling the real `File.medium_url()` on the seeded row
    rather than hand-built, matching this suite's convention of deriving
    expected values from the production helper rather than guessing its
    output shape (the sibling `_web_input` test above does the same with
    `piefed_markdown_to_lemmy_markdown`).
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    icon_file = File(source_url='https://icon.example/original.png',
                     file_path='app/static/media/communities/thumb.png')
    db.session.add(icon_file)
    db.session.commit()
    s.community.icon_id = icon_file.id
    db.session.commit()
    medium_url = icon_file.medium_url()
    assert medium_url != icon_file.source_url, 'test setup must diverge source_url from medium_url'
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    api_input = _api_input(icon_url=medium_url, banner_url=None)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == []
    assert delete_calls == []
    assert s.community.icon_id == icon_file.id


def test_edit_community_icon_block_icon_url_matches_neither_clears_and_deletes(
        app, db_session, monkeypatch):
    """`:325` and `:326` BOTH TRUE: `icon_url_changed = True`,
    `remove_file.delete_from_disk()` runs on the row `community.icon_id`
    pointed at, and `community.icon_id` is cleared to None. `is_image_url`
    is asserted called exactly ONCE (not twice) even though `:332`'s `not
    community.icon_id` is then also True: `icon_url_changed` is a plain
    boolean re-set to the same True value there, not re-checked a second
    time before `:348`'s single downstream read.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    icon_file = File(source_url='https://icon.example/original.png',
                     file_path='app/static/media/communities/thumb.png')
    db.session.add(icon_file)
    db.session.commit()
    s.community.icon_id = icon_file.id
    db.session.commit()
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    api_input = _api_input(icon_url='https://icon.example/totally-different.png', banner_url=None)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == ['https://icon.example/totally-different.png']
    assert delete_calls == [icon_file.id]
    assert s.community.icon_id is None


def test_edit_community_icon_block_orphaned_icon_id_clears_without_delete(
        app, db_session, monkeypatch):
    """`:328-331`'s FALSE arm of `:329`'s `if remove_file:` -- the state the
    prior four icon-block tests above never reach: `community.icon_id` is a
    genuinely valid, non-null id, `:325` and `:326` are BOTH True (same
    inputs as the "matches neither" test above), so `:327-331` runs, but a
    FRESH `File.query.get(community.icon_id)` (`:328`) finds no row, unlike
    `community.icon` (`:325`'s `community.icon.source_url` /`:326`'s
    `.medium_url()`), which is resolved once, eagerly, alongside `community`
    itself (`icon = db.relationship('File', lazy='joined', ...)`,
    app/models.py:636) via a JOIN in `community`'s own original query --
    never through `File.query` at all. This models a fresh lookup finding
    the row already gone (e.g. a concurrent hard delete) while the
    already-loaded relationship object this test seeded is still valid and
    unaffected.

    `File.query` (a Flask-SQLAlchemy class-level query descriptor) is
    replaced on `File` itself with a stand-in whose `.get` always returns
    None -- patched only on `File`, so no other model's `.query` is
    touched, and patched via `monkeypatch.setattr(File, 'query', ...)`
    (an attribute on the class object itself, not a name imported with
    `from ... import`, so no rebinding-into-globals concern applies here
    the way it does for `is_image_url`/`process_upload`/`task_selector`).

    THIS TEST'S ONE DISTINGUISHING ASSERTION FROM THE "MATCHES NEITHER" TEST
    ABOVE is `delete_calls == []`: `remove_file` is None here, so
    `:330`'s `remove_file.delete_from_disk()` never runs. `:331`'s
    `community.icon_id = None` sits OUTSIDE `:329`'s `if remove_file:`
    block (one indent level back), so it still runs and is asserted
    unchanged from the sibling test -- a mutant moving `:331` inside
    `:329`'s `if` would leave `icon_id` uncleared in exactly this state and
    nothing else, which is what this test exists to catch (see the
    mutation-verification section of this round's report for the mutant
    applied by hand and the exact failure it produces).
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    icon_file = File(source_url='https://icon.example/original.png',
                     file_path='app/static/media/communities/thumb.png')
    db.session.add(icon_file)
    db.session.commit()
    s.community.icon_id = icon_file.id
    db.session.commit()
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))

    class _AlwaysMissingQuery:
        def get(self, ident):
            return None

    monkeypatch.setattr(File, 'query', _AlwaysMissingQuery())
    api_input = _api_input(icon_url='https://icon.example/totally-different.png', banner_url=None)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == ['https://icon.example/totally-different.png']
    assert delete_calls == []
    assert s.community.icon_id is None


# `edit_community` (app/shared/community.py:334-344), the banner block --
# structurally the icon block's mirror, PLUS `cache.delete_memoized(
# Community.header_image, community)` at both `:341` and `:343`. When
# `:340` clears `image_id`, `:342` is then also true, so the call fires
# TWICE for the "matches neither" state below; the icon block has no
# equivalent cache call in either of ITS arms at all. REGISTERED AS AN
# ASYMMETRY, NOT FIXED -- this suite's tests never assert on the cache call
# itself: it is unkillable under tests/conftest.py:68's `CACHE_TYPE =
# 'NullCache'` (D602, D589). `icon_url` is held at the input builder's own
# falsy default (`None`) throughout so the icon block's own `is_image_url`
# call (`:348`) never fires and confounds the spy these tests reuse from
# the icon-block tests above to observe `banner_url_changed`.


def test_edit_community_banner_block_no_image_id_marks_changed_without_delete(
        app, db_session, monkeypatch):
    """`:334`'s FALSE arm (`community.image_id` is falsy) skips to `:342`'s
    TRUE arm, which sets `banner_url_changed = True` without ever reaching
    `:335`'s inner guard or `:339`'s `delete_from_disk()`.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    assert s.community.image_id is None
    api_input = _api_input(icon_url=None, banner_url='https://banner.example/candidate.png')

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == ['https://banner.example/candidate.png']
    assert delete_calls == []
    assert s.community.image_id is None


def test_edit_community_banner_block_banner_url_matches_source_url_no_change(
        app, db_session, monkeypatch):
    """`:334`'s FALSE arm via its second conjunct: `community.image_id` is
    truthy but `banner_url == community.image.source_url`, so `:335-340`
    never run and `:342`'s `not community.image_id` is also False (the
    banner row survives untouched) -- `banner_url_changed` stays False.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    banner_file = File(source_url='https://banner.example/original.png',
                       file_path='app/static/media/communities/banner.png')
    db.session.add(banner_file)
    db.session.commit()
    s.community.image_id = banner_file.id
    db.session.commit()
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    api_input = _api_input(icon_url=None, banner_url=banner_file.source_url)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == []
    assert delete_calls == []
    assert s.community.image_id == banner_file.id


def test_edit_community_banner_block_banner_url_matches_medium_url_no_change(
        app, db_session, monkeypatch):
    """`:334`'s TRUE arm (`banner_url != source_url`) but `:335`'s FALSE arm
    (`banner_url == community.image.medium_url()`): `:335-340`'s inner
    block never runs. The expected medium URL is computed by calling the
    real `File.medium_url()` on the seeded row, matching this suite's
    convention.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    banner_file = File(source_url='https://banner.example/original.png',
                       file_path='app/static/media/communities/banner.png')
    db.session.add(banner_file)
    db.session.commit()
    s.community.image_id = banner_file.id
    db.session.commit()
    medium_url = banner_file.medium_url()
    assert medium_url != banner_file.source_url, 'test setup must diverge source_url from medium_url'
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    api_input = _api_input(icon_url=None, banner_url=medium_url)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == []
    assert delete_calls == []
    assert s.community.image_id == banner_file.id


def test_edit_community_banner_block_banner_url_matches_neither_clears_and_deletes(
        app, db_session, monkeypatch):
    """`:334` and `:335` BOTH TRUE: `banner_url_changed = True`,
    `remove_file.delete_from_disk()` runs, and `community.image_id` clears
    to None. See this section's header comment for the `cache.
    delete_memoized` double-call asymmetry this state triggers at `:341`
    and `:343` -- not asserted here, since it is unkillable under NullCache.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    banner_file = File(source_url='https://banner.example/original.png',
                       file_path='app/static/media/communities/banner.png')
    db.session.add(banner_file)
    db.session.commit()
    s.community.image_id = banner_file.id
    db.session.commit()
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))
    api_input = _api_input(icon_url=None, banner_url='https://banner.example/totally-different.png')

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == ['https://banner.example/totally-different.png']
    assert delete_calls == [banner_file.id]
    assert s.community.image_id is None


def test_edit_community_banner_block_orphaned_image_id_clears_without_delete(
        app, db_session, monkeypatch):
    """`:337-340`'s FALSE arm of `:338`'s `if remove_file:` -- the banner
    mirror of the icon block's identical gap above. `community.image_id` is
    a genuinely valid id and `:334`/`:335` are BOTH True (same inputs as the
    "matches neither" test above), so `:336-340` runs, but a FRESH
    `File.query.get(community.image_id)` (`:337`) finds no row, unlike
    `community.image` (already eager-loaded alongside `community` via a
    JOIN, `image = db.relationship('File', foreign_keys=[image_id], ...)`,
    app/models.py:638 -- no `lazy='joined'` on this one specifically, but
    the same principle: it is never resolved through `File.query`), which
    this test seeded as a real, valid row and is unaffected by the patch
    below.

    See the icon-block sibling test above for the full reasoning on why
    `File.query` is replaced (patched only on `File`, not a
    rebound-import name) and why this state does not require breaking any
    database constraint to construct: it models a fresh lookup racing a
    concurrent hard delete of the row a stale, already-loaded relationship
    object still reflects.

    `delete_calls == []` is this test's one distinguishing assertion from
    the "matches neither" sibling above (`remove_file` is None here, so
    `:339`'s `delete_from_disk()` never runs); `community.image_id is None`
    is asserted regardless, since `:340`'s clear sits OUTSIDE `:338`'s `if
    remove_file:` block and runs either way -- a mutant moving it inside
    would leave `image_id` uncleared in exactly this state.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    banner_file = File(source_url='https://banner.example/original.png',
                       file_path='app/static/media/communities/banner.png')
    db.session.add(banner_file)
    db.session.commit()
    s.community.image_id = banner_file.id
    db.session.commit()
    image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: image_url_calls.append(url) or False)
    delete_calls = []
    monkeypatch.setattr(File, 'delete_from_disk', lambda self, *a, **kw: delete_calls.append(self.id))

    class _AlwaysMissingQuery:
        def get(self, ident):
            return None

    monkeypatch.setattr(File, 'query', _AlwaysMissingQuery())
    api_input = _api_input(icon_url=None, banner_url='https://banner.example/totally-different.png')

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert image_url_calls == ['https://banner.example/totally-different.png']
    assert delete_calls == []
    assert s.community.image_id is None


# `edit_community` (app/shared/community.py:345-346), the language delete.


def test_edit_community_language_delete_scoped_to_community_not_bystander(
        app, db_session, monkeypatch):
    """The raw `db.session.execute(text('DELETE FROM "community_language"
    WHERE community_id = :community_id'), ...)`. Seeded rows: two languages
    attached to `s.community` (English, French) and one attached to
    `s.bystander` (English, reused) -- inserted directly into the
    `community_language` association table with the identical raw-SQL idiom
    production uses, rather than through `Community.languages.append(...)`,
    which would route through the ORM's own collection machinery instead of
    this statement's actual table shape.

    After the call, `s.community`'s two seeded rows must be GONE, and
    `s.bystander`'s seeded row must SURVIVE -- the second half is what
    actually kills a mutant dropping the `WHERE community_id =
    :community_id` predicate, which would otherwise wipe every row in the
    table (including the bystander's) and still leave this test's first
    half passing on its own; sub-project 47 hit exactly this mechanism (c)
    three times, twice in one file.

    `:377-379`'s own append of the 'und' Language re-populates a single row
    for `s.community` (Task 3's territory, incidentally exercised here
    since `from_scratch=False` runs on through it) -- this test's own two
    languages are asserted gone BY ID, not by an empty-table count, so that
    re-population cannot mask a mutant that only partially applied the
    predicate.
    """
    s = _seed()
    _seed_und_language()
    english = Language(code='en', name='English')
    french = Language(code='fr', name='French')
    db.session.add_all([english, french])
    db.session.commit()
    db.session.execute(text('INSERT INTO "community_language" (community_id, language_id) '
                            'VALUES (:community_id, :language_id)'),
                       {'community_id': s.community.id, 'language_id': english.id})
    db.session.execute(text('INSERT INTO "community_language" (community_id, language_id) '
                            'VALUES (:community_id, :language_id)'),
                       {'community_id': s.community.id, 'language_id': french.id})
    db.session.execute(text('INSERT INTO "community_language" (community_id, language_id) '
                            'VALUES (:community_id, :language_id)'),
                       {'community_id': s.bystander.id, 'language_id': english.id})
    db.session.commit()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    monkeypatch.setattr('app.shared.community.is_image_url', lambda url: False)
    api_input = _api_input()

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    remaining = db.session.execute(text(
        'SELECT community_id, language_id FROM "community_language" '
        'WHERE community_id IN (:community_id, :bystander_id)'),
        {'community_id': s.community.id, 'bystander_id': s.bystander.id}).fetchall()
    remaining_pairs = {(row[0], row[1]) for row in remaining}

    assert (s.community.id, english.id) not in remaining_pairs
    assert (s.community.id, french.id) not in remaining_pairs
    assert (s.bystander.id, english.id) in remaining_pairs


# `edit_community` (app/shared/community.py:348-391), image creation, field
# assignment (already fully covered by the source-fork and permission-guard
# tests above, per this task's own baseline coverage run -- see the task
# report), the language block, and the return fork. Task 3's territory.
#
# NETWORK HAZARD, RESTATED FOR THIS SECTION: every test above kept
# `icon_url`/`banner_url` falsy specifically to keep `:348`/`:354`'s
# `is_image_url(...)` call from ever firing (see the module docstring).
# This section's target is exactly the branch that call sits on, so every
# test below that gives `icon_url` or `banner_url` a truthy value patches
# `app.shared.community.is_image_url` BEFORE that value can reach it --
# rebound on `app.shared.community`, never on `app.utils` where it is
# defined, for the same import-binding reason as `process_upload`/
# `task_selector` above (`from app.utils import ... is_image_url ...`,
# app/shared/community.py:23-25, binds the name into THIS module's globals
# at import time).
#
# BASELINE, MEASURED BEFORE ANY TEST IN THIS SECTION EXISTED (`--cov=app.
# shared.community` against only the 19 tests above): `edit_community`'s
# only missing statements were `349-353` (the icon-creation body) and
# `355-359` (its banner mirror), plus `373-375` (the discussion_languages
# loop, never exercised with a non-empty list) and the single missing
# branch `378->380` (the `undetermined.id not in discussion_languages`
# FALSE arm, never exercised because every prior test's discussion_languages
# was empty, making the `not in` trivially True every time). Lines `361-369`
# (the field-assignment block) and `388-391` (the return fork) were ALREADY
# fully covered, both branches, by the tests above -- restated here so a
# reader does not have to re-derive it: `test_edit_community_api_arm_reads_
# all_ten_keys_and_authorises_user` and its web-arm sibling already assert
# every one of `:361-369`'s eight fields (including `:365`'s
# `markdown_to_html`-transformed value, not the raw string) under
# `from_scratch=True`'s `return community` (`:388`'s TRUE arm), and
# `test_edit_community_permission_guard_moderator_not_admin_is_admitted`
# (and its admin-alone sibling) already assert `:391`'s `return user.id`
# under `from_scratch=False`. This section adds no new tests for either of
# those two ranges: doing so would duplicate assertions this file already
# makes elsewhere, not close a gap.


def test_edit_community_icon_creation_from_scratch_true_creates_file_and_sizes(
        app, db_session, monkeypatch):
    """`:348`'s PASSING case via its FIRST disjunct: `icon_url` truthy,
    `is_image_url` forced True, and `from_scratch=True` alone drives
    `(from_scratch or icon_url_changed)` True -- `icon_url_changed` stays at
    its `:319` initial value of False the whole way through, since
    `from_scratch=True` skips `:321-346` (the only place that sets it)
    entirely. The sibling test below
    (`..._icon_creation_via_icon_url_changed_when_not_from_scratch`) proves
    the SECOND disjunct alone suffices, with `from_scratch=False`; a mutant
    that dropped either name from the `or` (e.g. requiring both, or keeping
    only one) fails one of the two tests.

    `make_image_sizes`'s arguments are asserted as a tuple, not just that it
    was called once: `community.icon_id` (read AFTER the call -- `:352` sets
    it before `:353` reads it), `40`, `250`, `'communities'`, and
    `community.low_quality` read dynamically off the row rather than
    hardcoded `False`. An argument-drop or argument-swap mutant on `:353` is
    caught by this tuple comparison rather than a bare call-count assertion
    (sub-project 47's D639, registered for exactly this class of survivor).
    `banner_url` stays at the input builder's falsy default throughout so
    `:354`'s independent `is_image_url` call never fires and confounds the
    spy.
    """
    s = _seed()
    is_image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: is_image_url_calls.append(url) or True)
    make_image_sizes_calls = []
    monkeypatch.setattr('app.shared.community.make_image_sizes',
                        lambda *a: make_image_sizes_calls.append(a))
    icon_url = 'https://icon.example/new-icon.png'
    api_input = _api_input(icon_url=icon_url, banner_url=None)

    result = edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=True)

    assert result is s.community
    assert is_image_url_calls == [icon_url]
    new_file = File.query.filter_by(source_url=icon_url).one()
    assert s.community.icon_id == new_file.id
    assert make_image_sizes_calls == [
        (s.community.icon_id, 40, 250, 'communities', s.community.low_quality),
    ]


def test_edit_community_banner_creation_from_scratch_true_creates_file_and_sizes(
        app, db_session, monkeypatch):
    """`:354`'s PASSING case via its first disjunct -- the banner mirror of
    the icon test above. `icon_url` stays at the input builder's falsy
    default throughout so `:348`'s independent `is_image_url` call never
    fires and confounds the spy.
    """
    s = _seed()
    is_image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: is_image_url_calls.append(url) or True)
    make_image_sizes_calls = []
    monkeypatch.setattr('app.shared.community.make_image_sizes',
                        lambda *a: make_image_sizes_calls.append(a))
    banner_url = 'https://banner.example/new-banner.png'
    api_input = _api_input(icon_url=None, banner_url=banner_url)

    result = edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=True)

    assert result is s.community
    assert is_image_url_calls == [banner_url]
    new_file = File.query.filter_by(source_url=banner_url).one()
    assert s.community.image_id == new_file.id
    assert make_image_sizes_calls == [
        (s.community.image_id, 878, 1600, 'communities', s.community.low_quality),
    ]


def test_edit_community_icon_creation_via_icon_url_changed_when_not_from_scratch(
        app, db_session, monkeypatch):
    """`:348`'s PASSING case via its SECOND disjunct: `from_scratch=False`,
    but `icon_url_changed=True` -- set at `:327` by the same mismatch
    scenario Task 2's `icon_url_block_icon_url_matches_neither_clears_and_
    deletes` test uses (`icon_url` differs from both `community.icon.
    source_url` and its `medium_url()`) -- alone drives `(from_scratch or
    icon_url_changed)` True. Task 2's own test of this scenario forces
    `is_image_url` False to stay inside Task 2's territory and never reaches
    `:349-353`; this test forces it True instead, to reach them.

    Paired with the sibling test above, a mutant keeping only one of the
    disjunction's two names fails exactly one of this pair.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    old_icon = File(source_url='https://icon.example/original.png',
                    file_path='app/static/media/communities/thumb.png')
    db.session.add(old_icon)
    db.session.commit()
    s.community.icon_id = old_icon.id
    db.session.commit()
    is_image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: is_image_url_calls.append(url) or True)
    make_image_sizes_calls = []
    monkeypatch.setattr('app.shared.community.make_image_sizes',
                        lambda *a: make_image_sizes_calls.append(a))
    new_icon_url = 'https://icon.example/totally-different.png'
    api_input = _api_input(icon_url=new_icon_url, banner_url=None)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert is_image_url_calls == [new_icon_url]
    new_file = File.query.filter_by(source_url=new_icon_url).one()
    assert s.community.icon_id == new_file.id
    assert s.community.icon_id != old_icon.id
    assert make_image_sizes_calls == [
        (s.community.icon_id, 40, 250, 'communities', s.community.low_quality),
    ]


def test_edit_community_banner_creation_via_banner_url_changed_when_not_from_scratch(
        app, db_session, monkeypatch):
    """`:354`'s PASSING case via its second disjunct -- the banner mirror of
    the icon test above.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    old_banner = File(source_url='https://banner.example/original.png',
                      file_path='app/static/media/communities/banner.png')
    db.session.add(old_banner)
    db.session.commit()
    s.community.image_id = old_banner.id
    db.session.commit()
    is_image_url_calls = []
    monkeypatch.setattr('app.shared.community.is_image_url',
                        lambda url: is_image_url_calls.append(url) or True)
    make_image_sizes_calls = []
    monkeypatch.setattr('app.shared.community.make_image_sizes',
                        lambda *a: make_image_sizes_calls.append(a))
    new_banner_url = 'https://banner.example/totally-different.png'
    api_input = _api_input(icon_url=None, banner_url=new_banner_url)

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert is_image_url_calls == [new_banner_url]
    new_file = File.query.filter_by(source_url=new_banner_url).one()
    assert s.community.image_id == new_file.id
    assert s.community.image_id != old_banner.id
    assert make_image_sizes_calls == [
        (s.community.image_id, 878, 1600, 'communities', s.community.low_quality),
    ]


def test_edit_community_language_loop_appends_valid_skips_invalid(
        app, db_session, monkeypatch):
    """`:372-375`'s loop over `discussion_languages`: `:374`'s `if
    language:` TRUE arm (a valid id resolves via `Language.query.get` and is
    appended at `:375`) and FALSE arm (a nonexistent id resolves to `None`
    and is silently skipped) are both exercised in the SAME call,
    `discussion_languages` holding one of each -- a mutant dropping `:374`'s
    guard would still pass if only the valid-id case were tested (appending
    `None` would raise on to a relationship append, which is a crash, not a
    clean assertion failure), and a mutant that dropped the loop entirely
    would fail this test's `english.id in ...` assertion regardless.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    monkeypatch.setattr('app.shared.community.is_image_url', lambda url: False)
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    nonexistent_id = english.id + 10000
    assert Language.query.get(nonexistent_id) is None, 'test setup must pick a truly absent id'
    api_input = _api_input(discussion_languages=[english.id, nonexistent_id])

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    community_language_ids = {language.id for language in s.community.languages}
    assert english.id in community_language_ids
    assert nonexistent_id not in community_language_ids


def test_edit_community_und_already_in_discussion_languages_skips_duplicate_append(
        app, db_session, monkeypatch):
    """`:378`'s FALSE arm: `undetermined.id` is already IN
    `discussion_languages` (appended once already by `:372-375`'s loop,
    since 'und' is itself a valid `Language` row), so `:379`'s second append
    is skipped. This task's own baseline coverage run (before any test in
    this section existed) reported `378->380` as the one missing BRANCH
    inside `edit_community` -- every test above this section leaves
    `discussion_languages` empty, so `undetermined.id not in []` is always
    True and the FALSE arm this test isolates had never run.

    CORRECTED, PER CODE REVIEW: an earlier version of this docstring claimed
    a mutant that dropped `:378`'s guard and always appended would raise
    `IntegrityError` on `community_language`'s composite primary key
    (app/models.py:334-338), and that this made the FALSE arm's own
    behaviour unverifiable by a clean assertion. THAT CLAIM WAS NOT VERIFIED
    BY RUNNING THE MUTANT, AND IT WAS WRONG: applying `if True:` at `:378`
    by hand produces no `IntegrityError` at all. SQLAlchemy's flush-time
    dependency processing for a plain (non-association-object) many-to-many
    collection de-duplicates an object appended twice to the same
    relationship BEFORE emitting SQL, so `community_language` still ends up
    with exactly one `(community_id, und.id)` row either way -- the
    persisted state, and therefore `matching`/`len(matching)` below, is
    IDENTICAL whether the guard is present or not. `matching == 1` cannot
    tell the two apart; it was never a valid oracle for this branch's TRUE
    direction, and is kept below only for its ORIGINAL, still-valid purpose
    (proving `:379` does not raise / does not leave a second, distinct
    row -- see `_seed_und_language`'s own unique-code column, which a
    genuine duplicate INSERT attempt would violate before the relationship
    dedup even had a chance to apply, if the table lacked its composite key
    -- i.e. this assertion is a sanity check on the FINAL state, not a
    mutation oracle for the guard itself).

    The mutation oracle that actually works is a Python-level count of how
    many times `.append()` was CALLED on the collection, independent of
    what SQLAlchemy later folds into SQL: `sqlalchemy.event`'s `'append'`
    event on `Community.languages` fires once per `.append()` call, before
    any deduplication. A mutant that drops `:378`'s guard fires it TWICE for
    `und.id` in this exact scenario (once from `:372-375`'s loop, once more
    from the now-unconditional `:379`); the correct code fires it once. This
    is registered here as the fix for a false line in this task's own
    report (the crash claim), confirmed by hand-applying `if True:` at
    `:378` and observing `1 failed, 26 passed` with NO `IntegrityError` --
    the one failure was this task's own `:378`-missing-row pin test, dying
    for an UNRELATED reason (the guard's removal skips straight past the
    `.id` dereference that pin exists to catch), which is fix-catching, not
    a genuine kill, and does not by itself close this gap.
    """
    s = _seed()
    und = _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    monkeypatch.setattr('app.shared.community.is_image_url', lambda url: False)
    api_input = _api_input(discussion_languages=[und.id])
    append_calls = []

    def _record_append(target, value, initiator):
        if target is s.community:
            append_calls.append(value.id)
        return value

    event.listen(Community.languages, 'append', _record_append)
    try:
        edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)
    finally:
        event.remove(Community.languages, 'append', _record_append)

    matching = [language for language in s.community.languages if language.id == und.id]
    assert len(matching) == 1
    assert append_calls.count(und.id) == 1


def test_edit_community_undetermined_language_appended_and_correct_row_selected(
        app, db_session, monkeypatch):
    """CRITICAL FIX, per code review of this task's first submission:
    `:378`'s TRUE arm (`undetermined.id not in discussion_languages`, the
    ORDINARY case -- 'und' not already requested) was reached by every
    `from_scratch=False` test in this file, but NONE of them ever asserted
    that `:379`'s append actually happened: a hand-applied mutant changing
    `:378` to `if undetermined.id not in discussion_languages and False:`
    (permanently disabling the append) left all 27 of this task's
    then-existing tests passing. This test closes that gap directly:
    `discussion_languages` is empty (the input builder's own default, 'und'
    trivially not in it), so `:379` must run for `community.languages` to
    gain 'und' at all.

    A SECOND, independent gap shared the same blind spot: `:377`'s
    `Language.query.filter(Language.code == 'und').first()` -- every test
    in this file that reaches `:377` either has 'und' as the ONLY `Language`
    row, or inserts it before any other, so a mutant dropping the `.filter`
    predicate entirely (`Language.query.first()`) returns 'und' by
    insertion-order coincidence rather than by matching the code column --
    mechanism (c), emptiness with no negative control. `decoy`, a `Language`
    with a different code inserted BEFORE 'und', is the negative control:
    a predicate-dropped `.first()` returns `decoy` instead, so this test's
    assertions (comparing CODES, not merely counting rows) diverge from the
    correct result in that case too, closing both gaps with the one seed.
    """
    s = _seed()
    decoy = Language(code='xx', name='Decoy, must sort before und')
    db.session.add(decoy)
    db.session.commit()
    und = _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.task_selector', lambda *a, **kw: None)
    monkeypatch.setattr('app.shared.community.is_image_url', lambda url: False)
    api_input = _api_input()

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    community_language_codes = {language.code for language in s.community.languages}
    assert 'und' in community_language_codes
    assert 'xx' not in community_language_codes


def test_edit_community_task_selector_called_with_real_ids_not_literal(
        app, db_session, monkeypatch):
    """`:382`'s `task_selector('edit_community', user_id=user.id,
    community_id=community.id)`: arguments recorded and asserted, including
    that `community_id` is `s.community.id` and NOT a literal `1` --
    `_seed`'s bystander community consumes id 1 first (see the module
    docstring's id-1 trap note), so `s.community.id != 1` here, and a mutant
    hardcoding `community_id=1` at `:382` is caught by the tuple comparison
    below where a bare call-count assertion would miss it.
    """
    s = _seed()
    _seed_und_language()
    make_community_member(s.user, s.community, is_moderator=True)
    monkeypatch.setattr('app.shared.community.is_image_url', lambda url: False)
    calls = []
    monkeypatch.setattr('app.shared.community.task_selector',
                        lambda *a, **kw: calls.append((a, kw)))
    api_input = _api_input()

    edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)

    assert s.community.id != 1
    assert calls == [(('edit_community',), {'user_id': s.user.id, 'community_id': s.community.id})]


def test_edit_community_undetermined_language_missing_raises_attributeerror_registered_defect(
        app, db_session, monkeypatch):
    """REGISTERED, NOT FIXED: `:378`'s `undetermined.id` dereferences
    `:377`'s `Language.query.filter(Language.code == 'und').first()` result
    UNCONDITIONALLY -- if that lookup returns `None`, this raises
    `AttributeError: 'NoneType' object has no attribute 'id'`. Latent in
    production because `app/cli.py:181` seeds the 'und' row once at
    instance setup; this test does NOT delete that seed (every sibling test
    in this file shares `_seed_und_language` and deleting the row for real
    would break them within the same session) and does NOT even call
    `_seed_und_language` itself, so no 'und' row exists in this test's own
    database at all -- reflecting the genuinely-missing-row state directly
    rather than needing to fake it.

    `Language.query` is replaced on the class (matching Task 2's `File.
    query` stand-in technique, not a rebound `from ... import` name) with an
    object whose `.get` still delegates to the real query object (so
    `:373`'s loop, which runs first in the same call, is unaffected) and
    whose `.filter(...).first()` always returns `None`, so this test proves
    the crash from the row's absence specifically, not merely from an
    unpatched attribute error somewhere else in the call.
    """
    s = _seed()
    make_community_member(s.user, s.community, is_moderator=True)
    real_query = Language.query

    class _NoUndeterminedQuery:
        def get(self, ident):
            return real_query.get(ident)

        def filter(self, *a, **kw):
            class _EmptyResult:
                def first(self):
                    return None
            return _EmptyResult()

    monkeypatch.setattr(Language, 'query', _NoUndeterminedQuery())
    api_input = _api_input()

    with pytest.raises(AttributeError):
        edit_community(api_input, s.community, SRC_API, bearer(s.user), from_scratch=False)


# `make_community` (app/shared/community.py:213-251), the source fork, the
# verification guard, and the two name-collision checks -- Task 4's
# territory, and the first tests in this file to target `make_community`
# itself rather than `edit_community`.
#
# Task 5 owns `:253-290` (the Community row's own construction, the
# membership row, the language-population loop, and the two-shape return
# at :287-290). `make_community` is a single function body: once a test's
# guard and both existence checks pass, Python runs straight on through
# that range too -- there is no way to stop mid-call. Every test below
# that reaches a passing guard therefore incidentally exercises Task 5's
# territory, exactly as Task 1's `edit_community` fork tests incidentally
# exercised the field-assignment block below THEIR target range. No test
# below asserts anything about Task 5's own logic beyond what is needed to
# observe this task's own locals (the seven/seven keys the `:214` fork
# reads have no other externally visible trace) and to prove the two
# existence checks' FALSE arms did not wrongly raise.
#
# `make_community` HAS EXACTLY ONE PRODUCTION CALLER --
# `app/api/alpha/utils/community.py:221` -- and it ALWAYS passes SRC_API
# (confirmed by this file's own module docstring's grep, run once for the
# whole file, not repeated per task). The web/`else` arm at `:226-237` is
# therefore DEAD IN PRODUCTION, exactly like `edit_community`'s own web
# arm; every test below that reaches it repeats this disclosure in its own
# docstring, per this round's convention.
#
# AUTHORISE_API_USER'S OWN VERIFIED GUARD -- DISCOVERED WHILE WRITING THIS
# SECTION, NOT IN THE BRIEF: `app/utils.py:3628`'s
# `if user.ap_id is not None or user.verified is False or user.banned is
# True or user.deleted is True: raise Exception('incorrect_login')` runs
# INSIDE `authorise_api_user`, called at `make_community:224` -- BEFORE
# make_community's OWN `:239` guard ever executes, on the SRC_API arm.
# A SRC_API test built with `user.verified = False` never reaches `:239`
# at all: it dies at `:224` with `'incorrect_login'` instead, from a
# completely different function. The "unverified with a key" test below
# therefore uses the SRC_WEB arm (`user = current_user` at `:237`, which
# has no equivalent pre-check) to isolate `:239`'s own guard specifically.
# The "verified without a key" test safely uses SRC_API, since
# `authorise_api_user`'s guard never inspects `private_key` at all.


def _keyed_user(instance, name):
    """A local, verified user holding a real RSA keypair -- the one shape
    that passes make_community's `:239` guard (`user.verified is False or
    user.private_key is None`). `make_user`'s own defaults leave
    `private_key` at `None` (`with_keys=False`); `verified` already
    defaults `True`. `local=True` keeps `ap_id` `None`, which
    `authorise_api_user` also requires (see this section's header comment)
    for the SRC_API arm's own, separate guard.
    """
    return make_user(instance, name, local=True, with_keys=True)


def _existing_user_with_ap_profile_id(app, instance, unique_name, ap_profile_slug):
    """A `User` row whose `ap_profile_id` exactly matches the `/u/` shape
    `make_community`'s own existing-user check computes at `:243`
    (`'https://' + SERVER_NAME + '/u/' + name.lower()`).

    `make_user` never produces this shape itself: a local user
    (`local=True`) gets `ap_profile_id=None`, and a remote one gets
    `f'https://{domain}/users/{name}'` -- `/users/`, not `/u/`. Built via
    `make_user(local=False)` first, purely to satisfy every OTHER NOT NULL
    column the same way every other test in this suite does, then the one
    field under test is overwritten directly and re-committed -- the same
    technique this file already uses for its `File`/`Language` decoy rows.
    """
    user = make_user(instance, unique_name, local=False)
    user.ap_profile_id = f"https://{app.config['SERVER_NAME']}/u/{ap_profile_slug}"
    db.session.commit()
    return user


def test_make_community_guard_unverified_with_key_raises(app, db_session):
    """`:239`'s FIRST operand alone: `user.verified is False`, with a real
    private key present (`with_keys=True`), isolated via the SRC_WEB arm
    -- see this section's header comment for why the SRC_API arm cannot
    isolate this operand (`authorise_api_user`'s own, unrelated verified
    check would raise `'incorrect_login'` first).

    UNREACHABLE IN PRODUCTION -- see this section's header comment:
    `make_community` has exactly one production caller and it always
    passes SRC_API. This test reaches the web arm only by calling
    `make_community` directly with SRC_WEB.
    """
    s = _seed()
    user = _keyed_user(s.instance, 'unverified')
    user.verified = False
    db.session.commit()
    web_input = _web_input(url='newcommunity1')

    with web_ctx(app, user):
        with pytest.raises(Exception) as exc_info:
            make_community(web_input, SRC_WEB)

    assert str(exc_info.value) == "You can't create a community until your account is verified."


def test_make_community_guard_verified_without_key_raises(app, db_session):
    """`:239`'s SECOND operand alone: `user.private_key is None`, verified
    `True` (`make_user`'s own default) -- isolated via the SRC_API arm,
    which is safe here since `authorise_api_user`'s own guard (see this
    section's header comment) never inspects `private_key`.
    """
    s = _seed()
    user = make_user(s.instance, 'unkeyed', local=True)
    api_input = _api_input(name='newcommunity2')

    with pytest.raises(Exception) as exc_info:
        make_community(api_input, SRC_API, bearer(user))

    assert str(exc_info.value) == "You can't create a community until your account is verified."


def test_make_community_guard_verified_with_key_passes_and_creates_community(app, db_session):
    """The passing case: both `:239` operands `False` (verified `True`,
    `private_key` set via `with_keys=True`) -- no raise; execution
    proceeds into this task's own existence checks and on through Task 5's
    own `:253-290` territory (incidentally; see this section's header
    comment).

    `plugins.fire_hook` (`:285`) is a real, unpatched call -- it is a
    no-op for any hook name with no registered handler
    (`app/plugins/hooks.py:62-63`'s `if hook_name not in _hooks: return
    data`), and this suite registers none, so it introduces no side
    effect worth mocking.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'creator')
    api_input = _api_input(name='brandnewcommunity')

    result = make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='brandnewcommunity').one()
    assert result == (user.id, community.id)


def test_make_community_api_arm_slugifies_name_and_reads_seven_keys(app, db_session):
    """`:214`'s TRUE arm (`src == SRC_API`): `input['name']` is slugified
    in place at `:215`, and the seven keys at `:215-223` (name, title,
    nsfw, restricted_to_mods, local_only, discussion_languages,
    question_answer) are read into locals. `user = authorise_api_user(
    auth, return_type='model')` (`:224`) resolves the acting user from the
    bearer token, NOT `current_user` -- the web arm's source, covered by
    the sibling tests below.

    Every boolean key is set to its NON-default value, and `name` to a
    value containing spaces and mixed case, so the slugified result and
    each field assignment are distinct from any coincidental default. The
    expected slug is produced by calling the real `slugify` helper
    independently (`separator='_'`, then `.lower()`, matching `:215`
    exactly), not hand-derived, matching this suite's established
    convention.

    Execution continues on through Task 5's `:253-290` territory to make
    these locals observable at all (they have no other externally visible
    trace) -- this test asserts only the fields this task's own fork
    reads, not on Task 5's own creation logic beyond that.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'apicreator')
    raw_name = 'My Cool COMMUNITY!'
    expected_name = slugify(raw_name, separator='_').lower()
    api_input = _api_input(name=raw_name, title='API Title', nsfw=True,
                           restricted_to_mods=True, local_only=True,
                           question_answer=True)

    make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name=expected_name).one()
    assert community.title == 'API Title'
    assert community.nsfw is True
    assert community.restricted_to_mods is True
    assert community.local_only is True
    assert community.question_answer is True


def test_make_community_web_arm_strips_leading_c_prefix_and_reads_seven_attributes(
        app, db_session):
    """`:226`'s TRUE arm: `input.url.data` starts with `/c/`
    (case-/whitespace-insensitively, per `:226`'s own `.strip().lower()`),
    so `:227` strips exactly its first three characters before `:228`'s
    slugify. `user = current_user` (`:237`) resolves the acting user from
    the login context, not a bearer token -- `_web_input`'s shape is read
    via `.data` on each `_Field`, matching `edit_community`'s identical
    web arm above; `make_community` reads seven of its eight attributes
    (url, community_name, nsfw, restricted_to_mods, local_only, languages,
    question_answer -- not `description`/`rules`, per `_web_input`'s own
    docstring).

    UNREACHABLE IN PRODUCTION -- see this section's header comment:
    `make_community` has exactly one production caller and it always
    passes SRC_API. This test reaches the web arm only by calling
    `make_community` directly with SRC_WEB.

    CORRECTED, PER CODE REVIEW: an earlier version of this test discarded
    `:290`'s own return value (`return community.name`, the SRC_WEB arm's
    return) and re-derived the row via `Community.query.filter_by(name=
    expected_name).one()`. That `.one()` converts a `:226` divergence into
    `sqlalchemy.exc.NoResultFound` -- a crash, not a clean `AssertionError`
    -- before any field assertion can run, since a wrong slug means no row
    matches `expected_name` at all. Capturing `result` and asserting it
    equals `expected_name` DIRECTLY catches that divergence as a plain
    string-comparison `AssertionError` first; the row is then looked up by
    `result` itself (guaranteed to exist, since it is the exact name the
    call just used), not by the independently-computed `expected_name`, so
    the subsequent field assertions can never spuriously miss.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'webcreator1')
    raw_url = '/c/My Web COMMUNITY!'
    expected_name = slugify(raw_url[3:].strip(), separator='_').lower()
    web_input = _web_input(url=raw_url, community_name='Web Title', nsfw=True,
                           restricted_to_mods=True, local_only=True,
                           question_answer=True)

    with web_ctx(app, user):
        result = make_community(web_input, SRC_WEB)

    assert result == expected_name
    community = Community.query.filter_by(name=result).one()
    assert community.title == 'Web Title'
    assert community.nsfw is True
    assert community.restricted_to_mods is True
    assert community.local_only is True
    assert community.question_answer is True


def test_make_community_web_arm_without_c_prefix_reads_seven_attributes_unchanged(
        app, db_session):
    """`:226`'s FALSE arm: `input.url.data` does not start with `/c/`, so
    `:227`'s strip never runs and `:228`'s slugify operates on the whole,
    unstripped string.

    UNREACHABLE IN PRODUCTION -- see this section's header comment.

    CORRECTED, PER CODE REVIEW: same fix as the sibling test above --
    `result` is captured and asserted directly against `expected_name`
    (a plain `AssertionError` on divergence), and the row is then looked
    up by `result`, not the independently-computed `expected_name`, so a
    `:226` mutation cannot turn this test's failure into `NoResultFound`.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'webcreator2')
    raw_url = 'My Other COMMUNITY!'
    expected_name = slugify(raw_url.strip(), separator='_').lower()
    web_input = _web_input(url=raw_url, community_name='Other Web Title', nsfw=True,
                           restricted_to_mods=True, local_only=True,
                           question_answer=True)

    with web_ctx(app, user):
        result = make_community(web_input, SRC_WEB)

    assert result == expected_name
    community = Community.query.filter_by(name=result).one()
    assert community.title == 'Other Web Title'
    assert community.nsfw is True
    assert community.restricted_to_mods is True
    assert community.local_only is True
    assert community.question_answer is True


def test_make_community_existing_user_with_same_name_raises_exact_message(app, db_session):
    """`:243-246`'s TRUE arm: a `User` row already holds the exact `/u/`
    `ap_profile_id` the candidate name would produce. `:246`'s message is
    asserted EXACTLY, case-sensitively, per this round's disclosure
    requirement -- it is the only one of this module's three collision
    messages capitalised `'A User...'`, distinguishing it from `:251`'s
    and `:268`'s lowercase/capitalised `'community'`/`'Community'`
    messages.
    """
    s = _seed()
    user = _keyed_user(s.instance, 'namecollider')
    target_slug = 'takenname'
    _existing_user_with_ap_profile_id(app, s.instance, 'blocker', target_slug)
    api_input = _api_input(name=target_slug)

    with pytest.raises(Exception) as exc_info:
        make_community(api_input, SRC_API, bearer(user))

    assert str(exc_info.value) == \
        'A User with that name already exists, so it cannot be used for a Community'


def test_make_community_no_existing_user_conflict_creates_successfully(app, db_session):
    """`:243-246`'s FALSE arm, WITH a decoy present: an unrelated `User`
    already holds a DIFFERENT `/u/` `ap_profile_id`. Mechanism (c):
    `:244`'s `filter_by(ap_profile_id=...)` is a lookup of exactly the
    shape a predicate-dropped mutant could satisfy by matching ANY row
    instead of none -- without a decoy, `User.query.first()` on a
    near-empty table would have nothing to wrongly return, and a
    predicate-dropped mutant would be invisible to this test. If `:244`'s
    filter were reduced to `User.query.first()`, this decoy row (or
    `_seed()`'s own burn user/`s.user`, both present regardless) would be
    wrongly returned and this call would incorrectly raise -- this test's
    plain success assertion diverges from that outcome.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'namecollider2')
    _existing_user_with_ap_profile_id(app, s.instance, 'decoyuser', 'someone-else-entirely')
    api_input = _api_input(name='freshcommunityname')

    make_community(api_input, SRC_API, bearer(user))

    assert Community.query.filter_by(name='freshcommunityname').one() is not None


def test_make_community_existing_community_with_same_name_raises_lowercase_c_message(
        app, db_session):
    """`:247-251`'s TRUE arm: `s.community` (from `_seed()`, name
    'microblogs', hosted on SERVER_NAME) already holds the exact `/c/`
    `ap_profile_id` a new community named 'microblogs' would collide with.
    `:251`'s message is asserted EXACTLY, case-sensitively -- LOWERCASE
    'community', which is what distinguishes this line from Task 5's
    `:268` (`'Community with that name already exists'`, capital C, the
    IntegrityError-caught duplicate-insert fallback). A case-insensitive
    match cannot tell the two apart; this test proves the code took THIS
    check (`:250`'s pre-emptive lookup), not the DB-level fallback.
    """
    s = _seed()
    user = _keyed_user(s.instance, 'communitycollider')
    api_input = _api_input(name='microblogs')

    with pytest.raises(Exception) as exc_info:
        make_community(api_input, SRC_API, bearer(user))

    assert str(exc_info.value) == 'community with that name already exists'


def test_make_community_no_existing_community_conflict_creates_successfully(app, db_session):
    """`:247-251`'s FALSE arm, WITH decoys present: `_seed()`'s own
    `s.community` ('microblogs', SERVER_NAME host) and `s.bystander`
    ('bystander', a DIFFERENT host) already give two `Community` rows with
    two DIFFERENT `ap_profile_id`s in the table before this call --
    exactly the mechanism (c) negative control the module docstring's
    id-1 trap note says `.bystander` exists partly for. If `:249`'s
    `filter_by(ap_profile_id=...)` were reduced to a predicate-dropped
    `.first()`, one of these two rows would be wrongly returned and this
    test's plain success assertion would diverge into an unexpected
    raise.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'communitycollider2')
    api_input = _api_input(name='totallyfreshcommunity')

    make_community(api_input, SRC_API, bearer(user))

    assert Community.query.filter_by(name='totallyfreshcommunity').one() is not None


def test_make_community_guard_verified_none_registered_defect_not_blocked(app, db_session):
    """REGISTERED, NOT FIXED: `:239`'s `user.verified is False` is an `is`
    identity check, not a truthiness check -- `app/models.py:981`'s
    `verified = db.Column(db.Boolean, default=False)` carries no
    `nullable=False`, so `verified=None` is a constructible state, and
    `None is False` evaluates `False`. Combined with a real private key
    (`with_keys=True`), `:239`'s guard does NOT raise for a user who was
    never actually verified -- `None` is neither `True` nor `False`, and
    only the LATTER is checked. `authorise_api_user`'s own, separate
    verified check (see this section's header comment) uses the identical
    `is False` idiom, so it does not block this state either, and the
    SRC_API arm can be used directly.

    This test pins today's actual, defective behaviour: an unverified
    (`verified=None`, never through the real verification flow) but keyed
    user's community creation SUCCEEDS regardless.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'nullverified')
    user.verified = None
    db.session.commit()
    assert user.verified is None, 'test setup must produce a genuine None, not a falsy True/False'
    api_input = _api_input(name='defectcommunity')

    make_community(api_input, SRC_API, bearer(user))

    assert Community.query.filter_by(name='defectcommunity').one() is not None


# `make_community` (app/shared/community.py:253-290), the last uncovered group
# in this module: the Community construction, the IntegrityError fallback,
# membership creation, the discussion_languages/'und' language block, the
# `edit_community(..., from_scratch=True)` call, the plugin hook, and the
# return fork. Task 4 owns :214-251, closed with zero missing lines/branches
# in that range; every test below necessarily runs through :214-251 too
# (Python does not stop at :251), but asserts nothing about that range beyond
# what Task 4 already established.
#
# BASELINE, MEASURED BEFORE ANY TEST IN THIS SECTION EXISTED (`--cov=app.
# shared.community` against only the 39 tests above): the only missing lines
# inside :213-290 were `266, 267, 268` (the `except IntegrityError:` fallback,
# never reached -- no prior test ever produced a genuine duplicate-key
# collision) and `273, 274, 275` (the discussion_languages loop body, never
# reached -- every prior test's discussion_languages was the input builder's
# own empty-list default). The only missing BRANCHES inside that range were
# `[272, 273]` (the loop never entered at all), `[274, 272]` and `[274, 275]`
# (both arms of `if language:`, unreached for the same reason), and
# `[278, 280]` (`:278`'s FALSE arm -- 'und' already in discussion_languages --
# unreached because every prior test's discussion_languages was empty, making
# `undetermined.id not in []` trivially True every time).


def test_make_community_construction_sets_ap_fields_and_membership_flags(app, db_session):
    """`:253-262`'s `Community` construction, `:261`'s FALSE arm ('memes' not
    in the candidate name -- `low_quality` stays `False`), and `:270`'s
    `CommunityMember` row: both `is_moderator=True` and `is_owner=True` are
    asserted, not just that a row exists, so a mutant dropping either keyword
    (leaving the column's own `default=False`, app/models.py:3514-3515) is
    caught.

    `ap_profile_id`/`ap_public_url`/`ap_followers_url`/`ap_domain` are
    compared against independently-built expected strings (not re-derived
    from the community row itself), and `instance_id`/`subscriptions_count`
    against their literal `:261` values, so a mutant altering any one of the
    six construction keywords is caught by its own assertion rather than a
    single blanket check.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'fieldscreator')
    api_input = _api_input(name='freshfields')

    make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='freshfields').one()
    server_name = app.config['SERVER_NAME']
    assert community.ap_profile_id == f'https://{server_name}/c/freshfields'
    assert community.ap_public_url == f'https://{server_name}/c/freshfields'
    assert community.ap_followers_url == f'https://{server_name}/c/freshfields/followers'
    assert community.ap_domain == server_name
    assert community.instance_id == 1
    assert community.subscriptions_count == 1
    assert community.low_quality is False
    membership = CommunityMember.query.filter_by(user_id=user.id, community_id=community.id).one()
    assert membership.is_moderator is True
    assert membership.is_owner is True


def test_make_community_low_quality_true_when_name_contains_memes(app, db_session):
    """`:261`'s TRUE arm: a candidate name containing the substring 'memes'
    is created with `low_quality=True` -- real behaviour, not an accident of
    this test's fixture, per the brief. Paired with the sibling test above
    (a name with no 'memes' substring, `low_quality=False`), a mutant that
    forced either constant value regardless of the name, or inverted the
    `in` check, fails exactly one of the two.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'memescreator')
    api_input = _api_input(name='cool_memes_hub')

    make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='cool_memes_hub').one()
    assert community.low_quality is True


def test_make_community_genuine_duplicate_raises_capital_c_message_and_rolls_back(
        app, db_session, monkeypatch):
    """`:263-268`'s `try`/`except IntegrityError:` fallback, WITH A GENUINE,
    UNMOCKED integrity violation -- not `IntegrityError` raised directly by
    the test.

    REACHABILITY, established by reading the schema before writing this
    test: `Community.ap_profile_id` (app/models.py:595) carries the ONLY
    unique constraint this collision can violate
    (`db.Column(db.String(255), index=True, unique=True)`) -- `name` itself
    (`:561`) has no `unique=True`. `:249`'s own pre-check queries by that
    EXACT SAME column (`db.session.query(Community).filter_by(ap_profile_id=
    ap_profile_id).first()`), so under ordinary, single-threaded execution
    `:249` always catches a collision before `:265`'s insert is ever
    attempted -- `:268` is reachable ONLY by a genuine TOCTOU race (a second
    writer's commit lands between `:249`'s read and `:265`'s write), which a
    single test process cannot produce by simply calling `make_community`
    twice in sequence (the first call's own `:249`/`:250` check would catch
    the second). This is NOT fact 75 cause 8 ("Unreachable handler"): cause
    8 is proved by reading the callee's raising paths and showing NONE of
    them can fire for the call site's argument shape; here the callee
    (Postgres' own unique index) DOES have a raising path for this exact
    argument shape, and the only obstacle is `:249` running first in the
    SAME process -- a reachability gap from test-harness single-threading,
    not a proof that the `except` body can never execute for any input.

    The race is reproduced by leaving `:265`'s INSERT and the database's own
    unique index on `ap_profile_id` completely real, and blinding ONLY
    `:249`'s own lookup: `db.session.query` is patched (rebound on the
    `db.session` instance, per this round's constraint) so that a call whose
    first positional argument is `Community` returns a stub whose
    `filter_by(...).first()` always answers `None`, regardless of what the
    table actually contains -- exactly modelling `:249` reading a snapshot
    that predates the concurrent writer's commit. Every OTHER `db.session.
    query(...)` call (there are none elsewhere on this call path) and every
    `User.query`/`Language.query` call (a different attribute entirely, see
    `:244`'s own `User.query.filter_by(...)`, unaffected by this patch) pass
    through unchanged. A real `Community` row with the EXACT `ap_profile_id`
    the candidate name will produce is inserted first, via the same
    `make_community_factory` row-builder `_seed()` itself uses, at the
    default host (`test.piefed.local`), which is `app.config['SERVER_NAME']`
    in this test config (`.env.test:7`) -- confirmed by this file's own
    `:250` collision test using the identical assumption. So `:265`'s
    `db.session.commit()` genuinely violates the real unique index and
    Postgres genuinely raises `IntegrityError` -- nothing about the
    exception itself is mocked, only the pre-check that would otherwise
    have prevented reaching it.

    The message is asserted EXACTLY, case-sensitively: `'Community with
    that name already exists'`, capital C, at `:268` -- distinct from
    `:251`'s lowercase-c `'community with that name already exists'`, the
    pre-emptive check's own message, per this round's disclosure
    requirement (Task 4's `:250` test already makes the same point from the
    other side).

    `monkeypatch.undo()` restores `db.session.query` immediately after the
    call, before the follow-up assertion, so that assertion is provably
    unaffected by the patch regardless of whatever internal machinery
    `Community.query`'s own query-property does or does not share with
    `db.session.query` -- not merely assumed safe because `Community.query`
    is a different attribute.

    `_seed_und_language()` is deliberately NOT called here: the raise at
    `:268` happens before execution ever reaches Task 5's own
    `:270-280` membership/language block, so the module docstring's
    `_seed_und_language` crash hazard does not apply to this test at all.
    """
    s = _seed()
    user = _keyed_user(s.instance, 'racer')
    make_community_factory('racecommunity')
    real_query = db.session.query

    class _AlwaysEmptyCommunityFilter:
        def filter_by(self, **kwargs):
            class _Result:
                def first(self):
                    return None
            return _Result()

    def _query_stub(*args, **kwargs):
        if args and args[0] is Community:
            return _AlwaysEmptyCommunityFilter()
        return real_query(*args, **kwargs)

    monkeypatch.setattr(db.session, 'query', _query_stub)
    api_input = _api_input(name='racecommunity')

    with pytest.raises(Exception) as exc_info:
        make_community(api_input, SRC_API, bearer(user))
    monkeypatch.undo()

    assert str(exc_info.value) == 'Community with that name already exists'
    surviving = Community.query.filter_by(name='racecommunity').one()
    assert surviving.ap_profile_id == f"https://{app.config['SERVER_NAME']}/c/racecommunity"


def test_make_community_discussion_languages_loop_appends_valid_skips_invalid(
        app, db_session):
    """`:272-275`'s loop over `discussion_languages`: `:274`'s `if
    language:` TRUE arm (a valid id resolves via `Language.query.get` and is
    appended at `:275`) and FALSE arm (a nonexistent id resolves to `None`
    and is silently skipped) are both exercised in the SAME call -- Task
    4's tests all passed an empty list, so this loop body was entirely
    unexercised before this test (see this section's baseline note).

    Mechanism (c), the SAME shape the module docstring warns about for
    `:277`'s filter: `:273`'s `Language.query.get(language_choice)` is a
    lookup by a SPECIFIC id, reducible by a mutant to `Language.query.
    first()` (ignoring `language_choice` entirely) with no negative control
    if only one `Language` row existed. `decoy`, inserted FIRST with a
    DIFFERENT id than `english`'s, is that negative control: a
    `.get()`-to-`.first()` mutant would wrongly resolve BOTH loop iterations
    to `decoy` regardless of `language_choice`, so `decoy.id` would appear
    in `community.languages` and `english.id` would not -- this test's
    assertions (checked by id, not by count) diverge from the correct
    result in exactly that case.
    """
    s = _seed()
    decoy = Language(code='zz', name='Decoy, must sort before english')
    db.session.add(decoy)
    db.session.commit()
    english = Language(code='en', name='English')
    db.session.add(english)
    db.session.commit()
    _seed_und_language()
    user = _keyed_user(s.instance, 'languagecreator')
    nonexistent_id = english.id + 10000
    assert Language.query.get(nonexistent_id) is None, 'test setup must pick a truly absent id'
    api_input = _api_input(name='languagecommunity',
                           discussion_languages=[english.id, nonexistent_id])

    make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='languagecommunity').one()
    community_language_ids = {language.id for language in community.languages}
    assert english.id in community_language_ids
    assert decoy.id not in community_language_ids
    assert nonexistent_id not in community_language_ids


def test_make_community_und_not_already_requested_gets_appended(app, db_session):
    """`:277-279`'s TRUE arm: `undetermined.id` is NOT already in
    `discussion_languages` (the input builder's own empty-list default), so
    `:279` appends it.

    Same mechanism (c) hazard as `:273` above, for `:277`'s own
    `Language.query.filter(Language.code == 'und').first()`: `decoy`, a
    `Language` with a DIFFERENT code inserted BEFORE 'und', is the negative
    control -- a predicate-dropped `.first()` (ignoring the `code == 'und'`
    filter) would wrongly resolve to `decoy` instead, and this test's
    code-based assertions (not a bare row count) diverge from the correct
    result in that case, matching `edit_community`'s identical `:377`
    test (`test_edit_community_undetermined_language_appended_and_correct_
    row_selected`) this one mirrors.
    """
    s = _seed()
    decoy = Language(code='xx', name='Decoy, must sort before und')
    db.session.add(decoy)
    db.session.commit()
    _seed_und_language()
    user = _keyed_user(s.instance, 'undappendcreator')
    api_input = _api_input(name='undappendcommunity')

    make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='undappendcommunity').one()
    community_language_codes = {language.code for language in community.languages}
    assert 'und' in community_language_codes
    assert 'xx' not in community_language_codes


def test_make_community_und_already_requested_skips_duplicate_append(app, db_session):
    """`:277-279`'s FALSE arm: `undetermined.id` IS already in
    `discussion_languages`, so `:279`'s second append is skipped.

    SAME ORM-NORMALISATION HAZARD `edit_community`'s identical `:378` test
    registered (`test_edit_community_und_already_in_discussion_languages_
    skips_duplicate_append`'s docstring, verified there by hand-applying `if
    True:` at that line and observing NO `IntegrityError` and an IDENTICAL
    persisted row count either way): SQLAlchemy's flush-time dependency
    processing for a plain many-to-many collection de-duplicates an object
    appended twice BEFORE emitting SQL, so `community.languages`'s final
    state cannot distinguish a guarded single append from an unguarded
    double append -- `len(matching) == 1` below is kept only as a sanity
    check on the FINAL state, not as this test's mutation oracle. The actual
    oracle is a Python-level count of `.append()` CALLS on the collection,
    upstream of that normalisation: `sqlalchemy.event`'s `'append'` event on
    `Community.languages` fires once per `.append()` call. A mutant that
    drops `:278`'s guard fires it TWICE for `und.id` in this exact scenario
    (once from `:272-275`'s loop, since 'und' is itself a valid `Language`
    row and its own id is in `discussion_languages`; once more from the now
    -unconditional `:279`); the correct code fires it once.
    """
    s = _seed()
    und = _seed_und_language()
    user = _keyed_user(s.instance, 'undskipcreator')
    api_input = _api_input(name='undskipcommunity', discussion_languages=[und.id])
    # The community doesn't exist yet at listener-registration time (it is
    # created INSIDE the call this listener wraps), so every append on
    # `Community.languages` is recorded regardless of target and filtered
    # afterward by object identity -- simpler than pre-computing an id, and
    # equally precise, since this test creates exactly one community and
    # relies on the same session's identity map to return that SAME object
    # from the query below (no expire/close happens in between).
    append_calls = []

    def _record_append(target, value, initiator):
        append_calls.append((target, value.id))
        return value

    event.listen(Community.languages, 'append', _record_append)
    try:
        make_community(api_input, SRC_API, bearer(user))
    finally:
        event.remove(Community.languages, 'append', _record_append)

    community = Community.query.filter_by(name='undskipcommunity').one()
    matching = [language for language in community.languages if language.id == und.id]
    assert len(matching) == 1
    community_append_calls = [value_id for target, value_id in append_calls if target is community]
    assert community_append_calls.count(und.id) == 1


def test_make_community_calls_edit_community_from_scratch_and_returns_api_tuple(app, db_session):
    """`:282`'s `community = edit_community(input, community, src, auth,
    uploaded_icon_file, uploaded_banner_file, from_scratch=True)` -- per the
    module docstring's oracle check, this is the ONLY path in the whole
    codebase that reaches `edit_community`'s `from_scratch=True` arm (its
    other production caller, app/api/alpha/utils/community.py:261, always
    passes the default `from_scratch=False`).

    `description`/`rules` are the proof this call actually ran and its
    result was kept: `make_community`'s OWN construction (`:254-262`) never
    reads either key (RULING 1, this file's module docstring), so if `:282`
    were deleted or its return value discarded, the persisted community
    would have `description`/`rules` at their column defaults (`None`), not
    the values asserted below. `description` is asserted UNCHANGED from the
    raw input (no `\\r\\n`-normalising transform), matching `edit_community`'s
    own SRC_API-arm test above (`test_edit_community_api_arm_reads_all_ten_
    keys_and_authorises_user`) -- this call reaches that same arm, since
    `make_community`'s own `src` (`SRC_API`) is passed straight through.

    `:287`'s TRUE arm (`src == SRC_API`, `return user.id, community.id`) is
    asserted directly against the returned tuple -- the web arm's `:290`
    (`return community.name`) is already asserted by Task 4's `test_make_
    community_web_arm_strips_leading_c_prefix_and_reads_seven_attributes`
    and its sibling, so it is not re-tested here.

    AN ORDER TRAP, CAUGHT BY ACTUALLY RUNNING THE MUTATION (see this task's
    own report): `_seed()` mints its bystander/burn rows in lockstep for
    both the `User` and `Community` id sequences, so without the extra
    `make_user` call below, THIS test's own acting user and its own new
    community land on the SAME numeric id (both the 4th row minted in their
    respective tables) -- `result == (user.id, community.id)` would then
    pass identically whether `:288` returned `(user.id, community.id)` or
    the SWAPPED `(community.id, user.id)`, silently failing to discriminate
    argument order. `_decoy_user` below burns one extra `User` id first,
    desynchronising the two sequences so `user.id != community.id` and an
    order swap is actually observable.
    """
    s = _seed()
    _seed_und_language()
    _decoy_user = make_user(s.instance, 'editscratchdecoy', local=True)
    user = _keyed_user(s.instance, 'editscratchcreator')
    api_input = _api_input(name='editscratchcommunity', description='Custom Desc',
                           rules='Custom Rules')

    result = make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='editscratchcommunity').one()
    assert user.id != community.id, 'test setup must desynchronise the two id sequences'
    assert result == (user.id, community.id)
    assert community.description == 'Custom Desc'
    assert community.rules == 'Custom Rules'


def test_make_community_fires_new_local_community_plugin_hook(app, db_session, monkeypatch):
    """`:285`'s `plugins.fire_hook("new_local_community", community)`.
    `plugins` (`from app import db, cache, plugins`, app/shared/community.py
    :12) is a module-level name bound into THIS module's globals at import
    time, so the patch is rebound on `app.shared.community.plugins` itself,
    never on `app.plugins`, matching this round's constraint and this
    file's established convention for `process_upload`/`task_selector`/
    `is_image_url` above.

    The exact positional arguments are asserted, not just the call count: a
    mutant that passed the wrong hook name, or the community's id/name
    instead of the object itself, is caught by this tuple comparison. The
    SAME `community` object `make_community`'s own `:287-290` return fork
    would use is asserted as the second argument (`is`, not `==`), proving
    the hook fires with the fully-`edit_community`-processed row from
    `:282`, not a stale reference from before that call.
    """
    s = _seed()
    _seed_und_language()
    user = _keyed_user(s.instance, 'hookcreator')
    api_input = _api_input(name='hookcommunity')
    calls = []

    class _FakePlugins:
        def fire_hook(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr('app.shared.community.plugins', _FakePlugins())

    make_community(api_input, SRC_API, bearer(user))

    community = Community.query.filter_by(name='hookcommunity').one()
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args == ('new_local_community', community)
    assert kwargs == {}
    assert args[1] is community
