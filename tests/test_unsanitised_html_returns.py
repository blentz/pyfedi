"""The HTML these four places build from values they do not control.

D1375 established the rule: a string a route returns never met Jinja's
autoescaping, so it has to be escaped where it is built (fact 796). That round
swept for `f"<` and `f'<` and fixed three typeahead endpoints -- and **the sweep's
output was truncated with `head -20`**, so four more sites were never looked at.
This round redid it as an AST sweep for every f-string HTML `return` in `app/`,
which finds ten: two interpolate only an `<int:>` URL parameter, four are already
sanitised, and these four were not.

D1381.

THE STORED XSS -- `first_paragraph` (app/utils.py). Rendered
`{{ first_paragraph(post.body_html) | safe }}` in four post-teaser macros, which is
the feed listing. Two branches build the same string one line apart and only the
second sanitised it:

    return f'<p>{second_paragraph.text}</p>'            # no allowlist_html
    return allowlist_html(f'<p>{first_para.text}</p>')  # sanitised

`.text` DECODES entities, so a body carrying `&lt;img src=x onerror=alert(1)&gt;`
-- which is exactly what `allowlist_html` produces from an author who typed a
literal `<img ...>` -- came back out as live markup. Measured:

    second.text = '<img src=x onerror=alert(1)>'
    returned    = '<p><img src=x onerror=alert(1)></p>'

Reached by any author, because the branch needs a first paragraph of `Summary`,
`*Summary*`, `Comments`, or one starting `cross-posted from:` -- all of which they
write. Every viewer of the listing gets it.

THE THIRD-PARTY ANSWERS. `post_teaser_translate` and `post_translate` interpolate
whatever the configured `TRANSLATE_ENDPOINT` returned, and `post_check_ai`
whatever `DETECT_AI_ENDPOINT` returned. Both are network services whose responses
were trusted verbatim. `post_teaser_translate` also puts `post.slug` inside
`href="..."`, and a slug is built from `community.name`, which for a remote
community comes from the peer's actor document and restricts no characters (fact
797).

WHAT IS ESCAPED AND WHAT IS SANITISED. A title, a slug and a detector's verdict
are text, so they are escaped. A translated post BODY is meant to be HTML -- it is
the translation of `body_html` -- so it goes through `allowlist_html`, the
sanitiser every other reader of that column relies on.
"""
import pytest
from unittest.mock import patch

from app.utils import allowlist_html, first_paragraph

PAYLOAD_TEXT = '&lt;img src=x onerror=alert(1)&gt;'
PAYLOAD_LIVE = '<img src=x onerror=alert(1)>'

pytestmark = pytest.mark.usefixtures('site')

TRIGGERS = ['Summary', '*Summary*', 'Comments',
            'cross-posted from: https://elsewhere.example/post/1']


# --------------------------------------------------------------------------
# first_paragraph
# --------------------------------------------------------------------------


class TestFirstParagraphsSecondParagraphBranch:
    @pytest.mark.parametrize('trigger', TRIGGERS)
    def test_an_escaped_tag_does_not_come_back_live(self, app, trigger):
        """The measured defect. Each trigger is a first paragraph an author can
        write, and each takes the branch that skipped the sanitiser."""
        html = f'<p>{trigger}</p><p>{PAYLOAD_TEXT}</p>'

        result = first_paragraph(html)

        assert 'onerror' not in result
        assert PAYLOAD_LIVE not in result

    @pytest.mark.parametrize('payload', [
        '&lt;script&gt;alert(1)&lt;/script&gt;',
        '&lt;iframe src="https://evil.test"&gt;&lt;/iframe&gt;',
        '&lt;svg onload=alert(1)&gt;',
        '&lt;a href="javascript:alert(1)"&gt;x&lt;/a&gt;',
    ])
    def test_nothing_that_executes_survives(self, app, payload):
        result = first_paragraph(f'<p>Summary</p><p>{payload}</p>')

        assert '<script' not in result
        assert '<iframe' not in result
        assert 'onload' not in result
        assert 'javascript:' not in result

    def test_the_second_paragraph_is_still_shown(self, app):
        """Not "the branch returns nothing": the whole point of the branch is to
        skip a boilerplate first paragraph and show the real text. A fix that
        emptied it would pass every row above."""
        result = first_paragraph('<p>Summary</p><p>The actual summary text.</p>')

        assert 'The actual summary text.' in result

    def test_the_two_branches_agree(self, app):
        """The property, rather than a payload list: both branches build `<p>` +
        text and must treat that text the same way. The defect was one line's
        divergence from the line below it, so the invariant is that they agree.
        """
        body = PAYLOAD_TEXT

        via_second = first_paragraph(f'<p>Summary</p><p>{body}</p>')
        via_first = first_paragraph(f'<p>{body}</p>')

        assert via_second == via_first

    def test_an_ordinary_post_is_unchanged(self, app):
        result = first_paragraph('<p>Just a normal opening line.</p>')

        assert 'Just a normal opening line.' in result

    def test_a_body_with_no_paragraph_is_empty(self, app):
        assert first_paragraph('<div>no p here</div>') == ''

    def test_a_trigger_with_no_second_paragraph_falls_through(self, app):
        """`if second_paragraph:` -- a post whose only paragraph is `Summary`
        takes the sanitised line below instead."""
        assert 'Summary' in first_paragraph('<p>Summary</p>')


# --------------------------------------------------------------------------
# The translate routes
# --------------------------------------------------------------------------


def _translated(app, env, url, answer):
    """Drive one translate route with the translation service doubled.

    The two routes are `/post_teaser/<id>/translate` and `/post/<id>/translate`,
    so the caller passes the whole URL -- guessing one path for both answered 404
    and every "the payload is absent" assertion passed on the error page.
    """
    from tests.test_post_fragments import as_user, csrf

    anon, community, post, mod, author, outsider = env
    client = as_user(app, author)
    token = csrf(app, client)
    with patch.dict(app.config, {'TRANSLATE_ENDPOINT': 'https://tr.example',
                                 'TRANSLATE_KEY': ''}):
        with patch('app.post.routes.libretranslate_string', return_value=answer):
            response = client.post(url.format(id=post.id),
                                   data={'csrf_token': token})
    assert response.status_code == 200, response.data
    return response


@pytest.fixture
def post_env(app, db_session):
    """The seed tests/test_post_fragments.py uses for these same routes, built
    here rather than imported: a fixture function imported from another module is
    not registered as a fixture, and copying its body would drift from it.
    """
    from app import db
    from app.models import Language, Site
    from tests.factories import (make_community, make_community_member,
                                 make_instance, make_post, make_user)

    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = make_instance('test.piefed.local')
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    mod = make_user(local, 'mod', local=True, with_keys=True)
    mod.verified = True
    author = make_user(local, 'author', local=True, with_keys=True)
    author.verified = True
    outsider = make_user(local, 'outsider', local=True, with_keys=True)
    outsider.verified = True
    community = make_community('general')
    db.session.add(Language(code='und', name='Undetermined'))
    db.session.commit()
    make_community_member(mod, community, is_moderator=True)
    make_community_member(author, community)
    post = make_post(community, author, 'https://test.piefed.local/p/1',
                     title='THETITLE')
    post.body = 'the body in markdown'
    post.body_html = '<p>THEBODY</p>'
    db.session.commit()
    return app.test_client(), community, post, mod, author, outsider


class TestTheTranslationServicesAnswer:
    """`libretranslate_string` returns whatever the configured endpoint said."""

    ANSWERS = ['<img src=x onerror=alert(1)>',
               '<script>alert(1)</script>',
               '" onmouseover="alert(1)',
               '</a><svg onload=alert(1)>']

    @staticmethod
    def _no_live_markup(fragment: bytes):
        """The assertion that matters. `onerror` as a SUBSTRING is still present
        after escaping -- `&lt;img src=x onerror=alert(1)&gt;` contains it -- so
        asserting on the substring fails against a correct fix. What must be
        absent is a live tag: an unescaped `<` starting one.
        """
        # The element's own `</a></h3>` is deliberately not in this list: it is
        # the structure the route builds, not anything a payload put there. The
        # `</a><svg ...>` payload is caught by `<svg`.
        for tag in (b'<img', b'<script', b'<svg', b'<iframe'):
            assert tag not in fragment, fragment

    @pytest.mark.parametrize('answer', ANSWERS)
    def test_a_teaser_title_is_escaped(self, app, post_env, answer):
        response = _translated(app, post_env, '/post_teaser/{id}/translate', answer)

        title = response.data.split(b'post_teaser_title_a">')[1]
        self._no_live_markup(title)
        assert b'&lt;' in title or b'&quot;' in title or b'&#34;' in title

    @pytest.mark.parametrize('answer', ANSWERS)
    def test_a_full_post_title_is_escaped(self, app, post_env, answer):
        response = _translated(app, post_env, '/post/{id}/translate', answer)

        title = response.data.split(b'h1.post_title">')[1]
        self._no_live_markup(title)

    def test_a_translated_body_keeps_its_formatting(self, app, post_env):
        """The body is meant to be HTML, so it is sanitised rather than escaped
        -- a fix that escaped it would show readers raw tags. Asserted on the body
        div alone, because the double answers both calls and the title beside it
        is escaped, which is the point."""
        response = _translated(app, post_env, '/post/{id}/translate',
                               '<p>Bonjour <strong>le monde</strong></p>')

        body = response.data.split(b'</div>')[0]
        assert b'<strong>le monde</strong>' in body
        assert b'&lt;strong&gt;' not in body

    def test_a_dangerous_translated_body_is_sanitised_not_escaped(self, app,
                                                                 post_env):
        """The body's own payload case: `allowlist_html` keeps the `img` and
        drops the handler, rather than showing the reader escaped text."""
        response = _translated(app, post_env, '/post/{id}/translate',
                               '<p>hi</p><img src=x onerror=alert(1)>')

        body = response.data.split(b'</div>')[0]
        assert b'onerror' not in body
        assert b'<p>hi</p>' in body

    def test_a_translated_title_is_still_shown(self, app, post_env):
        response = _translated(app, post_env, '/post_teaser/{id}/translate', 'Bonjour')

        assert b'Bonjour' in response.data


class TestThePostSlugInTheHref:
    def test_a_remote_community_name_cannot_leave_the_attribute(self, app,
                                                                post_env):
        """`post.slug` is `/c/{community.name}@{community.ap_domain}/p/...`, and
        a remote community's name is the peer's `preferredUsername`, unfiltered
        (fact 797). It lands inside `href="..."`."""
        from app import db

        anon, community, post, mod, author, outsider = post_env
        post.slug = '/c/evil"onmouseover="alert(1)@peer.example/p/1/x'
        db.session.commit()

        response = _translated(app, post_env, '/post_teaser/{id}/translate', 'Bonjour')

        assert b'onmouseover="alert' not in response.data
        assert b'&quot;' in response.data or b'&#34;' in response.data


# --------------------------------------------------------------------------
# post_check_ai
# --------------------------------------------------------------------------


class TestTheDetectorsAnswer:
    def _checked(self, app, post_env, payload):
        from tests.test_post_fragments import as_user, csrf

        anon, community, post, mod, author, outsider = post_env
        client = as_user(app, author)
        token = csrf(app, client)
        with patch.dict(app.config, {'DETECT_AI_ENDPOINT': 'https://ai.example/x'}):
            with patch('app.post.routes.get_request') as fetched:
                fetched.return_value.status_code = 200
                fetched.return_value.json.return_value = payload
                return client.post(f'/post/{post.id}/check_ai',
                                   data={'csrf_token': token})

    def test_the_verdict_string_is_escaped(self, app, post_env):
        """`detection_result` is a string the detector chose, interpolated into
        HTML. `.upper()` does not make it safe -- and it is why these assertions
        are case-insensitive: the unfixed code produced
        `<IMG SRC=X ONERROR=ALERT(1)>`, which no lowercase `onerror` check finds
        and every browser still executes, HTML tag and attribute names being
        case-insensitive. Two mutants survived a lowercase assertion here.
        """
        response = self._checked(app, post_env,
                                 {'detection_result': '<img src=x onerror=alert(1)>',
                                  'confidence': 0.5})

        lowered = response.data.lower()
        # `<img` and not `onerror`: after escaping, `onerror` is still present as
        # a SUBSTRING of `&lt;img src=x onerror=alert(1)&gt;`, which is inert. The
        # live tag is what must be absent -- the same distinction the translate
        # tests above make, and asserting the substring instead is what made this
        # test fail against a correct fix.
        assert b'<img' not in lowered
        assert b'&lt;img' in lowered

    def test_an_attachment_verdict_is_escaped_too(self, app, post_env):
        """The second, textually separate interpolation of the same value."""
        response = self._checked(app, post_env, {
            'detection_result': 'ai', 'confidence': 0.9,
            'attachment': {'detection_result': '<script>alert(1)</script>',
                           'confidence': 0.8}})

        lowered = response.data.lower()
        assert b'<script' not in lowered
        assert b'&lt;script' in lowered

    def test_an_ordinary_verdict_is_still_reported(self, app, post_env):
        response = self._checked(app, post_env,
                                 {'detection_result': 'ai', 'confidence': 0.93})

        assert b'AI' in response.data
        assert b'93% confident' in response.data
