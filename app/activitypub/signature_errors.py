"""The signature exceptions, in a module of their own so signature.py and signature_rfc9421.py can both raise them
without importing each other at load time. signature.py re-exports both names."""


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
