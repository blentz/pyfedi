"""A code as it is stored and looked up: stripped and lower case, so it can be typed as it was read out."""


def normalise(text: str | None) -> str | None:
    return text.strip().lower() if isinstance(text, str) and text.strip() else None
