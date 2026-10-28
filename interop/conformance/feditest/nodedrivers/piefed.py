"""FediTest node driver for the interop stack's PieFed. Writes go through the alpha API (interop.clients.PieFed); reads
come from the database, because the alpha API shows neither follows nor who liked what. Anything not overridden here
PieFed cannot do through those, and FediverseNode records it as skipped: announce (the alpha API has no boost),
update and delete of notes, accept and reject of follow requests."""
import os
import time
from typing import Any

import httpx
import psycopg2
from feditest import nodedriver
from feditest.nodedrivers import Account, AccountManager, DefaultAccountManager, NodeConfiguration, NodeDriver, NonExistingAccount
from feditest.nodedrivers import APP_PAR, APP_VERSION_PAR, HOSTNAME_PAR
from feditest.protocols.fediverse import (ROLE_ACCOUNT_FIELD, ROLE_NON_EXISTING_ACCOUNT_FIELD, USERID_ACCOUNT_FIELD,
                                          USERID_NON_EXISTING_ACCOUNT_FIELD, FediverseAccount, FediverseNode,
                                          FediverseNonExistingAccount)
from feditest.testplan import TestPlanConstellationNode, TestPlanNodeAccountField, TestPlanNodeNonExistingAccountField

from interop.clients import PieFed
from interop.conformance.piefed_mapping import acct_to_userid, libpq_dsn, microblog_title, note_text, received_ap_id, without_mention

POLL_DEADLINE = 30    # seconds a read waits for federation to deliver what it asks about; the tests poll for 5
POLL_INTERVAL = 0.5

PASSWORD_ACCOUNT_FIELD = TestPlanNodeAccountField(
    'password',
    """Password of the PieFed account, for the alpha API login (required).""",
    lambda x: x if len(x) else None
)


class PieFedAccount(FediverseAccount):
    def __init__(self, role: str | None, userid: str, password: str):
        super().__init__(role, userid)
        self.password = password

    @staticmethod
    def create_from_account_info_in_testplan(account_info_in_testplan: dict[str, str | None], context_msg: str = ''):
        userid = USERID_ACCOUNT_FIELD.get_validate_from_or_raise(account_info_in_testplan, context_msg)
        role = ROLE_ACCOUNT_FIELD.get_validate_from(account_info_in_testplan, context_msg)
        password = PASSWORD_ACCOUNT_FIELD.get_validate_from_or_raise(account_info_in_testplan, context_msg)
        return PieFedAccount(role, userid, password)


class PieFedNode(FediverseNode):
    def __init__(self, rolename: str, config: NodeConfiguration, account_manager: AccountManager | None = None):
        super().__init__(rolename, config, account_manager)
        self._clients: dict[str, PieFed] = {}
        self._conn = None

    def _client(self, actor_acct_uri: str) -> PieFed:
        userid = acct_to_userid(actor_acct_uri, self.hostname)
        if userid not in self._clients:
            account = self._account_manager.get_account_by_match(lambda a: isinstance(a, PieFedAccount) and a.userid == userid)
            if account is None:
                raise ValueError(f'No account {userid} on {self}')
            self._clients[userid] = PieFed(userid, account.password)
        return self._clients[userid]

    def _rows(self, sql: str, *params: Any) -> list[tuple]:
        """One connection per node, autocommit so each poll sees what federation committed since the last; a dropped
        one is opened again."""
        if self._conn is None or self._conn.closed:
            self._conn = psycopg2.connect(libpq_dsn(os.environ['DATABASE_URL']))
            self._conn.autocommit = True
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()

    @staticmethod
    def _await(read, ready=bool):
        """read() until ready(answer) or POLL_DEADLINE: federation is asynchronous, and the tests' own polling is far shorter.
        The last answer is returned either way, so an absent object is still an answer."""
        deadline = time.monotonic() + POLL_DEADLINE
        answer = read()
        while not ready(answer) and time.monotonic() < deadline:
            time.sleep(POLL_INTERVAL)
            answer = read()
        return answer

    def _resolve(self, client: PieFed, query: str) -> dict:
        """Resolve a remote object. It answers 400 until the object has been delivered or fetched, so retry until POLL_DEADLINE."""
        deadline = time.monotonic() + POLL_DEADLINE
        while True:
            try:
                return client.resolve(query)
            except httpx.HTTPStatusError as e:
                if e.response.status_code != 400 or time.monotonic() >= deadline:
                    raise
                time.sleep(POLL_INTERVAL)

    def _user_id(self, acct_uri: str) -> int | None:
        """The user row of an account: a local one by name, a remote one by name and domain. Not by ap_id: that is the
        last segment of the actor URL, which is a numeric id on Mastodon."""
        user_name, _, host = acct_uri.removeprefix('acct:').rpartition('@')
        if host == self.hostname:
            rows = self._rows('SELECT id FROM "user" WHERE ap_id IS NULL AND lower(user_name) = lower(%s)', user_name)
        else:
            rows = self._rows('SELECT id FROM "user" WHERE lower(user_name) = lower(%s) AND lower(ap_domain) = lower(%s)', user_name, host)
        return rows[0][0] if rows else None

    def _acct_uri(self, user_name: str, ap_domain: str | None) -> str:
        return f'acct:{user_name}@{ap_domain or self.hostname}'

    # From FediverseNode

    def obtain_actor_acct_uri(self, rolename: str | None = None) -> str:
        return self._account_manager.obtain_account_by_role(rolename).actor_acct_uri

    def make_follow(self, actor_acct_uri: str, to_follow_actor_acct_uri: str) -> None:
        client = self._client(actor_acct_uri)
        client.follow_user(self._resolve(client, '@' + to_follow_actor_acct_uri.removeprefix('acct:'))['person']['person']['id'])

    def make_unfollow(self, actor_acct_uri: str, following_actor_acct_uri: str) -> None:
        client = self._client(actor_acct_uri)
        client.unfollow_user(self._resolve(client, '@' + following_actor_acct_uri.removeprefix('acct:'))['person']['person']['id'])

    def _follows(self, follower_acct_uri: str, leader_acct_uri: str) -> bool:
        """An accepted follow, whichever of the two is local: user_follower keeps the local user in local_user_id and is_inward says which way it points."""
        follower, leader = self._user_id(follower_acct_uri), self._user_id(leader_acct_uri)
        if follower is None or leader is None:
            return False
        return bool(self._rows('SELECT 1 FROM user_follower WHERE is_accepted AND ('
                               '(NOT is_inward AND local_user_id = %s AND remote_user_id = %s) OR '
                               '(is_inward AND local_user_id = %s AND remote_user_id = %s))', follower, leader, leader, follower))

    def actor_is_following_actor(self, actor_acct_uri: str, leader_actor_acct_uri: str) -> bool:
        return self._await(lambda: self._follows(actor_acct_uri, leader_actor_acct_uri))

    def actor_is_followed_by_actor(self, actor_acct_uri: str, follower_actor_acct_uri: str) -> bool:
        return self._await(lambda: self._follows(follower_actor_acct_uri, actor_acct_uri))

    def make_create_note(self, actor_acct_uri: str, content: str, deliver_to: list[str] | None = None) -> str:
        for to in deliver_to or []:   # a Mention in the text is the only way to address someone
            content += ' @' + to.removeprefix('acct:')
        client = self._client(actor_acct_uri)
        return client.post(microblog_title(content), content, client.microblogs_id())['post']['ap_id']

    def make_reply_note(self, actor_acct_uri: str, to_be_replied_to_object_uri: str, reply_content: str) -> str:
        client = self._client(actor_acct_uri)
        target = self._resolve(client, to_be_replied_to_object_uri)
        if 'comment' in target:
            comment = target['comment']['comment']
            reply = client.reply(comment['post_id'], reply_content, comment['id'])
        else:
            reply = client.reply(target['post']['post']['id'], reply_content)
        return reply['comment']['ap_id']

    def like_object(self, actor_acct_uri: str, object_uri: str) -> None:
        self._vote(actor_acct_uri, object_uri, 1)

    def unlike_object(self, actor_acct_uri: str, object_uri: str) -> None:
        self._vote(actor_acct_uri, object_uri, 0)

    def _vote(self, actor_acct_uri: str, object_uri: str, score: int) -> None:
        client = self._client(actor_acct_uri)
        target = self._resolve(client, object_uri)
        if 'comment' in target:
            client.like_comment(target['comment']['comment']['id'], score)
        else:
            client.like_post(target['post']['post']['id'], score)

    def _text(self, object_uri: str, addressee_url: str | None = None) -> str | None:
        rows = self._rows('SELECT ap_id, body_html FROM post WHERE ap_id = %s AND NOT deleted UNION ALL '
                          'SELECT ap_id, body_html FROM post_reply WHERE ap_id = %s AND NOT deleted', object_uri, object_uri)
        if received_ap_id(rows) is None:
            return None
        text = note_text(rows[0][1] or '', '')   # a stored body has no title paragraph
        return without_mention(text, addressee_url) if addressee_url else text

    def _text_for(self, actor_acct_uri: str, object_uri: str) -> str | None:
        """The text as `actor_acct_uri` reads it, without the Mention a sender driver appended to address that actor."""
        return self._text(object_uri, f'https://{self.hostname}/u/{acct_to_userid(actor_acct_uri, self.hostname)}')

    def actor_has_received_object(self, actor_acct_uri: str, object_uri: str) -> str | None:
        """The text of the object when PieFed holds it. Holding it is all PieFed records: it keeps no per-recipient inbox."""
        return self._await(lambda: self._text_for(actor_acct_uri, object_uri), lambda text: text is not None)

    def note_content(self, actor_acct_uri: str, note_uri: str) -> str | None:
        return self._text_for(actor_acct_uri, note_uri)

    def update_note(self, actor_acct_uri: str, note_uri: str, new_content: str) -> None:
        client = self._client(actor_acct_uri)
        client.edit(self._resolve(client, note_uri)['post']['post']['id'], title=microblog_title(new_content), body=new_content)

    def delete_object(self, actor_acct_uri: str, object_uri: str) -> None:
        client = self._client(actor_acct_uri)
        client.delete(self._resolve(client, object_uri)['post']['post']['id'])   # a note made by make_create_note is a post

    def object_author(self, actor_acct_uri: str, object_uri: str) -> str | None:
        rows = self._rows('SELECT u.user_name, u.ap_domain FROM post p JOIN "user" u ON u.id = p.user_id WHERE p.ap_id = %s UNION ALL '
                          'SELECT u.user_name, u.ap_domain FROM post_reply r JOIN "user" u ON u.id = r.user_id WHERE r.ap_id = %s',
                          object_uri, object_uri)
        return self._acct_uri(*rows[0]) if rows else None

    def direct_replies_to_object(self, actor_acct_uri: str, object_uri: str) -> list[str]:
        return self._await(lambda: self._replies(object_uri))

    def _replies(self, object_uri: str) -> list[str]:
        rows = self._rows('SELECT c.ap_id FROM post_reply c JOIN post p ON p.id = c.post_id WHERE p.ap_id = %s '
                          'AND c.parent_id IS NULL AND NOT c.deleted UNION ALL '
                          'SELECT c.ap_id FROM post_reply c JOIN post_reply r ON r.id = c.parent_id WHERE r.ap_id = %s AND NOT c.deleted',
                          object_uri, object_uri)
        return [row[0] for row in rows]

    def object_likers(self, actor_acct_uri: str, object_uri: str) -> list[str]:
        return self._await(lambda: self._likers(object_uri))

    def _likers(self, object_uri: str) -> list[str]:
        rows = self._rows('SELECT u.user_name, u.ap_domain FROM post_vote v JOIN post p ON p.id = v.post_id '
                          'JOIN "user" u ON u.id = v.user_id WHERE p.ap_id = %s AND v.effect > 0 UNION ALL '
                          'SELECT u.user_name, u.ap_domain FROM post_reply_vote v JOIN post_reply r ON r.id = v.post_reply_id '
                          'JOIN "user" u ON u.id = v.user_id WHERE r.ap_id = %s AND v.effect > 0', object_uri, object_uri)
        return [self._acct_uri(*row) for row in rows]

    # From WebFingerServer and ActivityPubNode

    def obtain_account_identifier(self, rolename: str | None = None) -> str:
        return self.obtain_actor_acct_uri(rolename)

    def obtain_non_existing_account_identifier(self, rolename: str | None = None) -> str:
        return self._account_manager.obtain_non_existing_account_by_role(rolename).actor_acct_uri

    def obtain_actor_document_uri(self, rolename: str | None = None) -> str:
        return f'https://{self.hostname}/u/{self._account_manager.obtain_account_by_role(rolename).userid}'


@nodedriver
class PieFedNodeDriver(NodeDriver):
    """A PieFed that already runs (the interop stack's), with accounts the testplan names."""

    @staticmethod
    def test_plan_node_parameters():
        return [APP_PAR, APP_VERSION_PAR, HOSTNAME_PAR]

    @staticmethod
    def test_plan_node_account_fields() -> list[TestPlanNodeAccountField]:
        return [ROLE_ACCOUNT_FIELD, USERID_ACCOUNT_FIELD, PASSWORD_ACCOUNT_FIELD]

    @staticmethod
    def test_plan_node_non_existing_account_fields() -> list[TestPlanNodeNonExistingAccountField]:
        return [ROLE_NON_EXISTING_ACCOUNT_FIELD, USERID_NON_EXISTING_ACCOUNT_FIELD]

    def create_configuration_account_manager(self, rolename: str, test_plan_node: TestPlanConstellationNode) -> tuple[NodeConfiguration, AccountManager | None]:
        config = NodeConfiguration(self, test_plan_node.parameter_or_raise(APP_PAR, {APP_PAR.name: 'PieFed'}),
                                   test_plan_node.parameter(APP_VERSION_PAR), test_plan_node.parameter_or_raise(HOSTNAME_PAR))
        accounts: list[Account] = [
            PieFedAccount.create_from_account_info_in_testplan(info, f'Constellation role "{rolename}", NodeDriver "{self}", Account {index}: ')
            for index, info in enumerate(test_plan_node.accounts or [])]
        non_existing: list[NonExistingAccount] = [
            FediverseNonExistingAccount.create_from_non_existing_account_info_in_testplan(
                info, f'Constellation role "{rolename}", NodeDriver "{self}", Non-existing account {index}: ')
            for index, info in enumerate(test_plan_node.non_existing_accounts or [])]
        return config, DefaultAccountManager(accounts, non_existing)

    def _provision_node(self, rolename: str, config: NodeConfiguration, account_manager: AccountManager | None) -> PieFedNode:
        return PieFedNode(rolename, config, account_manager)
