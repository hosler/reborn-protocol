"""Host-independent contracts used by GS1 host adapters."""

from __future__ import annotations


# Script-facing player property -> canonical host attribute.  A host may add
# properties whose backing name is host-specific (notably ``playeraccount``).
A_CLASS_PLAYER_ATTR = {
    "playerdir": "direction",
    "playersprite": "sprite",
    "playerrupees": "rupees",
    "playergralats": "rupees",
    "playerhearts": "hearts",
    "playerfullhearts": "max_hearts",
    "playerarrows": "arrows",
    "playerbombs": "bombs",
    "playerswordpower": "sword_power",
    "playershieldpower": "shield_power",
    "playernick": "nickname",
    "playerhead": "head_image",
    "playerbody": "body_image",
    "playersword": "sword_image",
    "playershield": "shield_image",
}

# Script-facing NPC property -> canonical host field.
A_CLASS_NPC_ATTR = {
    "x": "x",
    "y": "y",
    "dir": "direction",
    "image": "image",
    "ani": "gani",
    "nick": "nickname",
    "message": "message",
    "glovepower": "glove_power",
}


def host_value(value):
    """Return the GS1-facing scalar representation used by both hosts."""
    if isinstance(value, str):
        return value
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def tokens_count(context) -> float:
    """Return the numeric count produced by the most recent ``tokenize``."""
    return float(len(getattr(context, "tokenize_tokens", []) or []))
