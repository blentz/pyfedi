"""PostReply.new() stores the object-level visibility and nothing reads or writes
the old followers-only `private` marker (the to[0] rule was dead for
followers-only content, which create_post_reply refuses before reaching it).
The column stays; only the reads and the write are retired.

That PostReply.new stores `visibility` from the object's addressing is pinned in
tests/test_models_post_reply_new.py (test_a_followers_only_reply_is_stored_as_followers).
"""
import inspect

from app.models import PostReply


def test_new_no_longer_writes_private():
    source = inspect.getsource(PostReply.new)
    assert 'private=private' not in source
    assert "endswith('/followers')" not in source
