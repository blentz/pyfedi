"""No tracked file names the first domain's host.

The repository must work for any operator's domain, so the first domain's host
is written nowhere in it. Writing it here would defeat the point, so this file
holds only the SHA-256 of the host. Every host-like token on every line of every
tracked file is hashed, and so is every dot-suffix of it (`www.<host>` hashes
`www.<host>`, `<host>`, and the top-level label), so a sub-host or an e-mail
address on the domain is caught too.

A hit is reported as `path:line`. Tracked files are listed with `git ls-files`;
if git cannot run, the guard fails rather than skipping.
"""
import hashlib
import os
import re
import subprocess

FIRST_DOMAIN_SHA256 = '040a5614839e2e53449dd77f5c05b010dd08baf2e435663d3f6aa6fb657d1594'

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOST_LIKE = re.compile(r'[a-z0-9.-]+\.[a-z]{2,}')
# A DNS name is at most 253 characters. HOST_LIKE backtracks quadratically over a long run of host
# characters with no ".xx" in it (a base64 blob, an SVG path, a rule of dashes), so a run is tried only
# when a host could fit in it.
LONGEST_HOST = 253
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


class Verdicts:
    """Which runs name the host. A run is hashed once, however many files repeat it."""

    def __init__(self, target_hash):
        self.target_hash = target_hash
        self.seen = set()
        self.named = set()

    def naming(self, runs):
        """The members of `runs` that name the host."""
        for run in runs.difference(self.seen):
            if b'.' in run and run_names_host(run.decode('ascii'), self.target_hash):
                self.named.add(run)
        self.seen |= runs
        return runs & self.named


def run_names_host(run, target_hash):
    """True when a host-like token in `run`, or any dot-suffix of one, hashes to target_hash."""
    return len(run) <= LONGEST_HOST and any(
        hashlib.sha256('.'.join(labels[start:]).encode()).hexdigest() == target_hash
        for labels in (token.split('.') for token in HOST_LIKE.findall(run))
        for start in range(len(labels)))


def hit_lines(raw, verdicts):
    """The 1-based numbers of the lines of `raw` (bytes) that name the host."""
    named = verdicts.naming(runs_of(raw))
    if not named:
        return []
    return [number for number, line in enumerate(raw.split(b'\n'), start=1)
            if not named.isdisjoint(runs_of(line))]


def scan(root, paths, target_hash):
    """`path:line` for every line of every regular file in `paths` that names the host."""
    verdicts = Verdicts(target_hash)
    hits = []
    for path in paths:
        full = os.path.join(root, path)
        if os.path.islink(full) or not os.path.isfile(full):
            continue
        with open(full, 'rb') as handle:
            hits.extend(f'{path}:{number}' for number in hit_lines(handle.read(), verdicts))
    return hits


def test_no_tracked_file_names_the_first_domain():
    hits = scan(REPO_ROOT, tracked_paths(REPO_ROOT), FIRST_DOMAIN_SHA256)
    assert not hits, 'the first domain is named at:\n' + '\n'.join(hits)


def test_the_matcher_finds_a_host_its_sub_hosts_and_mixed_case(tmp_path):
    fixture_hash = hashlib.sha256(b'example.org').hexdigest()
    (tmp_path / 'clean.txt').write_text('nothing here\nexample.com and notexample.org\n')
    (tmp_path / 'dirty.txt').write_text('ok\nvisit SUB.Example.ORG/x today\nmail me@example.org.\n')
    (tmp_path / 'binary.bin').write_bytes(b'\xff\xfe example.org \x00')
    (tmp_path / 'folder').mkdir()
    (tmp_path / 'link').symlink_to(tmp_path / 'folder')
    paths = ['clean.txt', 'dirty.txt', 'binary.bin', 'folder', 'link', 'missing.txt']
    assert scan(str(tmp_path), paths, fixture_hash) == ['dirty.txt:2', 'dirty.txt:3', 'binary.bin:1']


def test_a_token_is_hashed_once():
    verdicts = Verdicts(hashlib.sha256(b'example.org').hexdigest())
    assert hit_lines(b'a.example.org\na.example.org\n', verdicts) == [1, 2]
    assert verdicts.seen == {b'a.example.org'}
    assert hit_lines(b'a.example.org\n', verdicts) == [1]
    assert verdicts.seen == {b'a.example.org'}


def test_a_run_too_long_to_be_a_host_is_skipped_but_its_neighbours_are_not():
    fixture_hash = hashlib.sha256(b'example.org').hexdigest()
    line = b'x' * 300 + b'.example.org example.org'
    assert run_names_host('x' * 300 + '.example.org', fixture_hash) is False
    assert hit_lines(line, Verdicts(fixture_hash)) == [1]


def test_tracked_paths_lists_the_repository_files():
    assert 'run_tests.sh' in tracked_paths(REPO_ROOT)
