"""A profile's RSS feed, and the file-upload page behind it.

Sub-project 81, slice D. Five defects, all measured:

* a DELETED account's posts were still syndicated at `/u/<name>/feed`, while
  the profile page itself refuses (D1056);
* posts in a PRIVATE community appeared in the author's public feed, because
  `user.posts` is every post the account has made whatever community it is in
  -- `show_community_rss` refuses a private community outright (D1057);
* the feed's id, alternate link and self link all described `/c/<actor>`, a
  COMMUNITY url, in a feed about a user (D1058);
* `if post.body_html is None: continue` dropped every post with no body from
  the feed, which is most link and image posts (D1059);
* the storage quota was checked only on the render path, after the POST had
  stored everything and returned a redirect, so it could not refuse an upload
  (D1060), and the URL box could add a File row per line with no cap (D1062).
"""
from unittest.mock import patch

import pytest

from app import db
from app.models import Community, File, Instance, Site, User
from tests.factories import (make_community, make_community_member,
                             make_instance, make_post, make_user)

pytestmark = pytest.mark.usefixtures('site')


def instance(domain='test.piefed.local', software='piefed'):
    """Fact 394."""
    existing = Instance.query.filter_by(domain=domain).first()
    return existing if existing is not None else make_instance(domain,
                                                               software=software)


def csrf(app, client):
    """Fact 355."""
    from flask import session as flask_session
    from flask_wtf.csrf import generate_csrf

    with app.test_request_context():
        token = generate_csrf()
        raw = flask_session['csrf_token']
    with client.session_transaction() as session:
        session['csrf_token'] = raw
    return token


def as_user(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


@pytest.fixture
def env(app, db_session):
    site = db.session.get(Site, 1)
    site.private_instance = False
    db.session.commit()
    local = instance()
    founder = make_user(local, 'founder', local=True)
    assert founder.id == 1  # fact 347
    author = make_user(local, 'author', local=True)
    author.ap_profile_id = 'https://test.piefed.local/u/author'
    db.session.commit()
    community = make_community('general')
    db.session.commit()
    make_community_member(author, community)
    db.session.commit()
    return app.test_client(), author, community


def a_post(community, author, number=1, title=None, body='the body',
           **columns):
    post = make_post(community, author,
                     f'https://test.piefed.local/p/{number}',
                     title=title or f'post {number}')
    if body is not None:
        post.body_html = f'<p>{body}</p>'
    for column, value in columns.items():
        setattr(post, column, value)
    db.session.commit()
    return post


def a_private_community(name='secret'):
    community = make_community(name)
    community.private = True
    db.session.commit()
    return community


# --------------------------------------------------------------------------
# The feed itself
# --------------------------------------------------------------------------


def test_the_feed_lists_the_authors_posts(app, env):
    client, author, community = env
    a_post(community, author, 1, title='a post of mine')

    response = client.get('/u/author/feed')

    assert response.status_code == 200
    assert response.headers['Content-Type'] == 'application/rss+xml'
    assert b'a post of mine' in response.data


def test_the_feed_is_about_the_user_not_a_community(app, env):
    """D1058. The id, the alternate link and the self link were all
    `/c/{actor}` -- a community url -- in a feed about a person.

    Only ONE of the three is observable here, and the pin says so: feedgen's
    RSS writer emits no atom id, and the channel `<link>` it does emit carries
    whichever link was set LAST, which is the self link. So reverting the id or
    the alternate alone leaves every row in this file green; reverting the self
    link fails this one. The other two are corrected because they are wrong,
    not because a row can see them."""
    client, author, community = env
    a_post(community, author, 1)

    response = client.get('/u/author/feed')
    body = response.data.decode()

    assert '/u/author' in body
    assert '/c/author' not in body


def test_a_deleted_accounts_feed_is_not_served(app, env):
    """D1056. `find_local_user` filters `banned` but NOT `deleted`, so a
    deleted account's posts were still syndicated here while `show_profile`
    refuses the page. Measured: the post was listed."""
    client, author, community = env
    a_post(community, author, 1, title='a post by a deleted user')
    author.deleted = True
    db.session.commit()

    response = client.get('/u/author/feed')

    assert response.status_code == 404
    assert b'a post by a deleted user' not in response.data


def test_a_banned_accounts_feed_is_not_served(app, env):
    """The lookup's own filter, which is what covers the banned half."""
    client, author, community = env
    a_post(community, author, 1)
    author.banned = True
    db.session.commit()

    response = client.get('/u/author/feed')

    assert response.status_code == 404


def test_an_unknown_account_has_no_feed(app, env):
    client, author, community = env

    response = client.get('/u/nobody/feed')

    assert response.status_code == 404


def test_a_private_communitys_posts_stay_out_of_the_feed(app, env):
    """D1057. `user.posts` is every post the account has made, whatever
    community it is in, so a post in a private community appeared in the
    author's PUBLIC feed. Measured: `PROBE x2 private post listed: True`.
    `show_community_rss` refuses a private community outright (D1013); this
    route never looked at the community at all."""
    client, author, community = env
    private = a_private_community()
    make_community_member(author, private)
    db.session.commit()
    a_post(community, author, 1, title='a public post')
    a_post(private, author, 2, title='a post in a private community')

    response = client.get('/u/author/feed')

    assert b'a public post' in response.data
    assert b'a post in a private community' not in response.data


def test_a_banned_communitys_posts_stay_out_of_the_feed(app, env):
    """The same join's second filter: a community this instance has banned
    answers 404 for its own feed, so its posts must not leak through an
    author's."""
    client, author, community = env
    banned = make_community('banned_community')
    banned.banned = True
    db.session.commit()
    make_community_member(author, banned)
    db.session.commit()
    a_post(community, author, 1, title='a public post')
    a_post(banned, author, 2, title='a post in a banned community')

    response = client.get('/u/author/feed')

    assert b'a public post' in response.data
    assert b'a post in a banned community' not in response.data


def test_a_post_with_no_body_is_still_listed(app, env):
    """D1059. `if post.body_html is None: continue` dropped the post from the
    feed entirely -- and a link or image post ordinarily has no body, so most
    of them were silently missing. Measured: `PROBE x4 with body listed: True
    | bodyless listed: False`."""
    client, author, community = env
    a_post(community, author, 1, title='has a body')
    a_post(community, author, 2, title='a link post with no body', body=None)

    response = client.get('/u/author/feed')

    assert b'has a body' in response.data
    assert b'a link post with no body' in response.data


def test_a_post_whose_body_cannot_be_encoded_is_skipped(app, env):
    """The check that remains: an UNENCODABLE body is a reason to skip an
    entry, because feedgen would produce a document no reader can parse."""
    client, author, community = env
    a_post(community, author, 1, title='fine')
    bad = a_post(community, author, 2, title='unencodable')
    bad.body_html = '<p>bad</p>'
    db.session.commit()

    with patch('app.user.routes.is_valid_xml_utf8',
               side_effect=lambda text: 'bad' not in text):
        response = client.get('/u/author/feed')

    assert b'fine' in response.data
    assert b'unencodable' not in response.data


def test_a_post_whose_title_cannot_be_encoded_is_skipped(app, env):
    client, author, community = env
    a_post(community, author, 1, title='fine')
    a_post(community, author, 2, title='unencodable title')

    with patch('app.user.routes.is_valid_xml_utf8',
               side_effect=lambda text: 'unencodable' not in text):
        response = client.get('/u/author/feed')

    assert b'fine' in response.data
    assert b'unencodable title' not in response.data


@pytest.mark.parametrize('columns', [
    {'deleted': True},
    {'from_bot': True},
])
def test_the_feed_omits_what_the_profile_omits(app, env, columns):
    client, author, community = env
    a_post(community, author, 1, title='visible')
    a_post(community, author, 2, title='hidden', **columns)

    response = client.get('/u/author/feed')

    assert b'visible' in response.data
    assert b'hidden' not in response.data


def test_an_unchanged_feed_is_a_304(app, env):
    client, author, community = env
    a_post(community, author, 1)
    first = client.get('/u/author/feed')

    second = client.get('/u/author/feed',
                        headers={'If-None-Match': first.headers['ETag']})

    assert second.status_code == 304


def test_the_feed_carries_the_avatar_and_bio(app, env):
    client, author, community = env
    avatar = File(source_url='https://test.piefed.local/avatar.png',
                  file_path='app/static/media/avatar.png')
    db.session.add(avatar)
    db.session.commit()
    author.avatar_id = avatar.id
    author.about = 'what I write about'
    db.session.commit()
    a_post(community, author, 1)

    response = client.get('/u/author/feed')

    assert b'avatar.png' in response.data
    assert b'what I write about' in response.data


def test_a_feed_without_an_avatar_or_bio_still_works(app, env):
    """Both `else` arms. feedgen refuses an empty subtitle, which is why the
    fallback is a space rather than ''."""
    client, author, community = env
    author.about = None
    author.avatar_id = None
    db.session.commit()
    a_post(community, author, 1)

    response = client.get('/u/author/feed')

    assert response.status_code == 200
    assert b'apple-touch-icon.png' in response.data


def test_a_media_url_becomes_an_enclosure(app, env):
    client, author, community = env
    a_post(community, author, 1, url='https://example.com/audio.mp3')

    response = client.get('/u/author/feed')

    assert b'enclosure' in response.data


def test_the_same_url_twice_is_only_enclosed_once(app, env):
    """`already_added` -- two posts sharing a url would otherwise produce two
    entries claiming the same enclosure."""
    client, author, community = env
    a_post(community, author, 1, title='first', url='https://example.com/a.mp3')
    a_post(community, author, 2, title='second', url='https://example.com/a.mp3')

    response = client.get('/u/author/feed')

    assert response.data.count(b'<enclosure') == 1


def test_a_post_with_a_slug_is_linked_by_it(app, env):
    client, author, community = env
    post = a_post(community, author, 1)
    post.slug = '/post/1/a-nice-title'
    db.session.commit()

    response = client.get('/u/author/feed')

    assert b'/post/1/a-nice-title' in response.data


def test_a_post_without_a_slug_falls_back_to_its_id(app, env):
    client, author, community = env
    post = a_post(community, author, 1)
    post.slug = None
    db.session.commit()

    response = client.get('/u/author/feed')

    assert f'/post/{post.id}'.encode() in response.data


def test_a_remote_account_is_found_by_handle(app, env):
    client, author, community = env
    remote = make_user(make_instance('other.example', software='piefed'),
                       'remote')
    db.session.commit()
    a_post(community, remote, 1, title='a remote post')

    response = client.get('/u/remote@other.example/feed')

    assert response.status_code == 200


# --------------------------------------------------------------------------
# D1060, D1062 -- the file upload page
# --------------------------------------------------------------------------


def upload_payload(token, **overrides):
    data = {'urls': '', 'referrer': '', 'submit': 'Upload',
            'csrf_token': token}
    data.update(overrides)
    return data


def used_quota(user, size):
    file = File(source_url='https://example.com/existing.png')
    db.session.add(file)
    db.session.commit()
    db.session.execute(
        db.text('INSERT INTO "user_file" (file_id, user_id, size) '
                'VALUES (:f, :u, :s)'),
        {'f': file.id, 'u': user.id, 's': size})
    db.session.commit()
    return file


def test_adding_files_by_url(app, env):
    client, author, community = env
    uploader = as_user(app, author)
    token = csrf(app, uploader)

    response = uploader.post('/user/files/upload',
                             data=upload_payload(token,
                                                 urls='https://example.com/one.png\n'
                                                      'https://example.com/two.png'),
                             content_type='multipart/form-data')

    assert response.status_code == 302
    assert File.query.filter_by(source_url='https://example.com/one.png').count() == 1
    assert File.query.filter_by(source_url='https://example.com/two.png').count() == 1


def test_blank_lines_in_the_url_box_are_ignored(app, env):
    client, author, community = env
    uploader = as_user(app, author)
    token = csrf(app, uploader)

    # The blank lines have to be BETWEEN two urls: `form.urls.data.strip()`
    # removes leading and trailing whitespace before the split, so a trailing
    # blank line never reaches the loop and a row built from one tests nothing.
    uploader.post('/user/files/upload',
                  data=upload_payload(token,
                                      urls='https://example.com/one.png\n\n   \n'
                                           'https://example.com/two.png'),
                  content_type='multipart/form-data')

    assert File.query.count() == 2
    assert File.query.filter_by(source_url='').count() == 0


def test_the_url_box_is_capped(app, env):
    """D1062. The box holds 10,000 characters -- roughly a thousand lines --
    and each line became a File row that does not count towards the storage
    quota. D993's family: a list from a form with no cap on its length."""
    from app.user.routes import FILE_URLS_PER_UPLOAD

    client, author, community = env
    uploader = as_user(app, author)
    token = csrf(app, uploader)
    urls = '\n'.join(f'https://example.com/{n}.png'
                     for n in range(FILE_URLS_PER_UPLOAD + 10))

    uploader.post('/user/files/upload', data=upload_payload(token, urls=urls),
                  content_type='multipart/form-data')

    assert File.query.count() == FILE_URLS_PER_UPLOAD


def test_an_account_over_quota_cannot_upload(app, env):
    """D1060. The quota was checked ONLY on the render path, after the POST
    had stored everything and returned a redirect -- so the message it flashes
    announced the limit had been passed rather than refusing anything.
    Measured: an account already over quota uploaded two more files and got a
    302. `process_upload` has no quota check of its own."""
    client, author, community = env
    used_quota(author, app.config['FILE_UPLOAD_QUOTA'] + 1)
    uploader = as_user(app, author)
    token = csrf(app, uploader)

    with patch('app.user.routes.process_upload') as upload:
        with patch('app.user.routes.flash') as flashed:
            response = uploader.post(
                '/user/files/upload',
                data=upload_payload(token, urls='https://example.com/one.png'),
                content_type='multipart/form-data')

    assert response.status_code == 302
    assert File.query.filter_by(source_url='https://example.com/one.png').count() == 0
    assert upload.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'exceeded your storage quota' in messages


def test_an_account_inside_its_quota_can_upload(app, env):
    """The other side of the same condition, which a `return` that fired
    unconditionally would pass."""
    client, author, community = env
    used_quota(author, 1)
    uploader = as_user(app, author)
    token = csrf(app, uploader)

    uploader.post('/user/files/upload',
                  data=upload_payload(token, urls='https://example.com/one.png'),
                  content_type='multipart/form-data')

    assert File.query.filter_by(source_url='https://example.com/one.png').count() == 1


def test_an_account_over_quota_is_refused_the_page(app, env):
    """The render path's own refusal, which is where the check used to live
    alone."""
    client, author, community = env
    used_quota(author, app.config['FILE_UPLOAD_QUOTA'] + 1)
    uploader = as_user(app, author)

    with patch('app.user.routes.flash') as flashed:
        with patch('app.user.routes.render_template',
                   return_value='rendered') as render:
            response = uploader.get('/user/files/upload')

    assert response.status_code == 302
    assert render.call_args is None
    messages = ' '.join(str(call.args[0]) for call in flashed.call_args_list)
    assert 'exceeded your storage quota' in messages


def test_the_upload_page_renders_inside_the_quota(app, env):
    client, author, community = env
    uploader = as_user(app, author)

    with patch('app.user.routes.render_template',
               return_value='rendered') as render:
        response = uploader.get('/user/files/upload')

    assert response.status_code == 200
    assert render.call_args.kwargs['form'] is not None


@pytest.mark.parametrize('field', [f'file{n}' for n in range(1, 11)])
def test_each_file_field_is_processed(app, env, field):
    """Ten near-identical blocks; a copy-paste error in any of them would send
    the wrong field to `process_upload`, and a row for the first says nothing
    about the tenth."""
    import io

    client, author, community = env
    uploader = as_user(app, author)
    token = csrf(app, uploader)
    payload = upload_payload(token)
    payload[field] = (io.BytesIO(b'an image'), f'{field}.png')

    with patch('app.user.routes.process_upload') as upload:
        uploader.post('/user/files/upload', data=payload,
                      content_type='multipart/form-data')

    assert upload.call_args is not None
    assert upload.call_args.args[0].filename == f'{field}.png'


def test_an_upload_lands_back_where_it_started(app, env):
    """`safe_redirect_target(form.referrer.data, ...)` -- the page is reached
    from several places, and an open redirect here would be one a logged-in
    user could be walked into."""
    client, author, community = env
    uploader = as_user(app, author)
    token = csrf(app, uploader)

    response = uploader.post(
        '/user/files/upload',
        data=upload_payload(token, referrer='https://evil.example/'),
        content_type='multipart/form-data')

    assert 'evil.example' not in response.headers['Location']


def test_a_page_url_carries_no_enclosure(app, env):
    """`if type and not type.startswith('text/')` -- an ordinary link post's
    url is a web page, and an enclosure tells a reader to download it as
    media."""
    client, author, community = env
    a_post(community, author, 1, url='https://example.com/article.html')

    response = client.get('/u/author/feed')

    assert b'enclosure' not in response.data


def test_a_url_with_no_recognisable_type_carries_no_enclosure(app, env):
    """The first half of the same condition: `mimetype_from_url` answers None
    for a url it cannot type, and `fe.enclosure(..., type=None)` would put an
    empty type into the document."""
    client, author, community = env
    a_post(community, author, 1, url='https://example.com/whatever')

    with patch('app.user.routes.mimetype_from_url', return_value=None):
        response = client.get('/u/author/feed')

    assert b'enclosure' not in response.data


def test_a_blank_url_line_creates_no_file(app, env):
    """`if url and url.strip() != ''` -- people paste lists with trailing
    newlines, and a File row with an empty source_url is a row nothing can
    ever serve."""
    client, author, community = env
    uploader = as_user(app, author)
    token = csrf(app, uploader)

    uploader.post('/user/files/upload',
                  data=upload_payload(token, urls='   \n\n  \n'),
                  content_type='multipart/form-data')

    assert File.query.count() == 0
