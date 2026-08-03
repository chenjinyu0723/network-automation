"""Port-name equivalence for safety checks without rewriting device commands.

Different switch families can accept or display either short (``GE0/0/1``) or
long (``GigabitEthernet0/0/1``) interface names.  The topology's original text
is therefore retained all the way to the generated CLI.  This module provides
only a comparison key so that protection and validation cannot be bypassed by
switching between the two spellings.
"""

from __future__ import annotations

import re

_PORT_PREFIXES = (
    ("GIGABITETHERNET", "GE"),
    ("TENGIGABITETHERNET", "XGE"),
    ("HUNDREDGIGABITETHERNET", "100GE"),
    ("FORTYGIGABITETHERNET", "40GE"),
)


def port_identity(port: str | None) -> str:
    """Return a comparison-only port key while preserving unknown names safely.

    Only a leading, well-known interface type is treated as an alias.  Other
    names are folded for case/space-insensitive exact comparison, rather than
    being guessed as a physical port.
    """

    compact = re.sub(r"\s+", "", (port or "")).upper()
    for long_name, short_name in _PORT_PREFIXES:
        if compact.startswith(long_name):
            return short_name + compact[len(long_name) :]
    return compact


def port_appears_in_output(output: str, port: str) -> bool:
    """Check validation output using the same aliases accepted by safety gates."""

    wanted = port_identity(port)
    if not wanted:
        return False
    # Preserve separators while replacing only the leading interface type, so
    # GE0/0/1 is not confused with GE0/0/10 or XGE0/0/1 embedded in prose.
    normalized_output = output.upper()
    for long_name, short_name in sorted(_PORT_PREFIXES, key=lambda item: -len(item[0])):
        normalized_output = normalized_output.replace(long_name, short_name)
    candidates = re.findall(
        r"(?<![A-Z0-9])(?:100GE|40GE|XGE|GE)\d+(?:/\d+)+(?:\.\d+)?",
        normalized_output,
    )
    return any(port_identity(candidate) == wanted for candidate in candidates)
