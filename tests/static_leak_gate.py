"""Which files a test left behind in the served static tree.

`app/static/media`, `app/static/tmp` and `app/static/posts` are real
directories in the checkout, and app code writes into them by relative path
(`'app/static/media/posts/' + new_filename[0:2] ...`) with no setting to point
it elsewhere. A test that provokes an upload, an image resize or an export and
does not remove the result leaves it there for good: before this gate the tree
held about 19,500 such files, and one full run added about 80 more.

Two halves, used by tests/conftest.py:

`StaticWriteTracker` attributes files to the test that CREATED them, in the
process that created them. It listens to CPython's audit events (`open` with
a write mode or O_CREAT/O_WRONLY/O_RDWR, `os.rename` -- which `os.replace`
and `shutil.move` also raise -- `os.link`, `os.symlink`), so it never walks
the tree: its cost does not grow with the 20,000 files a neglected checkout
holds, and it cannot blame a test for a file another xdist worker wrote at
the same moment, which a before/after walk would. A path is recorded only
when it did not exist at the moment it was opened, so rewriting a file that
was already there is not a leak.

`tree_snapshot` is the before/after walk, for the one thing the audit hook
cannot see: a write that does not go through Python (a C library writing by
itself, a subprocess). conftest runs it once at session start and once at
session end, on the controller, after every worker has finished.

Nothing here touches pytest, so tests/test_static_leak_gate.py exercises it
against a scratch directory.
"""
import os
import sys

# A write mode, in the `mode` argument `open` raises. `os.open` raises None
# there and the O_* flags in the third argument instead.
_WRITE_MODE_CHARACTERS = frozenset('wax+')
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT

# The events that can bring a new file into existence, and which argument
# names it. Everything else returns at the first comparison.
_DESTINATION_ARGUMENT = {'open': 0, 'os.rename': 1, 'os.link': 1, 'os.symlink': 1}


def _as_text(path):
    """A path argument as a str, or None for a file descriptor."""
    if isinstance(path, int):
        return None
    try:
        return os.fsdecode(os.fspath(path))
    except TypeError:
        return None


class StaticWriteTracker:
    """Records the files created under `roots` while it is started."""

    def __init__(self, roots):
        # abspath, not realpath: realpath costs a syscall per component on
        # every candidate event, and app code reaches these trees by the same
        # relative spelling the roots are built from.
        self.roots = tuple(os.path.join(os.path.abspath(root), '') for root in roots)
        self.created = set()
        self.recording = False

    def start(self):
        self.created = set()
        self.recording = True

    def stop(self):
        """Stop recording, returning what was created and still exists."""
        self.recording = False
        return sorted(path for path in self.created if os.path.lexists(path))

    def under_roots(self, path):
        return path.startswith(self.roots)

    def audit(self, event, args):
        """The audit hook. Has to be cheap: it sees every audited event."""
        if not self.recording:
            return
        index = _DESTINATION_ARGUMENT.get(event)
        if index is None:
            return
        if event == 'open':
            _path, mode, flags = args
            writes = ((isinstance(mode, str) and not _WRITE_MODE_CHARACTERS.isdisjoint(mode))
                      or (isinstance(flags, int) and flags & _WRITE_FLAGS))
            if not writes:
                return
        path = _as_text(args[index])
        if path is None:
            return
        path = os.path.abspath(path)
        if self.under_roots(path) and not os.path.lexists(path):
            self.created.add(path)


def install(tracker):
    """Register `tracker` as an audit hook. Audit hooks cannot be removed, so
    the tracker's `recording` flag, not this, is what turns it on and off."""
    sys.addaudithook(tracker.audit)


def remove_files(paths):
    """Delete each path, ignoring one that is already gone."""
    for path in paths:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def tree_snapshot(roots):
    """(files, directories) under `roots`, as absolute paths."""
    files, directories = set(), set()
    for root in roots:
        for directory, _subdirectories, filenames in os.walk(os.path.abspath(root)):
            directories.add(directory)
            files.update(os.path.join(directory, name) for name in filenames)
    return files, directories


def prune_new_empty_directories(before, after):
    """Remove directories in `after` but not `before` that are empty, deepest
    first, so a chain of new directories goes in one pass. Returns them."""
    removed = []
    for directory in sorted(after - before, key=len, reverse=True):
        try:
            os.rmdir(directory)
        except OSError:  # not empty, or already gone
            continue
        removed.append(directory)
    return removed
