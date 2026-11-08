"""The newest release of each Android app under test, fetched when the harness runs (owner policy: track the latest).

An app's matrix.json entry names where its APK comes from:
- {"fdroid": "<package>"}: f-droid.org's suggested version, from its package API.
- {"github": "<owner>/<repo>", "asset": "<regex>"}: the latest GitHub release's first asset matching the regex (with
  "prerelease": true, the newest release even if it is a prerelease: Thunder ships its PieFed fixes only there). When
  several match, one built for x86_64 or for every ABI wins over an arm-only one: the emulator is x86_64 (it can
  translate arm64, but slower).
Downloads are cached by file name under interop/apps/.apks (gitignored), so a re-run fetches only new releases.
"""
import re
from dataclasses import dataclass
from pathlib import Path

import httpx

CACHE = Path(__file__).parent / '.apks'
FDROID = 'https://f-droid.org'
GITHUB = 'https://api.github.com'


@dataclass(frozen=True)
class Apk:
    url: str
    version: str
    filename: str


def _abi_rank(name: str) -> int:
    lowered = name.lower()
    if 'x86_64' in lowered or 'x86-64' in lowered:
        return 0
    if not re.search(r'arm|aarch|x86', lowered):
        return 1   # no ABI in the name: a universal APK
    if 'arm64' in lowered or 'aarch64' in lowered:
        return 2
    return 3


def resolve(source: dict, http: httpx.Client) -> Apk:
    if 'fdroid' in source:
        package = source['fdroid']
        info = http.get(f'{FDROID}/api/v1/packages/{package}').raise_for_status().json()
        code = info['suggestedVersionCode']
        name = next((p.get('versionName') for p in info.get('packages', []) if p.get('versionCode') == code), None)
        filename = f'{package}_{code}.apk'
        return Apk(f'{FDROID}/repo/{filename}', name or str(code), filename)
    if 'github' in source:
        if source.get('prerelease'):
            # the newest release, prerelease or not: GitHub lists them newest first, and `latest` skips prereleases
            releases = http.get(f'{GITHUB}/repos/{source["github"]}/releases', params={'per_page': 10}).raise_for_status().json()
            release = next((r for r in releases if not r.get('draft')), None)
            if release is None:
                raise LookupError(f'{source["github"]}: no published release')
        else:
            release = http.get(f'{GITHUB}/repos/{source["github"]}/releases/latest').raise_for_status().json()
        pattern = re.compile(source.get('asset', r'\.apk$'))
        assets = [a for a in release.get('assets', []) if pattern.search(a['name'])]
        if not assets:
            raise LookupError(f'{source["github"]} {release.get("tag_name")}: no asset matches {pattern.pattern}')
        asset = min(assets, key=lambda a: (_abi_rank(a['name']), a['name']))
        return Apk(asset['browser_download_url'], release.get('tag_name', '').lstrip('v'), asset['name'])
    raise ValueError(f'unknown APK source {source!r}')


def fetch(source: dict, http: httpx.Client, cache: Path = CACHE) -> tuple[Path, str]:
    """The APK on disk and its version."""
    apk = resolve(source, http)
    cache.mkdir(parents=True, exist_ok=True)
    path = cache / apk.filename
    if not path.exists() or path.stat().st_size == 0:
        partial = path.with_name(path.name + '.part')
        with http.stream('GET', apk.url, follow_redirects=True) as response:
            response.raise_for_status()
            with partial.open('wb') as out:
                for chunk in response.iter_bytes():
                    out.write(chunk)
        partial.rename(path)
    return path, apk.version
