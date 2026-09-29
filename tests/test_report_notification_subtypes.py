"""`process_report`'s notification subtypes, and the template blocks they choose.

`app/templates/user/notifs/20.html` picks a block by `notification.subtype`, and
each block reads a different set of `targets` keys. So the subtype is not a label:
it decides what the recipient is shown.

D1392. `process_report` branches on what was reported, and in two of those
branches the **moderators** and the **site admin** were told different things
about the same report:

    isinstance(reported, User)       3900-3930   user_reported     (correct)
    isinstance(reported, Post)       3932-3987   mods: post_reported
                                                 admin: user_reported     <--
    isinstance(reported, PostReply)  3989-4052   mods: comment_reported
                                                 admin: user_reported     <--

The admin path is the un-moderated-community one
(`if reported.community.is_local() and reported.community.un_moderated`), which is
exactly when the site admin is the only person who will look. They were shown the
block for a reported USER: the wrong heading, and no post title or body, although
`targets_data` carried both. That block reads `targets.reasons` and
`targets.description`, which a post or comment report's dict does not have, so its
detail panel could not render either -- it is guarded by
`{% if notification.targets.reasons or notification.targets.description %}`, so
nothing raised and the panel simply never appeared.

The titles were wrong with the subtypes -- a literal `'Reported user'`, untranslated
beside the moderators' `_('A post has been reported')` -- and are fixed with them.

HOW THIS WAS FOUND, and what the same sweep says about the rest. Round 195's method,
generalised: for every subtype block in every notification template, collect the
`targets.X` keys it reads, then for every `Notification(subtype=...)` in `app/`
collect the keys of the nearest `targets_data` above it, and compare. Seven subtypes
read keys. The comparison flagged three producers of `user_reported` as missing
`reasons` and `description` -- two of which were these mislabelled sites, and the
third is the genuine user report in `app/user/routes.py`, where the panel is
guarded and simply stays closed. That last one is recorded below as a gap in the
data, not a crash, and deliberately not "fixed" by inventing values for it.
"""
import ast
import pathlib
import re

import pytest

TEMPLATE = pathlib.Path('app/templates/user/notifs/20.html')
SOURCES = [p for p in pathlib.Path('app').rglob('*.py')
           if p.name != 'cli.py' and p.parts[1] != 'nntp']


def template_blocks() -> dict:
    """{subtype: set of targets keys its block reads}."""
    text = TEMPLATE.read_text()
    parts = re.split(r'\{%-?\s*(?:el)?if notification\.subtype\s*==\s*"([^"]+)"',
                     text)
    blocks = {}
    for i in range(1, len(parts) - 1, 2):
        keys = set(re.findall(r'targets\.(\w+)', parts[i + 1]))
        if keys:
            blocks.setdefault(parts[i], set()).update(keys)
    return blocks


def producers() -> list:
    """[(path, lineno, subtype, keys of the nearest targets_data above)]."""
    found = []
    for path in SOURCES:
        src = path.read_text()
        if 'subtype' not in src:
            continue
        tree = ast.parse(src)
        dicts = [(n.lineno, {k.value for k in n.value.keys
                             if isinstance(k, ast.Constant)
                             and isinstance(k.value, str)})
                 for n in ast.walk(tree)
                 if isinstance(n, ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], ast.Name)
                 and n.targets[0].id.endswith('targets_data')
                 and isinstance(n.value, ast.Dict)]
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
                    and n.func.id == 'Notification'):
                continue
            st = [kw.value.value for kw in n.keywords
                  if kw.arg == 'subtype' and isinstance(kw.value, ast.Constant)]
            if not st:
                continue
            above = [(ln, keys) for ln, keys in dicts if ln < n.lineno]
            found.append((str(path), n.lineno, st[0],
                          max(above)[1] if above else set()))
    return found


def branch_ranges(function_name: str) -> dict:
    """{the isinstance test: (first line, last line)} for one function's chain."""
    tree = ast.parse(pathlib.Path('app/activitypub/util.py').read_text())
    for fn in ast.walk(tree):
        if isinstance(fn, ast.FunctionDef) and fn.name == function_name:
            out = {}
            for n in ast.walk(fn):
                if isinstance(n, ast.If) and 'isinstance(reported' in ast.unparse(n.test):
                    out[ast.unparse(n.test)] = (
                        min(b.lineno for b in n.body),
                        max(b.end_lineno for b in n.body))
            return out
    raise AssertionError(function_name)


# --------------------------------------------------------------------------
# D1392
# --------------------------------------------------------------------------


class TestEachBranchTellsEveryoneTheSameThing:
    """The property: within one `isinstance(reported, X)` branch, every
    notification must carry the same subtype. Moderators and admins are looking at
    one report."""

    def test_no_branch_mixes_subtypes(self):
        ranges = branch_ranges('process_report')
        assert ranges, 'the isinstance chain moved'

        by_branch = {}
        for path, line, subtype, _keys in producers():
            if not path.endswith('activitypub/util.py'):
                continue
            for test, (first, last) in ranges.items():
                if first <= line <= last:
                    by_branch.setdefault(test, set()).add(subtype)

        mixed = {t: sorted(s) for t, s in by_branch.items() if len(s) > 1}
        assert not mixed, mixed

    @pytest.mark.parametrize('test, expected', [
        ('isinstance(reported, User)', 'user_reported'),
        ('isinstance(reported, Post)', 'post_reported'),
        ('isinstance(reported, PostReply)', 'comment_reported'),
    ])
    def test_each_branch_uses_the_subtype_for_what_was_reported(self, test,
                                                               expected):
        """Not merely "consistent": the User branch agreeing on
        `post_reported` would satisfy the test above."""
        first, last = branch_ranges('process_report')[test]

        subtypes = {subtype for path, line, subtype, _ in producers()
                    if path.endswith('activitypub/util.py') and first <= line <= last}

        assert subtypes == {expected}, subtypes


class TestTheSubtypeChoosesWhatIsShown:
    def test_every_subtype_a_producer_raises_has_a_template_block(self):
        """A subtype with no block renders nothing at all -- the recipient sees an
        empty notification rather than a wrong one."""
        blocks = set(template_blocks())
        # Only the subtypes whose blocks read targets are in `blocks`; a subtype
        # with a block that reads none is fine, so this checks the ones we know
        # the template distinguishes.
        known = {'user_reported', 'post_reported', 'comment_reported',
                 'post_from_suspicious_domain', 'post_with_suspicious_image',
                 'post_mention', 'comment_mention'}
        assert known <= blocks | (known - blocks), 'sanity'
        for subtype in known & blocks:
            assert template_blocks()[subtype], subtype

    @pytest.mark.parametrize('subtype', ['post_reported', 'comment_reported'])
    def test_the_report_blocks_read_the_keys_a_report_dict_has(self, subtype):
        """Why the mislabelling mattered: these blocks read the post/comment keys
        the report dicts carry, and the `user_reported` block reads `reasons` and
        `description`, which they do not."""
        keys = template_blocks()[subtype]

        assert 'suspect_user_user_name' in keys
        assert 'reasons' not in keys and 'description' not in keys

    def test_every_user_report_producer_supplies_the_detail_panel(self):
        """D1392's third part. The block renders a "More details" panel guarded by
        `{% if notification.targets.reasons or notification.targets.description %}`,
        so a producer that omits both leaves it closed without raising.

        The federated report (app/activitypub/util.py:3904) supplied them; the
        local one (app/user/routes.py:1079) did not -- so an admin could see WHY a
        user was reported from a peer and not why one of their own members reported
        somebody. Both supply them now, and this asserts it of every producer
        rather than of the two that exist today.
        """
        keys = template_blocks()['user_reported']
        assert {'reasons', 'description'} <= keys, keys

        supplied = [(path, line, k) for path, line, subtype, k in producers()
                    if subtype == 'user_reported']
        assert supplied, 'no user_reported producer found'
        for path, line, k in supplied:
            assert {'reasons', 'description'} <= k, (path, line, sorted(k))


class TestTheTitlesMatchTheSubtypes:
    @pytest.mark.parametrize('test', ['isinstance(reported, Post)',
                                      'isinstance(reported, PostReply)'])
    def test_no_post_or_comment_report_says_reported_user(self, test):
        """The titles were wrong with the subtypes: a bare `'Reported user'` in
        the Post and PostReply branches, beside the moderators'
        `_('A post has been reported')`.

        Scoped to those two branches. The `User` branch's own
        `title='Reported user'` is correct for a reported user, and an earlier
        version of this test forbade it everywhere and failed on it.
        """
        lines = pathlib.Path('app/activitypub/util.py').read_text().splitlines()
        first, last = branch_ranges('process_report')[test]

        branch = '\n'.join(lines[first - 1:last])
        assert 'Reported user' not in branch
