"""`app/cli.py`'s `find_cross_host_actors`, the read-only audit for a `User`,
`Community` or `Feed` row whose `ap_profile_id` host disagrees with the row's
own `ap_domain`.

Task 3 closed the gate in `actor_json_to_model` that let a cooperating peer
mint a row like that. Closing the gate stops new ones; this function is how
an existing database is checked for rows that predate it. It only queries and
returns a list -- no `UPDATE`, `DELETE`, or migration.

Three things this file has to prove, per the task brief:

1. A deliberately mismatched row IS reported.
2. The database is unchanged by running the audit (row counts before and
   after are equal) -- that is what "read-only" means, observably.
3. A CONSISTENT row is NOT reported. A query that reports everything is not a
   finding, it is noise, and would pass a test that only checked (1).

`TestCaseFoldedDomainIsNotReported` is the sharpest version of (3): it pins a
real defect this audit found in `actor_json_to_model` (see that test's
docstring) that would otherwise make the audit produce a false positive on an
entirely legitimate row.

`TestNullColumnsAreNotReported` covers the other way a consistent-looking row
must be left alone: a local row (never fetched from a remote peer) carries
`ap_profile_id` and `ap_domain` both `NULL`, and `create_new_user` in
`app/auth/util.py` is the proof that really happens. A `NULL` cannot disagree
with anything.
"""

from app.cli import find_cross_host_actors
from app.models import Community, Feed, User
from tests.factories import make_instance

import pytest


@pytest.fixture
def instance(app, db_session):
    return make_instance('test.piefed.local', software='piefed')


def _counts(session):
    return (
        session.query(User).count(),
        session.query(Community).count(),
        session.query(Feed).count(),
    )


class TestMismatchedRowsAreReported:
    """One deliberately mismatched row per table, each on its own consistent
    background row so a query that reports everything would still fail the
    "consistent row not reported" half of each test.
    """

    def test_a_mismatched_user_is_reported(self, app, db_session, instance):
        consistent = User(user_name='alice', email='alice@example.com', instance_id=instance.id,
                          verified=True, ap_id='alice@good.example',
                          ap_profile_id='https://good.example/u/alice', ap_domain='good.example')
        mismatched = User(user_name='mallory', email='mallory@example.com', instance_id=instance.id,
                          verified=True, ap_id='mallory@evil.example',
                          ap_profile_id='https://evil.example/u/mallory', ap_domain='good.example')
        db_session.add_all([consistent, mismatched])
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('User', mismatched.id) in reported
        assert ('User', consistent.id) not in reported

    def test_a_mismatched_community_is_reported(self, app, db_session, instance):
        consistent = Community(name='ontopic', title='ontopic', instance_id=instance.id,
                               ap_profile_id='https://good.example/c/ontopic', ap_domain='good.example')
        mismatched = Community(name='offtopic', title='offtopic', instance_id=instance.id,
                               ap_profile_id='https://evil.example/c/offtopic', ap_domain='good.example')
        db_session.add_all([consistent, mismatched])
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('Community', mismatched.id) in reported
        assert ('Community', consistent.id) not in reported

    def test_a_mismatched_feed_is_reported(self, app, db_session, instance):
        consistent = Feed(name='goodfeed', title='goodfeed', instance_id=instance.id,
                          ap_profile_id='https://good.example/f/goodfeed', ap_domain='good.example')
        mismatched = Feed(name='evilfeed', title='evilfeed', instance_id=instance.id,
                          ap_profile_id='https://evil.example/f/evilfeed', ap_domain='good.example')
        db_session.add_all([consistent, mismatched])
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('Feed', mismatched.id) in reported
        assert ('Feed', consistent.id) not in reported


class TestNullColumnsAreNotReported:
    """`ap_profile_id` and `ap_domain` are both nullable, and a local row --
    one that has never been fetched from a remote peer -- carries neither.
    `create_new_user` (app/auth/util.py) builds a User with no `ap_profile_id`
    and no `ap_domain` at all; the local-community path in
    app/community/routes.py sets both to the same local server instead, so a
    User is the row that actually exercises the both-NULL case in production.

    A NULL cannot disagree with anything. Reporting one would be a false
    positive -- worse, it is exactly the failure mode the brief calls out by
    name: a local user reported as a cross-host smuggling suspect would make
    the whole report useless.
    """

    def test_a_local_user_with_both_columns_null_is_not_reported(self, app, db_session, instance):
        local = User(user_name='localonly', email='localonly@example.com', instance_id=instance.id,
                    verified=True)
        db_session.add(local)
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('User', local.id) not in reported

    def test_a_row_with_only_ap_domain_null_is_not_reported(self, app, db_session, instance):
        """ap_profile_id set, ap_domain left NULL -- the asymmetric half of
        the NULL guard, distinct from the both-NULL local case above.
        """
        half_null = Community(name='halfnull', title='halfnull', instance_id=instance.id,
                              ap_profile_id='https://good.example/c/halfnull', ap_domain=None)
        db_session.add(half_null)
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('Community', half_null.id) not in reported

    def test_a_row_with_only_ap_profile_id_null_is_not_reported(self, app, db_session, instance):
        half_null = Feed(name='onlydomain', title='onlydomain', instance_id=instance.id,
                         ap_profile_id=None, ap_domain='good.example')
        db_session.add(half_null)
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('Feed', half_null.id) not in reported


class TestCaseFoldedDomainIsNotReported:
    """A real, pre-existing defect in `actor_json_to_model`, found while
    building this audit and reported rather than fixed (out of scope for this
    task): the `User` branch stores `ap_domain=server` -- unlowered -- while
    the `Community` and `Feed` branches both store `ap_domain=server.lower()`.
    `server` itself is not guaranteed lowercase: on the ordinary `https://`
    fetch path, `extract_domain_and_actor` returns `urlparse(...).netloc`
    verbatim, without lowercasing it, while `ap_profile_id` is always stored
    lowercased (`activity_json['id'].lower()`, all three branches).

    A `User` row created through that path can therefore have an `ap_domain`
    that differs from its `ap_profile_id`'s host only in case -- e.g.
    `ap_profile_id='https://good.example/u/carol'` next to
    `ap_domain='Good.Example'`. That is a same-host row, not a cross-host one,
    and reporting it would be a false positive of exactly the kind the brief
    warns about ("a query that reports everything is not a finding, it is
    noise"). `find_cross_host_actors` lowercases `ap_domain` before comparing
    for this reason; this test is what proves that guard actually holds.
    """

    def test_a_domain_differing_only_in_case_is_not_reported(self, app, db_session, instance):
        row = User(user_name='carol', email='carol@example.com', instance_id=instance.id,
                  verified=True, ap_id='carol@good.example',
                  ap_profile_id='https://good.example/u/carol', ap_domain='Good.Example')
        db_session.add(row)
        db_session.commit()
        before = _counts(db_session)

        mismatches = find_cross_host_actors(db_session)

        assert _counts(db_session) == before
        reported = {(name, row_id) for name, row_id, _, _ in mismatches}
        assert ('User', row.id) not in reported


class TestReportedTupleShape:
    """The exact (model_name, row_id, ap_profile_id, ap_domain) tuple a caller
    (the `audit-cross-host-actors` CLI command, or a future consumer) can rely
    on -- covered once here rather than repeated in every table's test above.
    """

    def test_the_mismatch_tuple_carries_the_original_column_values(self, app, db_session, instance):
        mismatched = Community(name='offtopic', title='offtopic', instance_id=instance.id,
                               ap_profile_id='https://evil.example/c/offtopic', ap_domain='good.example')
        db_session.add(mismatched)
        db_session.commit()

        mismatches = find_cross_host_actors(db_session)

        assert (
            'Community', mismatched.id, 'https://evil.example/c/offtopic', 'good.example'
        ) in mismatches
