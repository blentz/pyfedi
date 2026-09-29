"""The `post_from_suspicious_domain` notification: one dict, four writers, one
template.

`app/templates/user/notifs/20.html`'s block for this subtype reads exactly three
keys:

    :92   targets.orig_post_title
    :106  targets.orig_post_body   (through markdown_to_html)
    :110  targets.suspect_user_user_name   (twice: the href and the text)

and four places build the dict that has to supply them:

    app/models.py           Post.new            -- the federated create
    app/activitypub/util.py update_post_from_activity -- the federated edit
    app/shared/post.py      make_post / edit_post -- the local paths

D1391 is what happens when those two lists are never compared.

**`suspect_user_user_name` was written by nobody.** Every writer set
`orig_post_title` and `orig_post_body`, and `app/shared/post.py` set
`author_user_name` -- the key OTHER subtypes' blocks read. So the Author line
rendered `/u/` with no text, on every path, for every recipient. Jinja renders a
missing key as empty rather than raising, which is why it was never noticed.

**`Post.new`'s admin branch reassigned the dict** to `{'gen', 'post_id'}`, so an
admin's notification for a federated post lost the title and the body that a
moderator's for the *same post* carried -- on the path where the post came from a
peer and the context matters most. The other two writers give admins the
moderators' dict.

**`orig_post_domain` was `post.domain`, the relationship, read before assignment.**
None for a new post; for a post that already had a domain, a `Domain` object in a
`db.JSON` column: `StatementError (builtins.TypeError) Object of type Domain is
not JSON serializable`, measured. That half was a **registered defect**, pinned
deliberately in `tests/test_ap_update_post_tails.py` with a banner saying the
arbitration needed a slice owning all three files at once, and naming the three
reasons sub-project 18 used when it fixed the fourth site. D1391 owned all three
and finished it on the same reasons; those pins now assert the repair, as their own
docstring asked.

WHAT THIS FILE ADDS. The pins above each cover one writer. This one asserts the
property that made the defect possible: **every writer produces the same keys, and
that set covers what the template reads.** A fifth writer, or a template that
starts reading a fourth key, fails here rather than rendering blank.
"""
import ast
import pathlib
import re

import pytest

TEMPLATE = pathlib.Path('app/templates/user/notifs/20.html')
SUBTYPE = 'post_from_suspicious_domain'

# The writers, as (file, the name of the dict variable they build).
WRITERS = [
    'app/models.py',
    'app/activitypub/util.py',
    'app/shared/post.py',
]


def keys_written_near_subtype(path: str) -> list[set]:
    """For each `subtype='post_from_suspicious_domain'` notification in a file,
    the keys of the nearest `targets_data = {...}` literal above it.

    Read from the source rather than by running each writer: two of them need a
    federated activity and a configured Domain to reach, and the property under
    test is that the source agrees with the template, not that any one run does.

    Scoped by proximity to the subtype, NOT by "a dict that has orig_post_title".
    The first version of this helper used the latter and picked up
    app/activitypub/util.py:2124, which builds the dict for
    `post_with_suspicious_image` -- a different notification with its own keys,
    legitimately different. Comparing it against this subtype's writers reported
    four failures that were the test's fault.
    """
    source = pathlib.Path(path).read_text()
    tree = ast.parse(source)

    dicts = []           # (lineno, keys) for every targets_data literal
    for node in ast.walk(tree):
        if (isinstance(node, ast.Assign) and len(node.targets) == 1
                and isinstance(node.targets[0], ast.Name)
                and node.targets[0].id == 'targets_data'
                and isinstance(node.value, ast.Dict)):
            dicts.append((node.lineno,
                          {k.value for k in node.value.keys
                           if isinstance(k, ast.Constant)
                           and isinstance(k.value, str)}))

    found = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == 'Notification'):
            continue
        if not any(kw.arg == 'subtype' and isinstance(kw.value, ast.Constant)
                   and kw.value.value == SUBTYPE for kw in node.keywords):
            continue
        above = [(ln, keys) for ln, keys in dicts if ln < node.lineno]
        assert above, f'{path}:{node.lineno} has no targets_data above it'
        found.append(max(above)[1])
    return found


def keys_the_template_reads() -> set:
    """`targets.<key>` inside this subtype's `{% elif %}` block."""
    text = TEMPLATE.read_text()
    start = text.index(f'"{SUBTYPE}"')
    end = text.index('{% elif notification.subtype', start + 1)
    return set(re.findall(r'targets\.(\w+)', text[start:end]))


class TestTheWritersAgreeWithEachOther:
    def test_every_writer_produces_the_same_keys(self):
        """D1391's first half: `Post.new`'s admin branch reassigned the dict to
        two keys, so one recipient of one event got a different shape from
        another."""
        shapes = {}
        for path in WRITERS:
            for keys in keys_written_near_subtype(path):
                shapes.setdefault(frozenset(keys), []).append(path)

        assert len(shapes) == 1, {sorted(k): v for k, v in shapes.items()}

    def test_there_is_at_least_one_writer_per_file(self):
        """Guards the sweep itself: if a refactor renames `targets_data`, the test
        above would pass on an empty set of shapes and prove nothing."""
        for path in WRITERS:
            assert keys_written_near_subtype(path), path


class TestTheWritersCoverTheTemplate:
    def test_the_template_reads_only_keys_that_are_written(self):
        """D1391's second half: the template reads `suspect_user_user_name` and no
        writer set it, so the Author line was blank everywhere. Jinja renders a
        missing key as empty, so nothing failed -- which is why this has to be
        asserted rather than observed."""
        written = set().union(*[keys
                                for path in WRITERS
                                for keys in keys_written_near_subtype(path)])

        missing = keys_the_template_reads() - written

        assert not missing, missing

    def test_the_template_really_reads_three_keys(self):
        """The other direction of the same guard: if this drops to zero because
        the block moved or was renamed, the test above passes vacuously."""
        assert keys_the_template_reads() == {
            'orig_post_title', 'orig_post_body', 'suspect_user_user_name'}


class TestTheSiblingSubtype:
    """`post_with_suspicious_image` has one producer
    (app/activitypub/util.py:2122) and its own block in the same template
    (:115-140), which reads the same three keys. It had the same missing
    `suspect_user_user_name`, found by the sweep above catching its dict -- so the
    same property is asserted for it rather than left as the next round's
    surprise."""

    SUBTYPE = 'post_with_suspicious_image'

    def _keys(self):
        import ast as _ast

        source = pathlib.Path('app/activitypub/util.py').read_text()
        tree = _ast.parse(source)
        dicts = [(n.lineno, {k.value for k in n.value.keys
                             if isinstance(k, _ast.Constant)})
                 for n in _ast.walk(tree)
                 if isinstance(n, _ast.Assign) and len(n.targets) == 1
                 and isinstance(n.targets[0], _ast.Name)
                 and n.targets[0].id == 'targets_data'
                 and isinstance(n.value, _ast.Dict)]
        for n in _ast.walk(tree):
            if (isinstance(n, _ast.Call) and isinstance(n.func, _ast.Name)
                    and n.func.id == 'Notification'
                    and any(kw.arg == 'subtype'
                            and isinstance(kw.value, _ast.Constant)
                            and kw.value.value == self.SUBTYPE
                            for kw in n.keywords)):
                above = [(ln, keys) for ln, keys in dicts if ln < n.lineno]
                assert above
                return max(above)[1]
        raise AssertionError(f'no producer found for {self.SUBTYPE}')

    def test_the_template_reads_only_keys_that_are_written(self):
        text = TEMPLATE.read_text()
        start = text.index(f'"{self.SUBTYPE}"')
        nxt = text.find('{% elif notification.subtype', start + 1)
        block = text[start:nxt if nxt > 0 else len(text)]
        read = set(re.findall(r'targets\.(\w+)', block))

        assert read, 'the block moved or was renamed'
        assert not read - self._keys(), read - self._keys()


class TestTheKeysThemselves:
    @pytest.mark.parametrize('key', ['gen', 'post_id', 'orig_post_title',
                                     'orig_post_body', 'orig_post_domain',
                                     'suspect_user_user_name'])
    def test_each_expected_key_is_present(self, key):
        """Named individually so a failure says which key went missing, rather
        than printing two sets to diff."""
        for path in WRITERS:
            for keys in keys_written_near_subtype(path):
                assert key in keys, (path, sorted(keys))

    def test_no_writer_still_uses_the_old_author_key(self):
        """`author_user_name` was the wrong name in THIS dict -- it is the right
        one in the dicts for other subtypes, whose blocks do read it, so the check
        is scoped to the dicts that carry `orig_post_title`."""
        for path in WRITERS:
            for keys in keys_written_near_subtype(path):
                assert 'author_user_name' not in keys, path

    def test_no_writer_stores_the_domain_relationship(self):
        """`orig_post_domain` must be a name, not `post.domain`. Asserted against
        the source because the object only fails at flush, and only for a post
        that already had a domain -- the condition the registered defect needed.
        """
        for path in WRITERS:
            source = pathlib.Path(path).read_text()
            assert "'orig_post_domain': post.domain," not in source, path
            assert "'orig_post_domain': post.domain}" not in source, path
