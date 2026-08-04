"""Stable, deterministic Message IDs for provenance tracing.

Format: <conversation_id>_s<session_index:02d>_m<turn_index:03d>

Same dataset hash always produces the same IDs. No random UUIDs.
"""

from __future__ import annotations


def make_message_id(
    conversation_id: str,
    session_index: int,
    turn_index: int,
) -> str:
    """Generate a stable message ID from conversation/session/turn indices.

    Example: conv_03_s05_m007
    """
    return f"{conversation_id}_s{session_index:02d}_m{turn_index:03d}"


def parse_message_id(message_id: str) -> tuple[str, int, int] | None:
    """Parse a stable message ID back into (conversation_id, session_index, turn_index).

    Returns None if the format doesn't match.
    """
    import re
    m = re.match(r"(.+)_s(\d{2,})_m(\d{3,})$", message_id)
    if not m:
        return None
    return m.group(1), int(m.group(2)), int(m.group(3))
