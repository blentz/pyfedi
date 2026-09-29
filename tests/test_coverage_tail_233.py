"""Round 233: the last four lines of `app/main/routes.py`.

Three clusters, each small and each about a caller-supplied value:

    the modlog's name filters   `someone@this.instance` must mean the local account
                                `someone`, and a name that is not a local account must
                                still be tried as a REMOTE one
    /manifest.json              the PWA manifest is chosen by parsing the User-Agent, and
                                an iPhone's `mac os x` has its own directory
    /r/randomnsfw               two spellings of one query, the second excluding the
                                instances the viewer has blocked

The modlog rows are the ones that matter. `tests/test_main_modlog.py` already covers
filtering by a moderator and by a suspect, and its `test_a_name_written_with_this_instances_domain`
builds the handle from `Site.name` rather than from `SERVER_NAME` -- which is not the value
the route compares against, so the domain-stripping branch it is named for had never run.
"""
from types import SimpleNamespace

import pytest
from flask import current_app, g

from app import db
from app.models import Community, Instance, InstanceBlock, ModLog, Site
from tests.factories import make_community, make_instance, make_user


@pytest.fixture
def env(app, api_baseline):
    g.admin_ids = []
    site = db.session.get(Site, 1)
    site.private_instance = False
    g.site = site
    return SimpleNamespace(app=app, site=site, baseline=api_baseline,
                           client=app.test_client())


def signed_in_client(app, user):
    client = app.test_client()
    with client.session_transaction() as session:
        session['_user_id'] = str(user.id)
        session['_fresh'] = True
    return client


# --------------------------------------------------------------------------
# The modlog's two name filters
# --------------------------------------------------------------------------


class TestNamingSomebodyInTheModlog:
    """Both filters accept a name in either shape -- `someone` or
    `someone@this.instance` -- and fall back to an `ap_id` lookup for a remote account.
    The entries are counted by their `reason`, which appears only in an entry.
    """

    @pytest.fixture
    def seeded(self, env):
        local_mod = make_user(env.baseline.instance_local, 'localmod', local=True)
        peer = make_instance('peer.example')
        remote_suspect = make_user(peer, 'wanderer')
        remote_suspect.ap_id = 'wanderer@peer.example'
        local_suspect = make_user(env.baseline.instance_local, 'localrascal', local=True)
        community = make_community('modlogland')
        db.session.commit()
        db.session.add(ModLog(user_id=local_mod.id, target_user_id=local_suspect.id,
                              community_id=community.id, type='mod',
                              action='delete_post', reason='localspam', public=True,
                              link='post/1', link_text='a post'))
        db.session.add(ModLog(user_id=env.baseline.user1.id,
                              target_user_id=remote_suspect.id,
                              community_id=community.id, type='mod',
                              action='ban_user', reason='remoterudeness', public=True,
                              link='u/1', link_text='someone'))
        db.session.commit()
        env.local_mod = local_mod
        env.local_suspect = local_suspect
        env.remote_suspect = remote_suspect
        return env

    def test_a_suspect_named_with_this_instances_domain_is_the_local_account(self,
                                                                            seeded):
        """The `@SERVER_NAME` branch of the SUSPECT filter. A person copying a handle out
        of a post gets `localrascal@test.piefed.local`, and the account is stored under the
        bare name, so without the split the filter matches nobody and the whole log is
        returned -- which reads as 'this moderator did all of this'."""
        server = current_app.config['SERVER_NAME']

        response = seeded.client.get(
            f'/modlog?suspect_user_name=localrascal@{server}')

        body = response.get_data(as_text=True)
        assert response.status_code == 200
        assert 'localspam' in body
        assert 'remoterudeness' not in body

    def test_a_remote_suspect_is_found_by_their_ap_id(self, seeded):
        """The fallback. The first lookup requires `ap_id is NULL`, so a remote account is
        never its result; the second matches `ap_id` exactly, which is the `name@host`
        string."""
        response = seeded.client.get(
            '/modlog?suspect_user_name=wanderer@peer.example')

        body = response.get_data(as_text=True)
        assert 'remoterudeness' in body
        assert 'localspam' not in body

    def test_a_moderator_named_with_this_instances_domain_is_the_local_account(self,
                                                                              seeded):
        """The same branch in the MODERATOR filter, which is a separate copy of the block
        -- and one that has already been wrong once: it used to search for
        `suspect_user_name` here, so filtering by a local moderator returned the whole
        unfiltered log."""
        server = current_app.config['SERVER_NAME']

        response = seeded.client.get(f'/modlog?user_name=localmod@{server}')

        body = response.get_data(as_text=True)
        assert 'localspam' in body
        assert 'remoterudeness' not in body

    def test_a_name_nobody_holds_narrows_nothing(self, seeded):
        """`if user:` -- an unmatched name leaves the query unfiltered rather than
        answering an empty log. Recorded rather than changed: it is the existing behaviour
        of both blocks, and it is what makes the two rows above meaningful, since a filter
        that matched nobody would otherwise be indistinguishable from one that worked."""
        response = seeded.client.get('/modlog?suspect_user_name=nosuchperson')

        body = response.get_data(as_text=True)
        assert 'localspam' in body
        assert 'remoterudeness' in body


# --------------------------------------------------------------------------
# /manifest.json
# --------------------------------------------------------------------------


class TestThePwaManifest:
    """The manifest is chosen by parsing the User-Agent. `app/static/pwa_manifests` holds
    `android/`, `ios/` and `default/`, and the path is built from the parsed OS family --
    with one hand-written mapping.

    That mapping is for the DESKTOP. `ua_parser` reports `ios` for an iPhone and an iPad,
    which already names a directory, and `mac os x` for Safari on a Mac, which names none
    -- so without `if os_family == 'mac os x'` a Mac would fall back to `default`. Writing
    the rows the other way round, on the assumption that the mapping was for iPhones, left
    a mutant alive: deleting the branch changed nothing for an iPhone.
    """

    IPHONE = ('Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) '
              'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 '
              'Safari/604.1')
    MAC = ('Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 '
           '(KHTML, like Gecko) Version/17.0 Safari/605.1.15')
    ANDROID = ('Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 '
               '(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36')

    def _manifest(self, env, user_agent):
        response = env.client.get('/manifest.json',
                                  headers={'User-Agent': user_agent})

        assert response.status_code == 200
        return response.get_json()

    def test_a_mac_is_served_the_ios_manifest(self, env):
        """The mapping itself. Each file carries `piefed_manifest_version` naming itself,
        which is what distinguishes them: `default` and `ios` agree on `display`, so an
        assertion about that field would pass against the fallback."""
        served = self._manifest(env, self.MAC)

        assert served['piefed_manifest_version'] == 'ios'

    def test_an_iphone_is_served_the_ios_manifest_without_the_mapping(self, env):
        """An iPhone parses as `ios`, which names the directory directly -- so it takes the
        `else` and reaches the same file by the other route."""
        served = self._manifest(env, self.IPHONE)

        assert served['piefed_manifest_version'] == 'ios'

    def test_an_android_phone_is_served_the_android_manifest(self, env):
        """The `else`, which builds the path from the parsed family directly."""
        served = self._manifest(env, self.ANDROID)

        assert served['piefed_manifest_version'] == 'android'

    def test_an_unknown_platform_falls_back_to_the_default(self, env):
        """`return path if os.path.exists(path) else .../default/...`. There is no
        `windows/` directory, so a desktop browser exercises the fallback rather than a
        404 or a crash."""
        served = self._manifest(env, 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)')

        assert served['piefed_manifest_version'] == 'default'

    def test_the_site_name_is_written_into_whichever_file_was_opened(self, env):
        """What the route is for. The manifest on disk is a template; the instance's own
        name and description replace its fields on every request."""
        env.site.name = 'A Named Instance'
        env.site.description = 'with a description'
        db.session.commit()

        served = self._manifest(env, self.MAC)

        assert served['piefed_manifest_version'] == 'ios'
        assert served['name'] == 'A Named Instance'
        assert served['description'] == 'with a description'


# --------------------------------------------------------------------------
# /r/randomnsfw
# --------------------------------------------------------------------------


class TestTheRandomNsfwCommunity:
    """Two spellings of one query: the second adds `i.id not in :blocked_instances`. A
    viewer with no blocks takes the shorter one, which is why both need a row -- the
    blocked-instance arm is the one that can silently stop excluding anything.
    """

    @pytest.fixture
    def seeded(self, env):
        peer = make_instance('nsfwpeer.example')
        community = make_community('adultsonly', host='nsfwpeer.example')
        community.instance_id = peer.id
        community.nsfw = True
        community.post_count = 5
        community.private = False
        db.session.commit()
        env.peer = peer
        env.community = community
        return env

    def test_an_anonymous_viewer_is_sent_to_an_nsfw_community(self, seeded):
        """The unblocked arm. `blocked_or_banned_instances(0)` is `[]` for a viewer with no
        account, so the walrus is falsy and the query with no parameters runs."""
        response = seeded.client.get('/r/randomnsfw')

        assert response.status_code == 302
        assert 'adultsonly' in response.headers['Location']

    def test_a_viewer_who_has_blocked_the_instance_is_told_there_are_none(self, seeded):
        """The blocked arm, and the only assertion that can tell it apart: with the one
        NSFW community's instance blocked there is nothing left to redirect to, so the
        answer is the 'No communities found' page rather than a redirect."""
        reader = make_user(seeded.baseline.instance_local, 'blocker', local=True)
        reader.verified = True
        db.session.add(InstanceBlock(user_id=reader.id, instance_id=seeded.peer.id))
        db.session.commit()

        response = signed_in_client(seeded.app, reader).get('/r/randomnsfw')

        assert response.status_code == 200
        assert 'No communities found' in response.get_data(as_text=True)

    def test_a_viewer_who_has_blocked_something_else_still_gets_one(self, seeded):
        """The control for the row above. A non-empty block list must take the longer query
        AND still find the community, or the two rows together would be satisfied by an arm
        that returns nothing whenever anything is blocked."""
        other = make_instance('irrelevant.example')
        reader = make_user(seeded.baseline.instance_local, 'otherblocker', local=True)
        reader.verified = True
        db.session.add(InstanceBlock(user_id=reader.id, instance_id=other.id))
        db.session.commit()

        response = signed_in_client(seeded.app, reader).get('/r/randomnsfw')

        assert response.status_code == 302
        assert 'adultsonly' in response.headers['Location']

    def test_a_private_community_is_never_the_random_one(self, seeded):
        """`c.private is false` in both spellings. A random-community link that could land
        on an invite-only community would disclose its existence to anybody who kept
        clicking."""
        seeded.community.private = True
        db.session.commit()

        response = seeded.client.get('/r/randomnsfw')

        assert response.status_code == 200
        assert 'No communities found' in response.get_data(as_text=True)
