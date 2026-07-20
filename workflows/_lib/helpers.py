"""Shared helpers for workflows.

Anything under an underscore-prefixed folder (like this ``_lib/``) is
ignored by workflow discovery but perfectly importable from flow files:

    from _lib.helpers import shout
"""


def shout(text: str) -> str:
    return f"{text.upper()}!!"


def money(amount: float, currency: str = "EUR") -> str:
    return f"{amount:,.2f} {currency}"
