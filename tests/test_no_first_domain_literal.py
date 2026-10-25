"""No tracked file names the first domain's host.

The repository must work for any operator's domain, so the first domain's host
is written nowhere in it. Writing it here would defeat the point, so this file
holds only the SHA-256 of the host. In every host-like token on every line of
every tracked file, every candidate that starts and ends at a `.`/`-` boundary
(or at the token's own start or end) and has the host's length is hashed, so a
sub-host (`www.<host>`), an e-mail address on the domain, or the host inside a
longer token (`x-<host>-y`) is caught too.

A second guard keeps the host's bare stem out as well: no token of letters may start with the stem (a
nickname such as `<stem>_user`, `<Stem>Bob`, `<stem>.foo`), unless the whole token is an English word or
third-party text on the allowlist. Only hashes are kept for the same reason.

A hit is reported as `path:line`. Tracked files are listed with `git ls-files`;
if git cannot run, the guard fails rather than skipping.
"""
import hashlib
import os
import re
import subprocess

FIRST_DOMAIN_SHA256 = '040a5614839e2e53449dd77f5c05b010dd08baf2e435663d3f6aa6fb657d1594'
# The host's length. Only a candidate of exactly this length can hash to it, so a candidate of any other
# length is not hashed, and a long run costs one check per label instead of one per substring.
FIRST_DOMAIN_LENGTH = 10

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LABEL = re.compile(r'[a-z0-9]+')
# Files are scanned as bytes: every byte that cannot be in a host name becomes a space, so `split()`
# yields the runs of host characters. That is the one C-speed pass over 12 MB that keeps the guard
# under its time budget; matching in Python text costs about three times as much. Upper case is
# folded first. Multi-byte characters and invalid UTF-8 end a run, as they do in a decoded text.
HOST_BYTES = b'abcdefghijklmnopqrstuvwxyz0123456789.-'
RUN_BREAKS = bytes(byte if byte in HOST_BYTES else 0x20 for byte in range(256))


def runs_of(raw):
    """The distinct runs of host characters in `raw` (bytes) that hold a dot."""
    return set(raw.lower().translate(RUN_BREAKS).split())


def tracked_paths(root):
    listing = subprocess.run(['git', 'ls-files', '-z'], cwd=root, check=True, capture_output=True).stdout
    return [path.decode('utf-8', errors='surrogateescape') for path in listing.split(b'\0') if path]


def candidates(run, length):
    """The substrings of `run`, `length` long, that could be the host name.

    A candidate starts at the run start or right after a '.' or '-', ends at the run end or right before
    a '.' or '-', and holds a dot. So the host is found wherever it sits in a run, however long the run
    (`--host`, `x-host`, `host-foo`, a rule of dashes with the host in it). A name that merely ends in
    the host, with no boundary before it, is not one.
    """
    for label in LABEL.finditer(run):
        start = label.start()
        end = start + length
        if end <= len(run) and (end == len(run) or run[end] in '.-') and '.' in run[start:end]:
            yield run[start:end]


def run_names_host(run, target_hash, length):
    """True when a candidate in `run` hashes to target_hash."""
    return any(hashlib.sha256(candidate.encode()).hexdigest() == target_hash for candidate in candidates(run, length))


class Verdicts:
    """Which runs name the host. A run is hashed once, however many files repeat it."""

    def __init__(self, target_hash, length):
        self.target_hash = target_hash
        self.length = length
        self.seen = set()
        self.named = set()

    def naming(self, runs):
        """The members of `runs` that name the host."""
        for run in runs.difference(self.seen):
            if b'.' in run and run_names_host(run.decode('ascii'), self.target_hash, self.length):
                self.named.add(run)
        self.seen |= runs
        return runs & self.named


def hit_lines(raw, verdicts):
    """The 1-based numbers of the lines of `raw` (bytes) that name the host."""
    named = verdicts.naming(runs_of(raw))
    if not named:
        return []
    return [number for number, line in enumerate(raw.split(b'\n'), start=1)
            if not named.isdisjoint(runs_of(line))]


def scan(root, paths, target_hash, length):
    """`path:line` for every line of every tracked file in `paths` that names the host.

    A symlink is scanned by its link text. A path that is neither a symlink nor present is reported,
    never skipped; a directory (a submodule) holds no tracked bytes of its own and is passed over.
    """
    verdicts = Verdicts(target_hash, length)
    hits = []
    for path in paths:
        full = os.path.join(root, path)
        if os.path.islink(full):
            raw = os.fsencode(os.readlink(full))
        elif not os.path.exists(full):
            hits.append(f'{path}: tracked but missing')
            continue
        elif not os.path.isfile(full):
            continue
        else:
            with open(full, 'rb') as handle:
                raw = handle.read()
        hits.extend(f'{path}:{number}' for number in hit_lines(raw, verdicts))
    return hits


def test_no_tracked_file_names_the_first_domain():
    hits = scan(REPO_ROOT, tracked_paths(REPO_ROOT), FIRST_DOMAIN_SHA256, FIRST_DOMAIN_LENGTH)
    assert not hits, 'the first domain is named at:\n' + '\n'.join(hits)


def test_the_matcher_finds_a_host_its_sub_hosts_and_mixed_case(tmp_path):
    fixture_hash = hashlib.sha256(b'example.org').hexdigest()
    (tmp_path / 'clean.txt').write_text('nothing here\nexample.com and notexample.org\n')
    (tmp_path / 'dirty.txt').write_text('ok\nvisit SUB.Example.ORG/x today\nmail me@example.org.\n')
    (tmp_path / 'binary.bin').write_bytes(b'\xff\xfe example.org \x00')
    (tmp_path / 'folder').mkdir()
    (tmp_path / 'dirlink').symlink_to(tmp_path / 'folder')
    (tmp_path / 'textlink').symlink_to('sub.example.org')
    (tmp_path / 'cleanlink').symlink_to('clean.txt')
    paths = ['clean.txt', 'dirty.txt', 'binary.bin', 'folder', 'dirlink', 'textlink', 'cleanlink']
    assert scan(str(tmp_path), paths, fixture_hash, 11) == ['dirty.txt:2', 'dirty.txt:3', 'binary.bin:1', 'textlink:1']


def test_a_tracked_path_missing_from_the_working_tree_is_reported(tmp_path):
    fixture_hash = hashlib.sha256(b'example.org').hexdigest()
    (tmp_path / 'dangling').symlink_to('nowhere')
    assert scan(str(tmp_path), ['gone.txt', 'dangling'], fixture_hash, 11) == ['gone.txt: tracked but missing']


def test_the_host_is_found_wherever_it_sits_in_a_run():
    fixture_hash = hashlib.sha256(b'example.org').hexdigest()
    named = [b'--sub.example.org', b'x-example.org', b'example.org-foo', b'-example.org-',
             b'-.' * 200 + b'example.org', b'example.org' + b'.-' * 200, b'a.' * 300 + b'example.org.' + b'b.' * 300]
    for line in named:
        assert hit_lines(line, Verdicts(fixture_hash, 11)) == [1], line[:30]
    for line in [b'notexample.org', b'example.com', b'x' * 400 + b'example.org', b'-.' * 200]:
        assert hit_lines(line, Verdicts(fixture_hash, 11)) == [], line[:30]


def test_a_token_is_hashed_once():
    verdicts = Verdicts(hashlib.sha256(b'example.org').hexdigest(), 11)
    assert hit_lines(b'a.example.org\na.example.org\n', verdicts) == [1, 2]
    assert verdicts.seen == {b'a.example.org'}
    assert hit_lines(b'a.example.org\n', verdicts) == [1]
    assert verdicts.seen == {b'a.example.org'}


def test_tracked_paths_lists_the_repository_files():
    assert 'run_tests.sh' in tracked_paths(REPO_ROOT)


# The first domain's bare stem: the first STEM_LENGTH letters of a token are hashed, so a token that starts with
# the stem is found without the stem being written here. A token is a run of letters; digits, underscores, dots
# and hyphens end one, and lower-casing makes `CamelCase` one token that still starts with the stem.
FIRST_DOMAIN_STEM_SHA256 = '0ebdc3317b75839f643387d783535adc360ca01f33c75f7c1e7373adcd675c0b'
STEM_LENGTH = 4
# SHA-256 of each whole token that starts with the stem and is allowed: English words and third-party text.
ALLOWED_STEM_TOKENS = frozenset({
    '2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824',
    '6050ba508a2481468cac1e4398ceccc97551e7541d1f239c75f0cd6b35886939',
    '6dba792f27373611222d4463e0ec5638330caeb4361453fa62992f670acb6a69',
})
LETTER_BYTES = b'abcdefghijklmnopqrstuvwxyz'
TOKEN_BREAKS = bytes(byte if byte in LETTER_BYTES else 0x20 for byte in range(256))


def tokens_of(raw):
    """The distinct runs of letters in `raw` (bytes), lower-cased."""
    return set(raw.lower().translate(TOKEN_BREAKS).split())


class StemVerdicts:
    """Which tokens start with the stem and are not allowed. A token is hashed once."""

    def __init__(self, stem_hash, allowed, length=STEM_LENGTH):
        self.stem_hash = stem_hash
        self.allowed = allowed
        self.length = length
        self.seen = set()
        self.named = set()

    def naming(self, tokens):
        """The members of `tokens` that start with the stem and are not allowed."""
        for token in tokens.difference(self.seen):
            if (hashlib.sha256(token[:self.length]).hexdigest() == self.stem_hash
                    and hashlib.sha256(token).hexdigest() not in self.allowed):
                self.named.add(token)
        self.seen |= tokens
        return tokens & self.named


def stem_hit_lines(raw, verdicts):
    """The 1-based numbers of the lines of `raw` (bytes) with a token that starts with the stem."""
    named = verdicts.naming(tokens_of(raw))
    if not named:
        return []
    return [number for number, line in enumerate(raw.split(b'\n'), start=1)
            if not named.isdisjoint(tokens_of(line))]


def stem_scan(root, paths, stem_hash, allowed, length=STEM_LENGTH):
    """`path:line` for every line of every regular tracked file in `paths` with a token that starts with the stem.

    A symlink is scanned by its link text; a missing path is reported by the first guard, so it is passed over.
    """
    verdicts = StemVerdicts(stem_hash, allowed, length)
    hits = []
    for path in paths:
        full = os.path.join(root, path)
        if os.path.islink(full):
            raw = os.fsencode(os.readlink(full))
        elif os.path.isfile(full):
            with open(full, 'rb') as handle:
                raw = handle.read()
        else:
            continue
        hits.extend(f'{path}:{number}' for number in stem_hit_lines(raw, verdicts))
    return hits


def test_no_tracked_file_has_a_token_that_starts_with_the_stem():
    hits = stem_scan(REPO_ROOT, tracked_paths(REPO_ROOT), FIRST_DOMAIN_STEM_SHA256, ALLOWED_STEM_TOKENS)
    assert not hits, 'the first domain is nicknamed at:\n' + '\n'.join(hits)


def test_the_stem_matcher_splits_tokens_folds_case_and_honours_the_allowlist(tmp_path):
    fixture_hash = hashlib.sha256(b'exam').hexdigest()
    allowed = frozenset({hashlib.sha256(b'example').hexdigest()})
    (tmp_path / 'clean.txt').write_text('an example, a sexam and a subexam\nplain\n')
    (tmp_path / 'dirty.txt').write_text('ok\nexam_user and exam_bob\nExamBob\nx.EXAM.y\nexam1\nexample\n-exam-\n')
    (tmp_path / 'binary.bin').write_bytes(b'\xff\xfe exams \x00')
    (tmp_path / 'textlink').symlink_to('exam.foo')
    (tmp_path / 'folder').mkdir()
    paths = ['clean.txt', 'dirty.txt', 'binary.bin', 'textlink', 'folder', 'gone.txt']
    assert stem_scan(str(tmp_path), paths, fixture_hash, allowed) == [
        'dirty.txt:2', 'dirty.txt:3', 'dirty.txt:4', 'dirty.txt:5', 'dirty.txt:7', 'binary.bin:1', 'textlink:1']


def test_a_stem_token_is_hashed_once():
    verdicts = StemVerdicts(hashlib.sha256(b'exam').hexdigest(), frozenset())
    assert stem_hit_lines(b'exam_a\nexam_a\n', verdicts) == [1, 2]
    assert verdicts.seen == {b'exam', b'a'}
    assert stem_hit_lines(b'a\nexam\n', verdicts) == [2]
