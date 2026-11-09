"""PieFed's notifications that have a Mastodon form, and its name for each (plan W1.2): read by the notifications list
(app.mastodon.notifications) and by Web Push (app.push_delivery). Imports nothing of the API's routes."""
from app.constants import NOTIF_COMMUNITY, NOTIF_FEED, NOTIF_FOLLOW, NOTIF_FOLLOW_REQUEST, NOTIF_MENTION, \
    NOTIF_POST, NOTIF_REPLY, NOTIF_TOPIC, NOTIF_USER

POST_TYPES = (NOTIF_USER, NOTIF_COMMUNITY, NOTIF_TOPIC, NOTIF_FEED)
COMMENT_TYPES = (NOTIF_POST, NOTIF_REPLY)
TYPES = {**{kind: 'status' for kind in POST_TYPES}, **{kind: 'mention' for kind in COMMENT_TYPES},
         NOTIF_MENTION: 'mention', NOTIF_FOLLOW: 'follow', NOTIF_FOLLOW_REQUEST: 'follow_request'}
