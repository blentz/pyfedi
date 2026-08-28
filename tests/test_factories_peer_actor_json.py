"""Proof tests for peer_actor_json's `omit` parameter.

`omit` is how every actor_json_to_model test reaches the absent side of a
guard over a key the baseline supplies. It used to be implemented with
`document.pop(key, None)`, which meant a key that was not there was silently
skipped: a test written as `peer_actor_json(omit=('publickey',))` -- lower
`k` -- still received a document carrying its publicKey, drove production
code down the ordinary happy path, and passed under a name announcing it had
tested the missing-key branch. Nothing in the coverage number could show
that, because the line executed either way.

The tests below pin the strict behaviour that replaced it. They assert on
the returned document and on the raised exception, not on the factory's
internals.

Every valid `omit` key in the suite today is one the relevant baseline
actually supplies, so making this strict changed no existing call. Derived
with:

    grep -rn 'omit=' tests/ | grep -v 'def peer_actor_json' | grep -v factories.py
"""
import pytest

from tests.factories import peer_actor_json


class TestOmitRemovesTheKey:
    """The behaviour `omit` exists for, unchanged by the strictness above it.

    Production change that fails these: dropping the `del document[key]`, so
    the document keeps a key the caller asked to have removed and every
    missing-key test in the actor_json_to_model files starts exercising the
    present side of its guard instead.
    """

    def test_a_baseline_key_named_in_omit_is_gone_from_the_document(self):
        document = peer_actor_json(omit=('publicKey',))
        assert 'publicKey' not in document
        assert 'preferredUsername' in document, 'only the named key is removed'

    def test_a_key_supplied_through_fields_can_also_be_omitted(self):
        """`omit` is applied after `fields` is merged, so it operates on the
        finished document rather than on the per-type baseline. A test that
        wants a key present for one call and absent for the next does not
        have to know which of the two sources put it there.

        Production change that fails this: moving the omit loop above the
        `fields` merge, which would let `fields` resurrect an omitted key
        and would make this raise instead.
        """
        document = peer_actor_json(fields={'summary': 'hello'}, omit=('summary',))
        assert 'summary' not in document


class TestOmitRejectsAKeyTheDocumentDoesNotHave:
    """Production change that fails these: restoring `document.pop(key,
    None)`, under which every assertion below sees an ordinary document come
    back and no exception raised at all.
    """

    def test_a_misspelled_key_raises_key_error(self):
        with pytest.raises(KeyError):
            peer_actor_json(omit=('publickey',))

    def test_the_error_names_the_offending_key(self):
        """The message has to name the bad key: the whole failure this
        guards against is a typo, and an error that only said "unknown key"
        would leave the reader diffing the omit tuple against the baseline
        by eye."""
        with pytest.raises(KeyError) as excinfo:
            peer_actor_json(omit=('publickey',))
        assert 'publickey' in str(excinfo.value)

    def test_the_error_lists_the_keys_that_are_available(self):
        """Naming the valid keys is what makes the message actionable
        without opening tests/factories.py: the baseline differs per actor
        type, so 'which keys does a Person have?' is a real question at the
        moment the typo is discovered."""
        with pytest.raises(KeyError) as excinfo:
            peer_actor_json(omit=('publickey',))
        message = str(excinfo.value)
        assert 'preferredUsername' in message
        assert 'publicKey' in message

    def test_a_key_valid_for_another_actor_type_still_raises(self):
        """'outbox' is in the Group and Feed baselines but not the Person
        one. This is the realistic version of the typo -- a document shape
        copied from a sibling test file -- and it is exactly the case
        `pop(key, None)` hid.
        """
        assert 'outbox' in peer_actor_json('Group')
        with pytest.raises(KeyError):
            peer_actor_json('Person', omit=('outbox',))

    def test_one_bad_key_alongside_several_good_ones_still_raises(self):
        """A tuple of mostly-correct keys is how this reaches a real test:
        the good ones would be removed and the typo skipped, leaving the
        document in a state no test intended.
        """
        with pytest.raises(KeyError):
            peer_actor_json('Group', omit=('name', 'outbox', 'inbxo'))
