"""The reply forms show the post and comment being replied to. Where either carries a
content warning, its body sits in the same collapsed <details> the post page uses."""
import re

import pytest

from app import db
from tests.test_post_replies import a_reply, env  # noqa: F401  (env is a fixture)

pytestmark = pytest.mark.usefixtures('site')


def collapsed(html, text):
    """The <details class="content_warning"> block that holds `text`, or None."""
    for block in re.finditer(r'<details class="content_warning">.*?</details>', html, re.S):
        if text in block.group(0):
            return block.group(0)
    return None


@pytest.fixture(autouse=True)
def csrf_on(app):
    """A form's template calls `form.csrf_token()`, which the test config's WTF_CSRF_ENABLED = False leaves undefined."""
    app.config['WTF_CSRF_ENABLED'] = True
    yield
    app.config['WTF_CSRF_ENABLED'] = False


def warned(post, comment):
    post.body_html = '<p>the post body</p>'
    post.content_warning = 'post warning'
    comment.body_html = '<p>the comment body</p>'
    comment.content_warning = 'comment warning'
    db.session.commit()


def test_the_reply_form_collapses_a_warned_post_and_comment(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    warned(post, reply)

    html = client.get(f'/post/{post.id}/comment/{reply.id}/reply').get_data(as_text=True)

    assert re.search(r'<summary>\s*post warning\s*</summary>', collapsed(html, 'the post body'))
    assert re.search(r'<summary>\s*comment warning\s*</summary>', collapsed(html, 'the comment body'))


def test_the_reply_form_does_not_collapse_without_a_warning(app, env):
    client, community, post, mod, author, outsider = env
    reply = a_reply(post, author)
    warned(post, reply)
    post.content_warning = reply.content_warning = None
    db.session.commit()

    html = client.get(f'/post/{post.id}/comment/{reply.id}/reply').get_data(as_text=True)

    assert 'the post body' in html and 'the comment body' in html
    assert 'content_warning' not in html


def test_the_comment_edit_form_collapses_a_warned_post_and_parent(app, env):
    client, community, post, mod, author, outsider = env
    parent = a_reply(post, author)
    child = a_reply(post, author, body='the edited one', parent_id=parent.id)
    warned(post, parent)

    html = client.get(f'/post/{post.id}/comment/{child.id}/edit').get_data(as_text=True)

    assert collapsed(html, 'the post body') is not None
    assert collapsed(html, 'the comment body') is not None
