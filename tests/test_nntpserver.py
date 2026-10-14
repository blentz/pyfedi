"""The NNTP connection handler (app/nntp/nntpserver.py) refuses or handles every malformed client line cleanly.

Each test drives the production PieFedNNTPConnectionHandler over the fuzz harness's fake socket and in-memory
stub server (tests/fuzz/targets/nntp.py, session_replies), so a handler that raises, keeps reading a closed
connection, looks up articles past its group or buffers without bound fails here exactly as it fails the
nntp_handler fuzz target. Each test pins one finding of that target; its reproducer is in
tests/fuzz/corpus/nntp_handler/ or tests/fuzz/corpus/nntp_article_framing/.

The stub serves three groups: a.example (articles 1-3), b.example (empty) and c.example (one article, hidden once
the user logs in as alice, as PieFedNNTPServer hides a community the user is banned from).
"""

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
