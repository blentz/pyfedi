"""Every email the software sends, as code (spec 2026-11-24-admin-messaging D1, D2): when each is sent, to whom, and,
for the ones an admin may reword (MS1), the placeholders their text may use."""
from typing import NamedTuple

from flask_babel import lazy_gettext as _l


class Placeholder(NamedTuple):
    name: str
    description: str
    sample: str
    kind: str = 'text'   # text; url (a link where it stands alone); list (rendered by code, D8)
    required: bool = False


class BuiltIn(NamedTuple):
    key: str
    name: str
    when: str           # when it is sent, in the admin's words
    recipients: str     # who gets it
    templates: tuple    # its bundled templates under app/templates/email/, without extension
    placeholders: tuple = ()   # empty: not editable
    unsubscribe: str | None = None   # the footer its default carries: 'newsletter', 'notifications' or None (D9)

    @property
    def editable(self) -> bool:
        return bool(self.placeholders)

    def placeholder(self, name: str) -> Placeholder | None:
        return next((p for p in self.placeholders if p.name == name), None)


_NAME = Placeholder('name', _l("The member's display name"), 'Alex')
_SITE_NAME = Placeholder('site_name', _l("The name of the member's domain"), 'Example')
_SITE_URL = Placeholder('site_url', _l("The address of the member's domain"), 'https://piefed.example', 'url')

CATALOGUE = (
    BuiltIn('welcome', _l('Welcome'),
            _l('On sites that take registration applications: when an application is approved, and when an '
               'applicant verifies their email address.'),
            _l('The new member'), ('welcome',),
            (_NAME, _SITE_NAME, _SITE_URL), unsubscribe='newsletter'),
    BuiltIn('verify_email', _l('Verify email address'),
            _l('When someone registers, changes their email address or asks for the link again, and the site '
               'checks email addresses.'),
            _l('The address being checked'), ('verification',),
            (_NAME, _SITE_NAME,
             Placeholder('verify_url', _l('The link that verifies the address'),
                         'https://piefed.example/auth/verify_email/abc123', 'url', True))),
    BuiltIn('password_reset', _l('Reset password'),
            _l('When someone asks to reset their password.'),
            _l('The account'), ('reset_password',),
            (_NAME, _SITE_NAME,
             Placeholder('reset_url', _l('The link that sets a new password'),
                         'https://piefed.example/auth/reset_password/abc123', 'url', True))),
    BuiltIn('subscription_expiring', _l('Subscription expiring soon'),
            _l('Once a day, from 10 days before a subscription paid for in advance runs out, until it runs out or '
               'is renewed. Not for subscriptions that renew by themselves, or ones that were cancelled.'),
            _l('The subscriber'), ('prepaid_reminder',),
            (_NAME, _SITE_NAME,
             Placeholder('plan', _l('The name of the plan'), 'Supporter'),
             Placeholder('expiry_date', _l('The day it runs out (UTC)'), '2026-12-01'),
             Placeholder('days_left', _l('Whole days left'), '10'),
             Placeholder('renew_url', _l('The page where it is renewed'), 'https://piefed.example/subscribe', 'url'))),
    BuiltIn('unread_notifications', _l('Unread notifications'),
            _l('By the email notifications job, to members who have unread notifications since their last visit '
               'and have not turned these emails off. At most once between visits.'),
            _l('The member'), ('unread_notifications',),
            (_NAME, _SITE_NAME,
             Placeholder('notifications', _l('The list of unread notifications'),
                         '- Somebody replied to your post', 'list', True),
             Placeholder('recent_posts', _l('A few recent posts in their communities (may be empty)'),
                         '- A recent post', 'list'),
             Placeholder('notifications_url', _l('The notifications page'),
                         'https://piefed.example/notifications', 'url')),
            unsubscribe='notifications'),
    BuiltIn('community_invite', _l('Community invitation'),
            _l('When a member invites someone to a community by email address.'),
            _l('The email address they entered'), ('invite_to_community',),
            (Placeholder('inviter', _l('The display name of the member who invited them'), 'Alex'),
             Placeholder('community', _l("The community's name"), 'Gardening'),
             Placeholder('register_url', _l('Where to create an account'), 'https://piefed.example/auth/register',
                         'url'),
             Placeholder('join_url', _l('Where to join the community'),
                         'https://piefed.example/c/gardening/subscribe', 'url', True),
             _SITE_NAME)),
    BuiltIn('topic_suggestion', _l('Topic suggestion'),
            _l('When a member suggests communities for a topic.'),
            _l("The site's contact address"), ('suggested_topic',)),
    BuiltIn('community_move_request', _l('Request to move a community'),
            _l('When a member asks for a community to be moved here from another instance.'),
            _l("The instance's contact address"), ('move_community',)),
    BuiltIn('close_account', _l('Account closed'),
            _l('Not sent at present: kept with the bundled templates.'),
            _l('The member'), ('close_account',)),
    BuiltIn('test_email', _l('Test email'),
            _l('When an admin opens the test email page, in debug mode only.'),
            _l('The admin, or the address given'), ()),
    BuiltIn('admin_message', _l('Messages from the admins'),
            _l('When an admin sends one, at once or at the time they chose (below).'),
            _l('All members who take the newsletter, or those chosen; a service notice reaches those who do not '
               'take it too.'), ('newsletter',), unsubscribe='newsletter'),
)

BY_KEY = {entry.key: entry for entry in CATALOGUE}

# The placeholders an admin message may use (D10).
MESSAGE_PLACEHOLDERS = (_NAME, _SITE_NAME, _SITE_URL)
