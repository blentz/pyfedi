"""O1 (Loops spike 2026-10-30, spec 2026-10-30 §3.3): a local user's ready video goes to their followers on a Loops
server as the Note Loops stores, not PeerTube's Video, which Loops answers 202 and drops (its inbox stores only a
Note with a video/mp4 Document whose url is a string)."""
import pytest

from app import db
from app.activitypub.serializers import loops_video, uses_loops_video
from app.shared.media import with_media
from app.shared.tasks.pages import send_post
from tests.factories import make_follow, make_instance, make_user
from tests.peertube_video_fixtures import _example_hosts_resolve_without_a_resolver

PUBLIC = 'https://www.w3.org/ns/activitystreams#Public'
LOOPS_INBOX = 'https://loops.example/ap/inbox'


@pytest.fixture
def loops_follower(peertube_video_video_post):
    instance = make_instance('loops.example', software='loops')
    instance.inbox = LOOPS_INBOX
    lucy = make_user(instance, 'lucy')
    lucy.ap_profile_id = lucy.ap_public_url = 'https://loops.example/ap/users/1'
    make_follow(peertube_video_video_post.author, lucy, is_inward=True)
    db.session.commit()
    return lucy


def test_the_loops_note_carries_the_video_as_loops_reads_it(peertube_video_video_post):
    peertube_video_video_post.image.alt_text = 'a clip of a cat'
    peertube_video_video_post.body_html = '<p>About the video</p>'
    note = loops_video(peertube_video_video_post)
    author = peertube_video_video_post.author
    assert note['id'] == note['url'] == peertube_video_video_post.public_url()   # Loops names our post by `url` in Likes and replies
    assert note['type'] == 'Note' and note['attributedTo'] == author.public_url()
    assert note['to'] == [PUBLIC] and note['cc'] == [author.followers_url()]
    assert note['content'] == '<p>A video</p><p>About the video</p>'
    assert note['attachment'] == [{'type': 'Document', 'mediaType': 'video/mp4', 'url': peertube_video_video_post.url,
                                   'name': None, 'width': 640, 'height': 360}]
    assert note['preview'] == {'type': 'Image', 'name': 'a clip of a cat', 'width': 640, 'height': 360,
                               'url': {'href': peertube_video_video_post.image.source_url, 'mediaType': 'image/jpeg'}}
    assert note['sensitive'] is False and note['summary'] is None and note['inReplyTo'] is None


def test_a_body_that_starts_with_the_title_is_not_doubled(peertube_video_video_post):
    peertube_video_video_post.body_html = '<p>A video, and more about it</p>'
    assert loops_video(peertube_video_video_post)['content'] == '<p>A video, and more about it</p>'


def test_a_title_is_escaped_and_a_sensitive_video_is_marked(peertube_video_video_post):
    peertube_video_video_post.title, peertube_video_video_post.body_html, peertube_video_video_post.nsfw = 'Cats <3 & dogs', '', True
    note = loops_video(peertube_video_video_post)
    assert note['content'] == '<p>Cats &lt;3 &amp; dogs</p>' and note['sensitive'] is True


def test_the_loops_follower_gets_the_authors_create_of_the_note(peertube_video_sent, peertube_video_video_post, loops_follower):
    send_post(peertube_video_video_post.id, session=db.session)
    body, key = peertube_video_sent[LOOPS_INBOX]
    assert body['type'] == 'Create' and body['actor'] == peertube_video_video_post.author.public_url()
    assert body['object'] == loops_video(peertube_video_video_post)
    assert body['to'] == [PUBLIC] and body['cc'] == [peertube_video_video_post.author.followers_url()]   # Loops reads the activity's
    assert key == peertube_video_video_post.author.public_url() + '#main-key'


def test_an_edit_is_the_authors_update_of_the_note(peertube_video_sent, peertube_video_video_post, loops_follower):
    send_post(peertube_video_video_post.id, edit=True, session=db.session)
    body = peertube_video_sent[LOOPS_INBOX][0]
    assert body['type'] == 'Update' and body['object']['type'] == 'Note'


def test_the_other_followers_are_sent_what_they_were(peertube_video_sent, peertube_video_video_post, loops_follower):
    send_post(peertube_video_video_post.id, session=db.session)
    assert peertube_video_sent['https://peertube.example/inbox'][0]['object']['type'] == 'Video'
    assert peertube_video_sent['https://follower.example/inbox'][0]['object']['type'] == 'Video'
    assert len(peertube_video_sent.calls) == 6


@pytest.mark.parametrize('change', [
    {'mime': 'video/webm'},
    {'mime': 'video/quicktime'},
    {'bytes': 100 * 1024 * 1024 + 1},
], ids=['webm', 'mov', 'over-100-MB'])
def test_a_video_loops_would_refuse_is_not_sent_to_loops(peertube_video_sent, peertube_video_video_post, loops_follower, change):
    with_media(peertube_video_video_post, **change)
    db.session.commit()
    assert not uses_loops_video(peertube_video_video_post)
    send_post(peertube_video_video_post.id, session=db.session)
    assert LOOPS_INBOX not in peertube_video_sent
    assert len(peertube_video_sent.calls) == 5   # everyone else as before


def test_exactly_100_mb_is_sent(peertube_video_video_post):
    with_media(peertube_video_video_post, bytes=100 * 1024 * 1024)
    assert uses_loops_video(peertube_video_video_post)


def test_a_video_peertube_could_not_take_is_not_sent_to_loops_either(peertube_video_sent, peertube_video_video_post, loops_follower):
    """uses_loops_video starts from uses_peertube_video: a post that is not a ready video goes the Page path."""
    with_media(peertube_video_video_post, status='processing')
    db.session.commit()
    assert not uses_loops_video(peertube_video_video_post)


def test_a_loops_follower_blocked_by_the_author_is_skipped(peertube_video_sent, peertube_video_video_post, loops_follower):
    from tests.factories import make_instance_block
    make_instance_block(peertube_video_video_post.author, loops_follower.instance)
    send_post(peertube_video_video_post.id, session=db.session)
    assert LOOPS_INBOX not in peertube_video_sent
