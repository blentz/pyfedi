"""The pure mapping between FediTest's view of a PieFed account or note and PieFed's own (interop/conformance/piefed_mapping.py)."""
import html

import pytest

from interop.conformance.piefed_mapping import acct_to_userid, libpq_dsn, microblog_title, note_text, received_ap_id


def _rendered(content, title):
    """What PieFed federates for a microblog post titled `title` whose body is `content`: app/activitypub/serializers/microblog.py
    note_content leaves the title out only when the body already starts with it as whole words."""
    body = ''.join(f'<p>{html.escape(p)}</p>' for p in content.split('\n\n'))
    if content.startswith(title) and (len(content) == len(title) or not content[len(title)].isalnum()):
        return body
    return f'<p>{html.escape(title)}</p>{body}'


def test_acct_to_userid():
    assert acct_to_userid('acct:alice@piefed.test') == 'alice'


def test_acct_other_host_rejected():
    with pytest.raises(ValueError):
        acct_to_userid('acct:alice@mastodon.test')


def test_acct_without_scheme_rejected():
    with pytest.raises(ValueError):
        acct_to_userid('alice@piefed.test')


def test_acct_host_is_the_nodes_own():
    assert acct_to_userid('acct:bob@other.test', 'other.test') == 'bob'


@pytest.mark.parametrize('content', [
    'good morning',
    'a longer note that is certainly more than fifty characters long, so the title is cut',   # the cut falls inside a word
    'two paragraphs, the first one past the fifty-character cut\n\nthe second',
    'quotes "and" <angle> & ampersands',
])
def test_note_text_roundtrip(content):
    title = microblog_title(content)
    assert note_text(_rendered(content, title), title) == content


def test_note_text_roundtrip_with_a_link():
    content = 'see https://piefed.test/post/1 for more'
    rendered = '<p>see <a href="https://piefed.test/post/1" rel="nofollow ugc" target="_blank">https://piefed.test/post/1</a> for more</p>'
    assert note_text(rendered, microblog_title(content)) == content


def test_note_text_title_that_is_a_prefix_of_the_content():
    content = 'good morning and then some'
    assert note_text('<p>good morning</p><p>good morning and then some</p>', 'good morning') == content


def test_note_text_keeps_a_first_paragraph_equal_to_the_title_when_it_is_all_there_is():
    assert note_text('<p>morning</p>', 'morning') == 'morning'


def test_note_text_without_a_title_strips_nothing():
    assert note_text('<p>one</p><p>two</p>', '') == 'one\n\ntwo'


def test_microblog_title_is_bounded():
    assert len(microblog_title('x' * 500)) == 50
    assert microblog_title('short') == 'short'
    assert microblog_title('  padded  ') == 'padded'


def test_unknown_object_is_none_not_error():
    assert received_ap_id([]) is None


def test_received_ap_id_is_the_first_rows_first_column():
    assert received_ap_id([('https://piefed.test/post/1', '<p>x</p>')]) == 'https://piefed.test/post/1'


def test_libpq_dsn_drops_the_sqlalchemy_driver():
    assert libpq_dsn('postgresql+psycopg2://piefed:pw@piefed-db/piefed') == 'postgresql://piefed:pw@piefed-db/piefed'
    assert libpq_dsn('postgresql://piefed:pw@piefed-db/piefed') == 'postgresql://piefed:pw@piefed-db/piefed'


def test_microblog_title_ends_on_a_whole_word_so_piefed_federates_the_body_alone():
    content = 'Testing sender_creates_note 2026-10-28 03:42:37.047348 @bob@mastodon.test'
    title = microblog_title(content)
    assert title == 'Testing sender_creates_note 2026-10-28 03:42:37'
    assert _rendered(content, title) == f'<p>{content}</p>'


def test_microblog_title_cuts_a_single_long_word_at_the_limit():
    assert microblog_title('x' * 500) == 'x' * 50
