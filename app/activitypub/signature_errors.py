"""The signature exceptions, in a module of their own so signature.py and signature_rfc9421.py can both raise them
without importing each other at load time. signature.py re-exports VerificationError and VerificationFormatError."""


class VerificationError(BaseException):
    """
    There was an error with verifying the signature
    """

    pass


class VerificationFormatError(VerificationError):
    """
    There was an error with the format of the signature (not if it is valid)
    """

    pass


class KeyRefetchable:
    """Marks a verification failure that a different key could cure -- the signature does not verify under the key
    tried, or that key is unusable -- so verify_refreshing_key refetches the signer's key for it, and for nothing
    else: a new key cannot fix a request for another host, an unsigned required header or a malformed signature."""


class KeyMismatchError(KeyRefetchable, VerificationError):
    """The signature does not verify under this key."""


class UnusableKeyError(KeyRefetchable, VerificationFormatError):
    """The key to verify with is not a usable public key, or not one of a type the signature's algorithm takes."""
