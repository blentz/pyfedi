"""Resolving and caching the client-app harness's APKs (interop/apps/apks.py), against canned F-Droid and GitHub."""
import httpx
import pytest

from interop.apps.apks import _abi_rank, fetch, resolve


def _client(routes, served=None):
    served = served if served is not None else []

    def handle(request):
        served.append(str(request.url))
        if request.url.path.endswith('.apk'):
            return httpx.Response(200, content=b'PK-apk')
        body = routes.get(str(request.url))
        return httpx.Response(200, json=body) if body is not None else httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handle))


FDROID = {'https://f-droid.org/api/v1/packages/com.keylesspalace.tusky': {
    'packageName': 'com.keylesspalace.tusky', 'suggestedVersionCode': 130,
    'packages': [{'versionName': '31.0', 'versionCode': 131}, {'versionName': '30.0', 'versionCode': 130}]}}
GITHUB = {'https://api.github.com/repos/thunder-app/thunder/releases/latest': {
    'tag_name': 'v0.7.0', 'assets': [
        {'name': 'thunder-v0.7.0-arm64-v8a.apk', 'browser_download_url': 'https://gh/arm64.apk'},
        {'name': 'thunder-v0.7.0-x86_64.apk', 'browser_download_url': 'https://gh/x86_64.apk'},
        {'name': 'thunder-v0.7.0.apk.sha256', 'browser_download_url': 'https://gh/sum'}]}}


def test_fdroid_takes_the_suggested_version_not_the_newest():
    apk = resolve({'fdroid': 'com.keylesspalace.tusky'}, _client(FDROID))
    assert apk.url == 'https://f-droid.org/repo/com.keylesspalace.tusky_130.apk'
    assert apk.version == '30.0'


def test_fdroid_without_a_version_name_uses_the_code():
    routes = {'https://f-droid.org/api/v1/packages/x.y': {'suggestedVersionCode': 7, 'packages': []}}
    assert resolve({'fdroid': 'x.y'}, _client(routes)).version == '7'


def test_github_prefers_the_x86_64_asset():
    apk = resolve({'github': 'thunder-app/thunder', 'asset': r'\.apk$'}, _client(GITHUB))
    assert (apk.url, apk.version, apk.filename) == ('https://gh/x86_64.apk', '0.7.0', 'thunder-v0.7.0-x86_64.apk')


def test_github_prerelease_takes_the_newest_published_release():
    routes = {'https://api.github.com/repos/thunder-app/thunder/releases?per_page=10': [
        {'tag_name': 'v0.9.1', 'draft': True, 'assets': []},
        {'tag_name': 'v0.9.0-5', 'prerelease': True, 'assets': [{'name': 'thunder-x86_64.apk', 'browser_download_url': 'https://gh/pre.apk'}]},
        {'tag_name': 'v0.8.6', 'assets': [{'name': 'thunder-x86_64.apk', 'browser_download_url': 'https://gh/stable.apk'}]}]}
    apk = resolve({'github': 'thunder-app/thunder', 'asset': r'x86_64\.apk$', 'prerelease': True}, _client(routes))
    assert (apk.url, apk.version) == ('https://gh/pre.apk', '0.9.0-5')


def test_github_prerelease_with_only_drafts_says_so():
    routes = {'https://api.github.com/repos/o/r/releases?per_page=10': [{'tag_name': 'v1', 'draft': True}]}
    with pytest.raises(LookupError, match='no published release'):
        resolve({'github': 'o/r', 'prerelease': True}, _client(routes))


def test_github_with_no_matching_asset_says_so():
    with pytest.raises(LookupError, match='no asset matches'):
        resolve({'github': 'thunder-app/thunder', 'asset': r'\.aab$'}, _client(GITHUB))


def test_an_unknown_source_is_refused():
    with pytest.raises(ValueError):
        resolve({'play': 'x'}, _client({}))


@pytest.mark.parametrize('name, rank', [
    ('app-x86_64-release.apk', 0), ('app-release.apk', 1), ('app-universal.apk', 1),
    ('app-arm64-v8a.apk', 2), ('app-armeabi-v7a.apk', 3), ('app-x86.apk', 3),
], ids=['x86_64', 'plain', 'universal', 'arm64', 'armv7', 'x86'])
def test_abi_ranking(name, rank):
    assert _abi_rank(name) == rank


def test_fetch_downloads_once_then_reuses_the_cache(tmp_path):
    served = []
    path, version = fetch({'fdroid': 'com.keylesspalace.tusky'}, _client(FDROID, served), cache=tmp_path)
    assert (path.read_bytes(), version) == (b'PK-apk', '30.0')
    fetch({'fdroid': 'com.keylesspalace.tusky'}, _client(FDROID, served), cache=tmp_path)
    assert sum(url.endswith('.apk') for url in served) == 1
    assert not list(tmp_path.glob('*.part'))
