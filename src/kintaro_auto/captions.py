from __future__ import annotations

import random
import re

_SPINTAX_PATTERN = re.compile(r"\{([^{}]+)\}")
_MENTION_PATTERN = re.compile(r"@([A-Za-z0-9._]+)")
_HASHTAG_PATTERN = re.compile(r"#[^\s#]+")


def resolve_spintax(template: str, rng: random.Random | None = None) -> str:
    chooser = rng or random
    result = template.replace("{RAND4}", str(chooser.randint(1000, 9999))).replace(
        "{RAND6}", str(chooser.randint(100000, 999999))
    )
    for _ in range(16):
        match = _SPINTAX_PATTERN.search(result)
        if not match:
            break
        inner = match.group(1)
        if "|" not in inner:
            break
        options = inner.split("|")
        chosen = chooser.choice(options)
        result = result[: match.start()] + chosen + result[match.end() :]
    return result


def randomize_hashtags(
    description: str,
    used_captions: set[str] | None = None,
    rng: random.Random | None = None,
) -> str:
    """Randomizes hashtag order uniquely across consecutive posts while preserving prefix text."""
    if not description or "#" not in description:
        return description
    tags = _HASHTAG_PATTERN.findall(description)
    if len(tags) <= 1:
        return description
    chooser = rng or random
    prefix = re.sub(r"\s{2,}", " ", _HASHTAG_PATTERN.sub("", description)).strip()
    seen = used_captions if used_captions is not None else set()
    for _ in range(25):
        shuffled = list(tags)
        chooser.shuffle(shuffled)
        cand = (f"{prefix} " if prefix else "") + " ".join(shuffled)
        if cand not in seen:
            seen.add(cand)
            return cand
    shuffled = list(tags)
    chooser.shuffle(shuffled)
    return (f"{prefix} " if prefix else "") + " ".join(shuffled)


def prepare_description_and_mentions(
    raw_description: str,
    explicit_mentions: list[str] | None = None,
    rng: random.Random | None = None,
    *,
    shuffle_tags: bool = False,
    used_captions: set[str] | None = None,
) -> tuple[str, list[str]]:
    """
    Resolves spintax (`{A|B}`, `{RAND4}`, `{RAND6}`), optionally randomizes hashtag order,
    and enforces Kintaro API's mention contract:
    `The description itself must contain a space after each mention.`
    Returns `(normalized_description, mentions_without_at)`.
    """
    text = resolve_spintax(raw_description, rng=rng)
    if shuffle_tags:
        text = randomize_hashtags(text, used_captions=used_captions, rng=rng)
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
