VERSION = '1.7.17'

POST_TYPE_LINK = 1
POST_TYPE_ARTICLE = 2
POST_TYPE_IMAGE = 3
POST_TYPE_VIDEO = 4
POST_TYPE_POLL = 5
POST_TYPE_EVENT = 6

POST_TYPE_NAMES = {
    POST_TYPE_LINK: "Link",
    POST_TYPE_ARTICLE: "Discussion",
    POST_TYPE_IMAGE: "Image",
    POST_TYPE_VIDEO: "Video",
    POST_TYPE_POLL: "Poll",
    POST_TYPE_EVENT: "Event",
}

POST_STATUS_SCHEDULED = -2
POST_STATUS_DRAFT = -1
POST_STATUS_REVIEWING = 0
POST_STATUS_PUBLISHED = 1
POST_STATUS_PROCESSING = -3   # uploaded video/audio awaiting ffprobe/ffmpeg

MEDIA_KIND_VIDEO = 'video'
MEDIA_KIND_AUDIO = 'audio'
# the <type> of /community/<actor>/submit/<type>: one compose form each
POST_FORM_TYPES = ('link', 'discussion', 'image', 'video', 'audio', 'poll', 'event')

# ActivityPub audience of a Post or PostReply, classified at ingest by
# app.activitypub.util.activitypub_visibility and enforced by app/visibility.py.
VISIBILITY_PUBLIC = 'public'
VISIBILITY_UNLISTED = 'unlisted'
VISIBILITY_FOLLOWERS = 'followers'
VISIBILITY_DIRECT = 'direct'
VISIBILITIES = (VISIBILITY_PUBLIC, VISIBILITY_UNLISTED, VISIBILITY_FOLLOWERS, VISIBILITY_DIRECT)

# Community.kind (spec 2026-10-14 §3.1). Only video_channel is read so far: its Group names the owner Person (§6.2).
COMMUNITY_KIND_DISCUSSION = 'discussion'
COMMUNITY_KIND_MICROBLOGS = 'microblogs'
COMMUNITY_KIND_VIDEO_CHANNEL = 'video_channel'
COMMUNITY_KIND_PODCAST = 'podcast'
COMMUNITY_KINDS_OFFERED = (COMMUNITY_KIND_DISCUSSION, COMMUNITY_KIND_VIDEO_CHANNEL, COMMUNITY_KIND_PODCAST)
# Apple Podcasts' top-level categories (spec §3.3, §10 A17.5); subcategories are not offered
APPLE_PODCAST_CATEGORIES = ('Arts', 'Business', 'Comedy', 'Education', 'Fiction', 'Government', 'Health & Fitness',
                            'History', 'Kids & Family', 'Leisure', 'Music', 'News', 'Religion & Spirituality',
                            'Science', 'Society & Culture', 'Sports', 'Technology', 'True Crime', 'TV & Film')

DATETIME_MS_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"

# Community subscription levels
SUBSCRIPTION_OWNER = 3
SUBSCRIPTION_MODERATOR = 2
SUBSCRIPTION_MEMBER = 1
SUBSCRIPTION_NONMEMBER = 0
SUBSCRIPTION_PENDING = -1
SUBSCRIPTION_BANNED = -2

THREAD_CUTOFF_DEPTH = 5

REPORT_STATE_NEW = 0
REPORT_STATE_ESCALATED = 1
REPORT_STATE_APPEALED = 2
REPORT_STATE_RESOLVED = 3
REPORT_STATE_DISCARDED = -1

REPORT_TYPE_USER = 0
REPORT_TYPE_POST = 1
REPORT_TYPE_REPLY = 2
REPORT_TYPE_COMMUNITY = 3
REPORT_TYPE_MESSAGE = 4

# different types of content notification that people can have. e.g. when a new post is made by a user or in a community.
# see NotificationSubscription in models.py

# -- user level ---
NOTIF_USER = 0  # if I am following userA I get notified if that user posts something
NOTIF_COMMUNITY = 1  # if I elect to get notified about new posts in a community
NOTIF_TOPIC = 2  # if I elect to get notified about new posts in communities in a topic
NOTIF_POST = 3  # this is new top-level comments on a post I am subscribed to
# I am auto subscribed to my posts, but I can elect to sub to other posts
NOTIF_REPLY = 4  # replies to a comment I made, or one I subscribed to
NOTIF_FEED = 5  # not actually used anywhere yet, but will be the same as NOTIF_TOPIC
# but for communities in a feed
NOTIF_MENTION = 6  # I have been mentioned in a post or comment
NOTIF_MESSAGE = 7  # I have a new chat message
NOTIF_BAN = 8  # I have been banned from a community
NOTIF_UNBAN = 9  # I have been un-banned from a community
NOTIF_NEW_MOD = 10  # I have been made a moderator for a community
NOTIF_REMINDER = 11  # A reminder that the user set before
NOTIF_ANSWER = 12  # An answer was accepted as the preferred one
NOTIF_FOLLOW = 13  # I have a new follower
NOTIF_FOLLOW_REQUEST = 14  # I have a new followe request

# --- mod/admin level ---
NOTIF_REPORT = 20  # a user, post, comment, or community have been reported

# --- admin level ---
NOTIF_REPORT_ESCALATION = 40  # a USER, POST, or COMMENT report has been escalated from mods to admins
NOTIF_REGISTRATION = 41  # a new registration / sign up has been generated

# --model/db default--
NOTIF_DEFAULT = 999  # default entry

ROLE_STAFF = 3
ROLE_ADMIN = 4
# D965: is_admin()/is_staff() match these role NAMES, so they are spelled once here
ROLE_STAFF_NAME = 'Staff'
ROLE_ADMIN_NAME = 'Admin'

DOWNVOTE_ACCEPT_NONE = -1
DOWNVOTE_ACCEPT_ALL = 0
DOWNVOTE_ACCEPT_MEMBERS = 2
DOWNVOTE_ACCEPT_INSTANCE = 4
DOWNVOTE_ACCEPT_TRUSTED = 6

MICROBLOG_APPS = ["mastodon", "misskey", "akkoma", "iceshrimp", "pleroma", "fedibird"]

SRC_WEB = 1
SRC_PUB = 2
SRC_API = 3
SRC_PLD = 4  # admin preload form to seed communities
SRC_PLG = 5  # plugins

APLOG_IN = True

APLOG_MONITOR = (True, 'Debug this')

APLOG_SUCCESS = (True, 'success')
APLOG_FAILURE = (True, 'failure')
APLOG_IGNORED = (True, 'ignored')
APLOG_PROCESSING = (True, 'processing')

APLOG_NOTYPE = (True, 'Unknown')
APLOG_DUPLICATE = (True, 'Duplicate')
APLOG_FOLLOW = (True, 'Follow')
APLOG_ACCEPT = (True, 'Accept')
APLOG_REJECT = (True, 'Reject')
APLOG_DELETE = (True, 'Delete')
APLOG_CHATMESSAGE = (True, 'Create ChatMessage')
APLOG_CREATE = (True, 'Create')
APLOG_UPDATE = (True, 'Update')
APLOG_LIKE = (True, 'Like')
APLOG_DISLIKE = (True, 'Dislike')
APLOG_RATE = (True, 'Rate')
APLOG_QA = (True, 'QA')
APLOG_REPORT = (True, 'Report')
APLOG_USERBAN = (True, 'User Ban')
APLOG_USERBLOCK = (True, 'User Block')
APLOG_LOCK = (True, 'Post Lock')
APLOG_QUOTEBOOST = (True, 'Quote Boost')

APLOG_UNDO_FOLLOW = (True, 'Undo Follow')
APLOG_UNDO_DELETE = (True, 'Undo Delete')
APLOG_UNDO_VOTE = (True, 'Undo Vote')
APLOG_UNDO_USERBAN = (True, 'Undo User Ban')

APLOG_ADD = (True, 'Add')
APLOG_REMOVE = (True, 'Remove')
APLOG_MOVE = (True, 'Move')

APLOG_ANNOUNCE = (True, 'Announce')
APLOG_PT_VIEW = (True, 'PeerTube View')

# D54: the most objects one Announce of a list carries, inbound or in our own batches
ANNOUNCE_MAX_OBJECTS = 100

INVITE_APPLY = 1
INVITE_MEMBERS_ONLY = 2
INVITE_MODS_ONLY = 3
INVITE_OWNER_ONLY = 4

ALLOWLIST_WEAK = 0      # A weak allowlist only has the effect of stopping people from adding remote communities from instances outside the list.
ALLOWLIST_STRONG = 1    # Strong allowlists drop activities from instances not on the list.
ALLOWLIST_INTENSE = 2   # As above but also drops activities originally authored by non-allowlist instances and relayed on via allowed instances. See can_create_post() for usage.


# Every permission string this codebase checks, and therefore every row the
# role-permissions page must be able to offer.
#
# It is a CONSTANT and not a `SELECT DISTINCT permission FROM role_permission`,
# which is where admin_permissions used to get it. Deriving the vocabulary from
# the rows meant that unchecking every box for a permission deleted the last row
# naming it, so it vanished from the page and could never be granted again --
# a one-way door on a security control, measured as:
#
#     PROBE p1 before: ['approve registrations', 'change user roles', 'manage users']
#     PROBE p1 after unchecking "approve registrations": ['change user roles', 'manage users']
#     PROBE p1 offered on the page now: ['change user roles', 'manage users']
#
# A permission nobody currently holds is still a permission. Keep this in step
# with the strings passed to permission_required() and user_access();
# tests/test_role_permissions.py fails if it drifts.
ROLE_PERMISSIONS = (
    'administer all communities',
    'administer all users',
    'approve registrations',
    'ban users',
    'change instance settings',
    'change user roles',
    'edit cms pages',
    'manage users',
)

# The roles the permissions page edits: 3 = Staff, 4 = Admin. Roles 1 and 2
# (Anonymous, Authenticated) hold no permissions, and any role beyond these is
# not represented on the page -- which is why the page's DELETE is scoped to
# these ids rather than clearing the whole table.
EDITABLE_ROLE_IDS = (3, 4)

# ActivityPub media types (interop C1 item 5)
AP_MEDIA_TYPE = 'application/activity+json'
AS2_PROFILE = 'https://www.w3.org/ns/activitystreams'
AS2_LD_MEDIA_TYPE = f'application/ld+json; profile="{AS2_PROFILE}"'
AP_MEDIA_TYPES = (AP_MEDIA_TYPE, 'application/ld+json')   # compared with a mimetype, which carries no parameters
AP_FETCH_ACCEPT = f'{AP_MEDIA_TYPE}, {AS2_LD_MEDIA_TYPE}'   # every outgoing ActivityPub GET, signed or not

# D1045/D1046. Each entry of a user's import -- the settings import's lists,
# the handles given to "Add people" -- drives one outbound actor fetch
# (`find_actor_or_create` reaches `create_actor_from_remote`, `bulk_follow`
# calls `search_for_user`), so an unbounded list is an unbounded outbound fetch
# primitive, the same family as D993 and D1025. Measured: 50 entries, 50
# lookups. A real Lemmy export lists tens of communities, not thousands.
# Entries past the cap are dropped.
IMPORT_MAX_ENTRIES = 500

# The largest image, in pixels, any decode path accepts: an upload, an icon or banner, a remote thumbnail or a
# federated image. app/__init__.py makes Pillow refuse anything larger at the header, before decoding a pixel.
# 50 MP is above any phone camera's default output (48 MP and 50 MP sensors bin to 12 MP) and below the 89.5 MP at
# which Pillow starts even to warn. A 6 KB PNG can declare 7072x7072 and cost seconds of CPU and hundreds of MB.
IMAGE_PIXEL_CAP = 50_000_000

# The lower ceiling for an image a peer serves to be made into a link's thumbnail (app/utils.py
# url_to_thumbnail_file), which fetches whatever url a federated post names. Counted as width x height times the
# frame count, at the header, before a pixel is decoded. A 29-byte GIF can declare a 37.7 MP screen, within
# IMAGE_PIXEL_CAP, and Pillow decodes even a one-pixel frame onto a full-screen canvas: 1.6 s of CPU per fetch. At
# 16 MP (4000x4000) the slowest thumbnail measured takes 0.9 s. Local uploads keep IMAGE_PIXEL_CAP.
REMOTE_THUMBNAIL_PIXEL_CAP = 16_000_000

# The reporter name the automated reports (AI detection) store beside `reporter_id: 1`: it names no account, so a
# report page shows it as text.
AUTOMATED_REPORTER = 'automated'
