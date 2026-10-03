"""Helpers shared by the discovery tests (interop D24). Defined once; import them into each test file."""


def nobody_excluded(host):
    """An `exclude` callback that lets every host through."""
    return False
