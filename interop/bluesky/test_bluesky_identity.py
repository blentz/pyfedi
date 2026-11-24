"""ATProto P4, end to end (spec 2026-11-23-atproto-federation-design.md §11, steps 4 and 6): alice, with a Bluesky
account made on PieFed, follows bob on the harness PDS; her follow, her like of his post and her reply to it are
records in her repo, her reply in his thread. bob replies to her microblog post's copy and likes it, and both land on
her post in PieFed (through Jetstream: she follows him). Run by `interop/run.sh bluesky` in the bluesky-runner
container."""
import time

import httpx

from interop.bluesky.test_bluesky_hosted import PDS, browser, clear_leftover, http, records_of, signed_in  # noqa: F401
from interop.bluesky.test_bluesky_inbound import HANDLE, bob, write  # noqa: F401
from interop.clients import PieFed, wait_for

PIEFED = 'https://piefed.test'


def found(piefed, url):   # PieFed's copy of a Bluesky post, by its bsky.app address
    try:
        return (piefed.resolve(url).get('post') or {}).get('post')   # the post_view's post
    except httpx.HTTPStatusError:
        return None


def piefed_me(piefed):
    return piefed.http.get('/user', params={'username': 'alice'}).raise_for_status().json()['person_view']['person']['id']


def mine(client, did, collection, test):
    return next((record for record in records_of(client, did, collection) if test(record['value'])), None)


def test_alice_and_bob_answer_each_other(browser, http, bob):   # noqa: F811
    clear_leftover(http, 'alice.piefed.test')
    alice = signed_in(browser, 'piefed.test', 'alice', 'interop-alice-pw')
    alice.get_by_role('button', name='Create @alice.piefed.test').click()
    alice.wait_for_load_state('networkidle')
    assert 'is ready' in alice.inner_text('body'), alice.inner_text('body')
    did = http.get('https://alice.piefed.test/.well-known/atproto-did').text.strip()
    piefed = PieFed('alice', 'interop-alice-pw')
    stamp = str(int(time.time()))

    # alice follows bob: a follow record in her repo
    alice.goto(f'{PIEFED}/u/{HANDLE}')
    if 'Unfollow' in alice.inner_text('body'):   # a run that stopped part way left the follow
        alice.get_by_role('link', name='Unfollow', exact=True).first.click()
        alice.wait_for_load_state('networkidle')
        alice.goto(f'{PIEFED}/u/{HANDLE}')
    alice.get_by_role('link', name='Follow', exact=True).first.click()
    alice.wait_for_load_state('networkidle')
    wait_for(lambda: mine(http, did, 'app.bsky.graph.follow', lambda value: value['subject'] == bob['did']),
             timeout=60)

    # bob posts; it reaches alice; she likes it and replies: both are records of hers, the reply in his thread
    uri = write(http, bob, f'Talk to me {stamp}')
    rkey = uri.rsplit('/', 1)[-1]
    post = wait_for(lambda: found(piefed, f'https://bsky.app/profile/{bob["did"]}/post/{rkey}'), timeout=90)
    piefed.like_post(post['id'], 1)
    wait_for(lambda: mine(http, did, 'app.bsky.feed.like', lambda value: value['subject']['uri'] == uri), timeout=60)
    piefed.reply(post['id'], f'Hello bob, from PieFed {stamp}')
    reply = wait_for(lambda: mine(http, did, 'app.bsky.feed.post',
                                  lambda value: value['text'] == f'Hello bob, from PieFed {stamp}'), timeout=60)
    assert reply['value']['reply']['parent']['uri'] == uri and reply['value']['reply']['root']['uri'] == uri

    # alice takes her like back: the record goes
    piefed.like_post(post['id'], 0)
    wait_for(lambda: mine(http, did, 'app.bsky.feed.like', lambda value: value['subject']['uri'] == uri) is None,
             timeout=60)

    # step 4: bob answers alice's microblog post's copy, and likes it: both land on her post here
    own = piefed.post(f'Answer me {stamp}', f'Answer me {stamp}', piefed.microblogs_id())['post']
    copy = wait_for(lambda: mine(http, did, 'app.bsky.feed.post', lambda value: value['text'].startswith(
        f'Answer me {stamp}')), timeout=60)
    ref = {'uri': copy['uri'], 'cid': copy['cid']}
    now = time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())
    for collection, record in (
            ('app.bsky.feed.post', {'$type': 'app.bsky.feed.post', 'text': f'Answered {stamp}', 'createdAt': now,
                                    'reply': {'root': ref, 'parent': ref}}),
            ('app.bsky.feed.like', {'$type': 'app.bsky.feed.like', 'subject': ref, 'createdAt': now})):
        http.post(f'{PDS}/xrpc/com.atproto.repo.createRecord', headers=bob['headers'], json={
            'repo': bob['did'], 'collection': collection, 'record': record}).raise_for_status()
    wait_for(lambda: any(f'Answered {stamp}' in reply['comment']['body'] for reply in piefed.replies(own['id'])),
             timeout=90)
    wait_for(lambda: piefed.get_post(own['id'])['counts']['upvotes'] >= 2, timeout=90)   # hers, and bob's

    # step 7 (A7): a post alice writes in a Bluesky app, signed in with an app password, becomes hers here, once,
    # and is not copied back to her repo
    alice.goto(f'{PIEFED}/user/settings/connections')
    alice.get_by_role('button', name='Get an app password').click()
    alice.wait_for_load_state('networkidle')
    app_password = alice.inner_text('#app_password').strip()
    app = http.post(f'{PDS}/xrpc/com.atproto.server.createSession',
                    json={'identifier': 'alice.piefed.test', 'password': app_password}).raise_for_status().json()
    text = f'Written in a Bluesky app {stamp}'
    http.post(f'{PDS}/xrpc/com.atproto.repo.createRecord', headers={'Authorization': f"Bearer {app['accessJwt']}"},
              json={'repo': did, 'collection': 'app.bsky.feed.post', 'record': {
                  '$type': 'app.bsky.feed.post', 'text': text,
                  'createdAt': time.strftime('%Y-%m-%dT%H:%M:%S.000Z', time.gmtime())}}).raise_for_status()

    def brought_in():
        posts = piefed.http.get('/post/list', params={'person_id': piefed_me(piefed), 'sort': 'New'}).json()
        return [view for view in posts.get('posts', []) if view['post'].get('body', '').startswith(text)]
    imported = wait_for(brought_in, timeout=90)
    time.sleep(5)   # time enough for a copy, were one to be made
    assert len(brought_in()) == 1 and len(imported) == 1
    assert len([record for record in records_of(http, did, 'app.bsky.feed.post')
                if record['value']['text'].startswith(text)]) == 1   # not copied back
    piefed.delete(imported[0]['post']['id'])

    # cleanup: the follow, the account (and with it her records), bob's post
    alice.goto(f'{PIEFED}/u/{HANDLE}')
    alice.get_by_role('link', name='Unfollow', exact=True).first.click()
    alice.wait_for_load_state('networkidle')
    piefed.delete(own['id'])
    alice.goto(f'{PIEFED}/user/settings/connections')
    alice.get_by_role('button', name='Delete this Bluesky account').click()
    alice.wait_for_load_state('networkidle')
    http.post(f'{PDS}/xrpc/com.atproto.repo.deleteRecord', headers=bob['headers'], json={
        'repo': bob['did'], 'collection': 'app.bsky.feed.post', 'rkey': rkey})
