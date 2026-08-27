"""ensure_domains_match decides whether an activity's `id` and its actor come
from the same host. It is an impersonation defence: a peer claiming an id on
one domain while attributing the content to an actor on another is refused.

Every case below is a peer-supplied document. Malformed and hostile shapes are
the point of the function, not edge cases.
"""
import pytest

from app.activitypub.util import ensure_domains_match


class TestMatchingDomains:
    """Mutation that fails these: changing the `id_domain == actor_domain`
    comparison to `!=`, or deleting the `return True`."""

    def test_actor_on_the_same_host_matches(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/activities/1',
            'actor': 'https://peer.example/u/alice',
        }) is True

    def test_attributed_to_string_on_the_same_host_matches(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': 'https://peer.example/u/alice',
        }) is True

    def test_attributed_to_list_of_strings_takes_the_first(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': ['https://peer.example/u/alice', 'https://other.example/u/bob'],
        }) is True

    def test_attributed_to_list_of_dicts_takes_the_first_person(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': [{'type': 'Person', 'id': 'https://peer.example/u/alice'}],
        }) is True


class TestMismatchedDomains:
    """The refusals. Mutation that fails these: making the comparison always
    true, or returning True at the end instead of False."""

    def test_actor_on_a_different_host_is_refused(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/activities/1',
            'actor': 'https://attacker.example/u/mallory',
        }) is False

    def test_attributed_to_a_different_host_is_refused(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': 'https://attacker.example/u/mallory',
        }) is False


class TestMissingParts:
    """Mutation that fails these: deleting the `if note_id and note_actor:`
    guard, so the function compares None against None and returns True."""

    def test_no_id_is_refused(self, app):
        assert ensure_domains_match({'actor': 'https://peer.example/u/alice'}) is False

    def test_no_actor_and_no_attributed_to_is_refused(self, app):
        assert ensure_domains_match({'id': 'https://peer.example/activities/1'}) is False

    def test_an_empty_attributed_to_list_is_refused(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': [],
        }) is False

    def test_a_list_of_non_person_dicts_is_refused(self, app):
        """The loop breaks only on a Person dict or a string; a list of other
        dicts leaves note_actor None."""
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': [{'type': 'Service', 'id': 'https://peer.example/u/bot'}],
        }) is False

    def test_an_attributed_to_that_is_neither_a_string_nor_a_list_is_refused(self, app):
        """`attributedTo` present but shaped as something the function does
        not special-case (here, a bare dict rather than a string or a list)
        leaves note_actor None -- the isinstance checks at
        `isinstance(attributed_to, str)` / `isinstance(attributed_to, list)`
        are exhaustive over what the function extracts, not over every shape
        a peer could send. Closes the branch coverage gap left by the
        brief's own test set, which only exercised the list arm with a list
        value."""
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'attributedTo': {'type': 'Person', 'id': 'https://peer.example/u/alice'},
        }) is False


class TestActorTakesPrecedenceOverAttributedTo:
    """`actor` is checked first and `attributedTo` is only consulted when it is
    absent. Mutation that fails this: swapping the branch order."""

    def test_actor_wins_when_both_are_present(self, app):
        assert ensure_domains_match({
            'id': 'https://peer.example/notes/1',
            'actor': 'https://attacker.example/u/mallory',
            'attributedTo': 'https://peer.example/u/alice',
        }) is False


class TestSuspectedNetlocDefect:
    """The spec records that this function compares `urlparse(...).netloc`,
    not `.hostname`. `netloc` carries userinfo and port, and the tests below
    pin CURRENT behaviour for that -- they are NOT asserting that this is the
    intended or correct behaviour. Confirmed with a direct probe:

        https://peer.example/x                       netloc=peer.example
        https://peer.example@attacker.example/x       netloc=peer.example@attacker.example  hostname=attacker.example
        https://peer.example:8443/x                   netloc=peer.example:8443              hostname=peer.example

    `hostname` is derived purely from `netloc`'s own string (userinfo and
    port stripped, then lowercased), which makes it impossible for two
    inputs with an EQUAL `netloc` to ever have a DIFFERENT `hostname` -- the
    string that decided the match also decides the would-be-correct answer.
    So userinfo cannot make this function ACCEPT (return True for) two
    genuinely different real hosts; every construction tried here that
    reaches `True` has the same real host (by hostname) on both sides too.
    What userinfo actually does, demonstrated below, is let one side's
    netloc carry an embedded, unrelated-looking domain token (the userinfo)
    while the two sides still match on their real host -- current code does
    not strip or validate that token at all, it is just along for the ride.
    Reported as a suspected defect, not fixed here. See the call-site
    analysis in the task report for whether it is reachable by a peer.
    """

    def test_userinfo_is_carried_whole_into_a_still_matching_comparison(self, app):
        """id and actor are both truly hosted on attacker.example (that is
        `hostname` for both), but id's netloc also carries a userinfo token
        that reads as 'peer.example' -- an unrelated domain. Because actor's
        netloc carries the identical token, the two netloc STRINGS are still
        equal and the function accepts. This is not a false accept relative
        to hostname (a hostname-based check would accept this same pair
        too, since both real hosts are attacker.example) -- it pins that the
        userinfo token itself is never inspected or stripped, so 'contains a
        domain-shaped substring that is not the real host' is invisible to
        this function and to anything that later re-displays id_domain /
        actor_domain as 'the domain' for this activity."""
        assert ensure_domains_match({
            'id': 'https://peer.example@attacker.example/activities/1',
            'actor': 'https://peer.example@attacker.example/u/mallory',
        }) is True

    def test_userinfo_on_only_one_side_is_still_correctly_refused(self, app):
        """id's netloc is 'peer.example@attacker.example' (real host
        attacker.example); actor's netloc is the bare host 'peer.example'.
        The real hosts differ (attacker.example vs peer.example), and a
        hostname-based comparison would refuse this pair for that reason.
        netloc comparison also refuses it, but only because the strings
        differ -- for the right real-world reason, reached by an accident of
        string inequality rather than by inspecting either host. Kept
        alongside the case above as the other half of the same probe: this
        function's few observable outcomes for userinfo-bearing input never
        actually diverge from what a hostname-based check would decide."""
        assert ensure_domains_match({
            'id': 'https://peer.example@attacker.example/activities/1',
            'actor': 'https://peer.example/u/alice',
        }) is False

    def test_a_port_makes_the_same_host_compare_unequal(self, app):
        """id's netloc is 'peer.example:8443' and actor's netloc is the bare
        host 'peer.example'. hostname-based comparison would see the SAME
        host and accept; netloc-based comparison sees different strings and
        refuses -- a legitimate peer serving its inbox on a non-default port
        would be rejected by this function today. This is the one case in
        this class where netloc and hostname genuinely disagree on the
        boolean outcome."""
        assert ensure_domains_match({
            'id': 'https://peer.example:8443/activities/1',
            'actor': 'https://peer.example/u/alice',
        }) is False
