"""A PeerTube user subscribes to a PieFed video_channel community, and the subscription is accepted (spec 2026-10-14
§6.2, §10 A11 and A12). PeerTube needs the Group to name its owner Person in attributedTo, and takes the Accept only
at the follower's own inbox: one at its shared inbox leaves the subscription pending."""
import psycopg2
import pytest

from interop.clients import wait_for

pytestmark = pytest.mark.interop


def _subscription_state(handle_name):
    """The state of carol's follow of the channel, from PeerTube's own table: its API lists a pending subscription
    the same as an accepted one."""
    with psycopg2.connect(host='peertube-db', dbname='peertube', user='peertube', password='peertube') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT f.state FROM "actorFollow" f JOIN actor a ON a.id = f."targetActorId" '
                        'JOIN actor me ON me.id = f."actorId" '
                        "WHERE a.\"preferredUsername\" = %s AND me.\"preferredUsername\" = 'carol'", (handle_name,))
            row = cur.fetchone()
            return row[0] if row else None


def test_a_peertube_subscription_to_a_piefed_video_channel_is_accepted(peertube):
    peertube.unsubscribe('vc1@piefed.test')    # `up` keeps the data: a repeated run subscribes afresh
    peertube.subscribe('vc1@piefed.test')

    def accepted():
        return _subscription_state('vc1') == 'accepted'
    wait_for(accepted, timeout=60)
