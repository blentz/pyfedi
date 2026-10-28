"""Run before each FediTest direction. The two directions share alice and bob, so what one leaves behind must not
change what the other sees: every follow between them is undone (a note reaches PieFed only through a follow), and
bob is aged. A new remote account may post 3 times in its first day (app.utils.can_create_post); the stack's bob is
minutes old, and the tests make a dozen posts, so he would be refused for a reason that has nothing to do with them."""
import os
from contextlib import closing

import psycopg2

from interop.clients import Mastodon, PieFed, tokens, wait_for
from interop.conformance.piefed_mapping import libpq_dsn


def query(sql):
    with closing(psycopg2.connect(libpq_dsn(os.environ['DATABASE_URL']))) as conn, conn:
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchall() if cur.description else []


def main():
    mastodon = Mastodon(tokens()['mastodon_bob'])
    alice = mastodon.resolve_account('alice@piefed.test')
    mastodon.unfollow(alice['id'])
    mastodon.remove_follower(alice['id'])
    piefed = PieFed('alice', 'interop-alice-pw')
    for remote_user_id, is_inward in query('SELECT remote_user_id, is_inward FROM user_follower'):
        if not is_inward:
            piefed.unfollow_user(remote_user_id)
    piefed.resolve('@bob@mastodon.test')   # PieFed knows him from here on
    query("UPDATE \"user\" SET created = now() - interval '30 days' WHERE ap_domain = 'mastodon.test'")
    wait_for(lambda: not query('SELECT 1 FROM user_follower'))


if __name__ == '__main__':
    main()
