"""The NNTP connection handler (app/nntp/nntpserver.py) refuses or handles every malformed client line cleanly.

Each test drives the production PieFedNNTPConnectionHandler over the fuzz harness's fake socket and in-memory
stub server (tests/fuzz/targets/nntp.py, session_replies), so a handler that raises, keeps reading a closed
connection, looks up articles past its group or buffers without bound fails here exactly as it fails the
nntp_handler fuzz target. Each test pins one finding of that target; its reproducer is in
tests/fuzz/corpus/nntp_handler/ or tests/fuzz/corpus/nntp_article_framing/.

The stub serves three groups: a.example (articles 1-3), b.example (empty) and c.example (one article, hidden once
the user logs in as alice, as PieFedNNTPServer hides a community the user is banned from).
"""

import pytest

from app.nntp.nntpserver import numbers_in_group, parse_datetime
from tests.fuzz.targets.nntp import session_replies

READY = '201 NNTP Service Ready, posting prohibited'
CLOSING = '205 Connection closing'


def test_blank_lines_are_not_read_ahead_of():
    """A blank line read another 2049 bytes into the buffer while lines were still waiting in it, so a stream of
    blank lines grew the buffer without bound."""
    assert session_replies(b'\n' * 6000 + b'QUIT\r\n') == [READY, CLOSING]


def test_a_line_past_the_line_limit_closes_the_connection():
    """A line with no LF in sight was buffered for as long as the client kept sending: _MAXLINE bounded each read,
    not the line."""
    assert session_replies(b'X' * 5000 + b'\r\nQUIT\r\n') == [READY, CLOSING]


ONE_BYTE_READS, SEVEN_BYTE_READS = b'\x00\x00', b'\x00\x02'   # the stub's config: recv hands over 1 or 7 bytes


def test_a_read_of_only_whitespace_inside_a_line_is_part_of_the_line():
    """_getline stopped reading at a read of nothing but whitespace and refused the unfinished line as too long."""
    assert session_replies(b'MODE READER\r\nGROUP' + b' ' * 14 + b'a.example\r\nQUIT\r\n', config=SEVEN_BYTE_READS) == [
        READY, READY, '211 3 1 3 a.example', CLOSING]
    assert session_replies(b'GROUP a.example\r\nQUIT\r\n', config=ONE_BYTE_READS) == [
        READY, '211 3 1 3 a.example', CLOSING]


def test_a_read_that_ends_inside_a_line_after_a_complete_one_keeps_the_rest():
    """Once the complete line was taken, the start of the next one left in the buffer was refused as too long."""
    assert session_replies(b'GROUP a.example\r\nA\r\nQUIT\r\n', config=SEVEN_BYTE_READS) == [
        READY, '211 3 1 3 a.example', '500 Unknown command', CLOSING]


def test_a_line_of_the_line_limit_is_read_and_one_past_it_refused():
    assert session_replies(b'X' * 2048 + b'\nQUIT\r\n') == [READY, '500 Unknown command', CLOSING]
    assert session_replies(b'X' * 2048 + b'\r\nQUIT\r\n') == [READY, CLOSING]


def test_a_line_past_the_line_limit_is_refused_when_its_lf_arrives_with_it():
    """The limit held only while no LF was buffered: a long line whose LF came in the read that took it past the
    limit was read whole."""
    replies = session_replies(b'DATE\r\n' + b'X' * 2143 + b'\r\nQUIT\r\n')
    assert replies[0] == READY and replies[1].startswith('111 ') and replies[2:] == [CLOSING]


POSTING = b'\x02\x05'   # the stub's config: no login, posting allowed
POSTING_READY = '200 NNTP Service Ready, posting allowed'


def test_the_handler_returns_when_the_client_closes_without_quit():
    """recv gives b'' forever on a closed connection, and _getline turned each into an empty line, which the
    handler skips: a client that left without QUIT pinned its thread in a busy loop for good."""
    assert session_replies(b'GROUP a.example\r\n') == [READY, '211 3 1 3 a.example']


def test_the_handler_returns_when_the_client_closes_in_the_middle_of_an_article():
    assert session_replies(b'POST\r\nSubject: x\r\n', config=POSTING) == [
        POSTING_READY, '340 Input article; end with <CR-LF>.<CR-LF>']


def test_an_unfinished_line_at_close_gets_no_reply():
    assert session_replies(b'GROUP a.example\r\nQUI') == [READY, '211 3 1 3 a.example']


def test_a_command_that_is_not_utf8_closes_the_connection():
    """_getline decoded every line as UTF-8 unguarded, so one byte that is not killed the handler mid-session."""
    assert session_replies(b'GROUP \xff\r\nQUIT\r\n') == [READY, CLOSING]


def test_an_article_line_that_is_not_utf8_closes_the_connection():
    assert session_replies(b'POST\r\nSubject: \xfe\r\n.\r\nQUIT\r\n', config=POSTING) == [
        POSTING_READY, '340 Input article; end with <CR-LF>.<CR-LF>', CLOSING]


def test_group_without_a_name_is_a_syntax_error():
    """`_, group_name = self.data.split()` raised ValueError for any GROUP without exactly one argument."""
    assert session_replies(b'GROUP\r\nQUIT\r\n') == [READY, '501 Syntax Error', CLOSING]


def test_group_with_two_names_is_a_syntax_error():
    assert session_replies(b'GROUP a.example b.example\r\nQUIT\r\n') == [READY, '501 Syntax Error', CLOSING]


def test_listgroup_without_a_name_lists_the_selected_group():
    """LISTGROUP refused no argument only while no group was selected, then read tokens[0] anyway: IndexError."""
    assert session_replies(b'GROUP a.example\r\nLISTGROUP\r\nQUIT\r\n') == [
        READY, '211 3 1 3 a.example', '211 3 1 3 a.example', '1', '2', '3', '.', CLOSING]


@pytest.mark.parametrize('date_str,time_str', [
    ('99999999999999999999990101', '000000'),
    ('20250101', '0000999999999999999999999'),
])
def test_parse_datetime_refuses_a_field_past_what_datetime_holds(date_str, time_str):
    """datetime() raises OverflowError, not ValueError, for a field past a C long; the callers refuse ValueError."""
    with pytest.raises(ValueError):
        parse_datetime(date_str, time_str)


def test_newnews_with_a_malformed_date_is_a_syntax_error():
    """newnews() and newgroups() caught only TypeError around parse_datetime, which raises ValueError."""
    assert session_replies(b'NEWNEWS a.example 2025x 000000\r\nQUIT\r\n') == [READY, '501 Syntax Error', CLOSING]


def test_newgroups_with_a_malformed_date_is_a_syntax_error():
    assert session_replies(b'NEWGROUPS 99999999999999999999990101 000000\r\nQUIT\r\n') == [
        READY, '501 Syntax Error', CLOSING]


def test_newnews_without_a_time_is_a_syntax_error():
    """newnews() let two arguments through its length check, then unpacked three: ValueError."""
    assert session_replies(b'NEWNEWS a.example 20250101\r\nQUIT\r\n') == [READY, '501 Syntax Error', CLOSING]


LOGIN = b'AUTHINFO USER alice\r\nAUTHINFO PASS secret word\r\n'


@pytest.mark.parametrize('command', [b'OVER 1-', b'XHDR Subject 1-', b'LISTGROUP'])
def test_a_group_hidden_by_logging_in_is_no_longer_selected(command):
    """PieFedNNTPServer.groups leaves out a community the user is banned from once they log in; the handler then
    looked the selected group up by name and raised KeyError."""
    assert session_replies(b'GROUP c.example\r\n' + LOGIN + command + b'\r\nQUIT\r\n', config=b'\x01\x05') == [
        READY, '211 1 1 1 c.example', '381 Enter passphrase', '281 Authentication accepted',
        '412 No newsgroup selected', CLOSING]


@pytest.mark.parametrize('command', [b'OVER 1-1000000000000000', b'XHDR Subject 1-1000000000000000',
                                     b'LISTGROUP a.example 1-1000000000000000', b'OVER -1000000000000000-2'])
def test_a_range_past_the_group_looks_up_only_the_groups_articles(command):
    """The range commands looped over every number the client named, looking each one up: `OVER 1-1000000000000000`
    held a thread for good. The stub fails the session if one command looks up more than the group holds."""
    replies = session_replies(b'GROUP a.example\r\n' + command + b'\r\nQUIT\r\n')
    assert replies[0] == READY and replies[-2:] == ['.', CLOSING]


@pytest.mark.parametrize('first,last,low,high,numbers', [
    (1, 10 ** 15, 1, 3, [1, 2, 3]),
    (-(10 ** 15), 2, 1, 3, [1, 2]),
    (2, 2, 1, 3, [2]),
    (5, 9, 1, 3, []),
    (3, 1, 1, 3, []),
    (1, 5, 0, 0, []),
    (0, 5, 0, 0, [0]),
])
def test_numbers_in_group_keeps_only_what_the_group_can_hold(first, last, low, high, numbers):
    assert list(numbers_in_group(first, last, low, high)) == numbers


def test_help_sends_each_paragraph_as_lines_of_its_own():
    """help() wrapped the whole text at once with replace_whitespace=False, so the blank lines between its
    paragraphs went out inside wrapped lines as bare LFs."""
    replies = session_replies(b'HELP\r\nQUIT\r\n', config=b'\x05\x05')
    assert replies[1] == '100 Help text follows' and replies[-2:] == ['.', CLOSING]
    text = replies[2:-2]
    assert not any('\n' in line or '\r' in line for line in text)
    assert text[0].startswith('This is an NNTP version 2 server.') and '' in text
    assert text[-1].endswith('followed by your password.')


def test_a_title_holding_crlf_cannot_end_a_reply_or_forge_the_next():
    """A remote post's title can hold line breaks. send_lines sent each line as it was, so `Hi CRLF . CRLF 200`
    ended the HEAD reply early and made the client read a forged reply after it."""
    replies = session_replies(b'GROUP a.example\r\nHEAD 1\r\nQUIT\r\n', subject='Hi\r\n.\r\n200 forged')
    assert replies[2:5] == ['221 1 <post-1@stub.example>', 'From: <alice@stub.example>', 'Subject: Hi  .  200 forged']
    assert replies.count('.') == 1 and replies[-2:] == ['.', CLOSING]


def test_an_author_name_holding_lf_stays_on_its_header_line():
    replies = session_replies(b'GROUP a.example\r\nHEAD 1\r\nQUIT\r\n', from_='a\nb@x')
    assert replies[3] == 'From: <a b@x>'


@pytest.mark.parametrize('line,sent', [(' .', '. .'), ('\t.forged', '.\t.forged'), ('\r.', '. .')])
def test_a_body_line_of_whitespace_and_a_dot_cannot_end_the_article(line, sent):
    """article() dot-stuffed a body line starting with '.', then send_lines stripped the line: ' .' went out as the
    '.' that ends the reply, and the rest of the body as forged replies."""
    replies = session_replies(b'GROUP a.example\r\nBODY 1\r\nQUIT\r\n', body=f'one\n{line}\n200 forged')
    assert replies[2:] == ['222 1 <post-1@stub.example>', 'one', sent, '200 forged', '.', CLOSING]


def test_a_tab_in_a_title_stays_inside_its_overview_field():
    """OVER separates fields with TAB, and ArticleInfo.__str__ joined them as they were: a title holding a tab
    shifted every field after it, so the client read the author as the date and so on."""
    replies = session_replies(b'GROUP a.example\r\nOVER 1\r\nQUIT\r\n', subject='a\tb')
    fields = replies[3].split('\t')
    assert fields[:3] == ['1', 'a b', 'alice@stub.example'] and len(fields) == 10
