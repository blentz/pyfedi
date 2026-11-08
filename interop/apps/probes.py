"""Protocol probes: the first calls an app of each family makes, sent to PieFed as that app would send them.

For apps whose client API PieFed does not serve (Mastodon's, PeerTube's, Lemmy's /api/v3), driving the app's UI would
only show its error screen. A probe instead makes the request the app's login and timeline screens start with and
records what PieFed answered: `unsupported` while PieFed has no such API, `passed` once it does, at which point the
gate (matrix.py) asks for the app to be driven for real. Each probe returns {step: (outcome, detail)}.

The calls, from each app's source (interop/apps/README.md lists the versions read):
- mastodon (Mastodon for Android, Tusky, Moshidon, Phanpy, Elk; Pixelfed's apps log in the same way): GET
  /api/v1/instance, then POST /api/v1/apps to register before the OAuth redirect; a logged-out timeline is GET
  /api/v1/timelines/public.
- peertube (the PeerTube app, Fedilab/TubeLab): GET /api/v1/oauth-clients/local, then POST /api/v1/users/token
  (password grant); browsing is GET /api/v1/videos.
- lemmy-v3 (Jerboa, and Thunder on a Lemmy-only build): POST /api/v3/user/login; browsing is GET /api/v3/post/list.
"""
import httpx

Probe = dict[str, tuple[str, str]]


def _json(response: httpx.Response):
    try:
        return response.json()
    except ValueError:
        return None


def _answer(response: httpx.Response) -> str:
    return f'{response.request.method} {response.request.url.path} answered {response.status_code}'


def mastodon(http: httpx.Client) -> Probe:
    app = http.post('/api/v1/apps', data={'client_name': 'interop-probe', 'redirect_uris': 'urn:ietf:wg:oauth:2.0:oob',
                                          'scopes': 'read write follow push', 'website': 'https://piefed.test'})
    body = _json(app)
    if app.status_code == 200 and isinstance(body, dict) and body.get('client_id'):
        login = ('passed', f'{_answer(app)} with a client_id')
    else:
        login = ('unsupported', f'{_answer(app)}: no app registration, so no OAuth login')
    timeline = http.get('/api/v1/timelines/public', params={'local': 'true', 'limit': 5})
    if timeline.status_code == 200 and isinstance(_json(timeline), list):
        view = ('passed', f'{_answer(timeline)} with a list of statuses')
    else:
        view = ('unsupported', f'{_answer(timeline)}: no Mastodon timelines')
    return {'login': login, 'view': view, 'post': _after(login, 'POST /api/v1/statuses')}


def peertube(http: httpx.Client) -> Probe:
    client = http.get('/api/v1/oauth-clients/local')
    body = _json(client)
    if client.status_code == 200 and isinstance(body, dict) and body.get('client_id'):
        login = ('passed', f'{_answer(client)} with a client_id')
    else:
        login = ('unsupported', f'{_answer(client)}: no OAuth client, so no password-grant login')
    videos = http.get('/api/v1/videos', params={'count': 5})
    body = _json(videos)
    if videos.status_code == 200 and isinstance(body, dict) and isinstance(body.get('data'), list):
        view = ('passed', f'{_answer(videos)} with a video list')
    else:
        view = ('unsupported', f'{_answer(videos)}: no PeerTube video API')
    return {'login': login, 'view': view, 'post': _after(login, 'POST /api/v1/videos/upload')}


def lemmy_v3(http: httpx.Client, username: str, password: str) -> Probe:
    response = http.post('/api/v3/user/login', json={'username_or_email': username, 'password': password})
    body = _json(response)
    if response.status_code == 200 and isinstance(body, dict) and body.get('jwt'):
        login = ('passed', f'{_answer(response)} with a jwt')
    else:
        login = ('unsupported', f'{_answer(response)}: no Lemmy v3 API')
    posts = http.get('/api/v3/post/list', params={'limit': 5})
    body = _json(posts)
    if posts.status_code == 200 and isinstance(body, dict) and isinstance(body.get('posts'), list):
        view = ('passed', f'{_answer(posts)} with a post list')
    else:
        view = ('unsupported', f'{_answer(posts)}: no Lemmy v3 API')
    return {'login': login, 'view': view, 'post': _after(login, 'POST /api/v3/post')}


def _after(login: tuple[str, str], call: str) -> tuple[str, str]:
    """Posting needs a login: blocked without one; with one, the app must be driven for real to post."""
    if login[0] != 'passed':
        return ('blocked', f'{call} needs a login')
    return ('passed', f'login works: drive the app to check {call}')


PROBES = {'mastodon': mastodon, 'peertube': peertube, 'lemmy-v3': lemmy_v3}


def run(family: str, http: httpx.Client, username: str = '', password: str = '') -> Probe:
    if family == 'lemmy-v3':
        return lemmy_v3(http, username, password)
    return PROBES[family](http)
