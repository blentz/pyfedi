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

from app.constants import SRC_API, SRC_WEB
from app.shared.community import edit_community
from app.utils import markdown_to_html, piefed_markdown_to_lemmy_markdown
from tests.factories import bearer, make_community, make_instance, make_user, web_ctx


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
    bystander = make_community('bystander', host='bystander.example')
    community = make_community()
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
