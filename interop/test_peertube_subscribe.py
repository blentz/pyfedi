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


def _piefed_member(community_name):
    """Whether PieFed has carol as a member of the community."""
    with psycopg2.connect(host='piefed-db', dbname='piefed', user='piefed', password='piefed') as conn:
        with conn.cursor() as cur:
            cur.execute('SELECT 1 FROM community_member m JOIN community c ON c.id = m.community_id '
                        'JOIN "user" u ON u.id = m.user_id WHERE c.name = %s AND c.ap_id IS NULL '
                        "AND u.ap_profile_id = 'https://peertube.test/accounts/carol'", (community_name,))
            return cur.fetchone() is not None


def test_a_peertube_subscription_to_a_piefed_video_channel_is_accepted(peertube):
    """On a reused stack this is a resubscription: PeerTube sends the Follow with the first one's id
    (`<account>/follows/<channel actor id>`), which PieFed takes only because the Undo released its claim (§10 A14)."""
    was_accepted = _subscription_state('vc1') == 'accepted'
    peertube.unsubscribe('vc1@piefed.test')    # `up` keeps the data: a repeated run subscribes afresh
    if was_accepted:
        # PeerTube delivers the Undo and the next Follow concurrently; a Follow that reuses the id and lands first is
        # still a duplicate, as any user resubscribing within a second would find. Wait for the Undo to land.
        wait_for(lambda: not _piefed_member('vc1'), timeout=60)
    peertube.subscribe('vc1@piefed.test')

    def accepted():
        return _subscription_state('vc1') == 'accepted'
    wait_for(accepted, timeout=60)
