# Seed the harness PeerTube: user carol, and API tokens for root and carol. Idempotent. Run inside piefed-web by
# run.sh (it has python, httpx and the harness CA); prints one JSON line {"peertube_root": ..., "peertube_carol": ...}.
import json

import httpx

http = httpx.Client(base_url='https://peertube.test/api/v1', verify='/tmp/harness-ca.crt', timeout=10)
client = http.get('/oauth-clients/local').raise_for_status().json()


def token(username, password):
    form = {'client_id': client['client_id'], 'client_secret': client['client_secret'], 'grant_type': 'password',
            'username': username, 'password': password}
    return http.post('/users/token', data=form).raise_for_status().json()['access_token']


root = token('root', 'interop-root-pw')
r = http.post('/users', headers={'Authorization': f'Bearer {root}'},
              json={'username': 'carol', 'password': 'interop-carol-pw', 'email': 'carol@peertube.test',
                    'role': 2, 'videoQuota': -1, 'videoQuotaDaily': -1})
if r.status_code != 409:   # 409: carol exists from an earlier `up`
    r.raise_for_status()
print(json.dumps({'peertube_root': root, 'peertube_carol': token('carol', 'interop-carol-pw')}))
