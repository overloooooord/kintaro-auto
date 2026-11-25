from __future__ import annotations

import random
import re

_SPINTAX_PATTERN = re.compile(r"\{([^{}]+)\}")
_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9._]+)")


def resolve_spintax(template: str, rng: random.Random | None = None) -> str:
    chooser = rng or random
    result = template
    while True:
        match = _SPINTAX_PATTERN.search(result)
        if not match:
            break
        options = match.group(1).split("|")
        chosen = chooser.choice(options)
        result = result[: match.start()] + chosen + result[match.end() :]
    return result


def prepare_description_and_mentions(
    raw_description: str,
    explicit_mentions: list[str] | None = None,
    rng: random.Random | None = None,
) -> tuple[str, list[str]]:
    """
    Resolves spintax and enforces Kintaro API's mention contract:
    `The description itself must contain a space after each mention.`
    Returns `(normalized_description, mentions_without_at)`.
    """
    text = resolve_spintax(raw_description, rng=rng)
    clean_explicit = [
        m.lstrip("@").strip()
        for m in (explicit_mentions or [])
        if m.lstrip("@").strip()
    ]

    # Append any explicit mentions not yet present in text
    existing_lower = {m.lower() for m in _MENTION_PATTERN.findall(text)}
    for username in clean_explicit:
        if username.lower() not in existing_lower:
            if text and not text.endswith(" "):
                text += " "
            text += f"@{username} "
            existing_lower.add(username.lower())

    # Ensure every `@username` in text is immediately followed by a literal space `' '`
    rebuilt: list[str] = []
    last_end = 0
    ordered_mentions: list[str] = []
    seen: set[str] = set()

    for m in _MENTION_PATTERN.finditer(text):
        uname = m.group(1)
        rebuilt.append(text[last_end : m.end()])
        next_char = text[m.end() : m.end() + 1]
        if next_char != " ":
            rebuilt.append(" ")
        last_end = m.end()
        if uname.lower() not in seen:
            seen.add(uname.lower())
            ordered_mentions.append(uname)

    rebuilt.append(text[last_end:])
    return "".join(rebuilt), ordered_mentions
