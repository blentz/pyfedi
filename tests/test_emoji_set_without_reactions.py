"""Setting an emoji reaction on a post or comment that has none yet, with no emoji in the form (fuzz-routes finding,
2026-11-13): the reactions partial looped over emoji_reactions, a JSON column that is None until a first reaction, and
raised TypeError, a 500. The partial is the reactions it has, none here, and keeps its id for htmx to swap into."""
import pytest

from tests.test_vote_direction_route import voter  # noqa: F401  (a fixture: a signed-in voter, a post and a reply)

pytestmark = pytest.mark.usefixtures('site')


def test_a_post_with_no_reactions_answers_its_empty_reactions(voter):
    client, post, _reply = voter
    response = client.post(f'/post/{post.id}/emoji_set')
    assert response.status_code == 200 and f'id="reactions_{post.id}"' in response.get_data(as_text=True)


def test_a_comment_with_no_reactions_answers_its_empty_reactions(voter):
    client, _post, reply = voter
    response = client.post(f'/comment/{reply.id}/emoji_set')
    assert response.status_code == 200 and f'id="reactions_{reply.id}"' in response.get_data(as_text=True)
