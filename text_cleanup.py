"""Dictionary corrections and filler-word removal without a language model.

Both steps are plain text rules, so they add no measurable latency and run
for every provider, local or cloud.
"""

from __future__ import annotations

import re

# Clear hesitations per language. Words that carry meaning ("like", "well",
# "dus", "gewoon") are deliberately absent.
FILLER_LANGUAGES = {
    "en": ("English", ("uh", "uhh", "um", "umm", "uhm", "erm", "er", "mhm")),
    "nl": ("Dutch", ("uh", "uhm", "eh", "ehm", "euh", "euhm", "hm")),
    "de": ("German", ("äh", "ähm", "ehm", "hm", "mh")),
    "fr": ("French", ("euh", "heu", "hum")),
    "es": ("Spanish", ("eh", "em", "ehm", "este")),
}
# Fillers in one language that are ordinary words in another. They stay
# whenever the second language is enabled.
PROTECTED_WORDS = {
    "en": (),
    "nl": ("er", "este"),
    "de": ("um", "er", "este"),
    "fr": ("este",),
    "es": ("er",),
}
DEFAULT_FILLER_LANGUAGES = ("en", "nl")

_SENTENCE_END = ".!?…"
_MARK = "\x00"
_WORD_EDGE = r"[\w'’-]"


def normalize_vocabulary(value) -> list[str]:
    """Return unique, non-empty vocabulary terms in their original order."""
    if isinstance(value, str):
        value = value.replace("\n", ",").split(",")
    seen, terms = set(), []
    for item in value if isinstance(value, (list, tuple)) else ():
        term = " ".join(str(item).split())
        if term and term.lower() not in seen:
            seen.add(term.lower())
            terms.append(term)
    return terms


def normalize_corrections(value) -> list[dict]:
    """Return correction rules as {"from": ..., "to": ...} with both sides filled."""
    rules, seen = [], set()
    for item in value if isinstance(value, (list, tuple)) else ():
        if isinstance(item, dict):
            source, target = item.get("from", ""), item.get("to", "")
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            source, target = item
        else:
            continue
        source = " ".join(str(source).split())
        target = " ".join(str(target).split())
        if source and target and source != target and source.lower() not in seen:
            seen.add(source.lower())
            rules.append({"from": source, "to": target})
    return rules


def normalize_filler_languages(value) -> list[str]:
    """Known language codes from ``value``; defaults only when the setting is absent or malformed.

    An explicit empty list is kept, so a user can switch every language off and
    remove only their own extra words.
    """
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_FILLER_LANGUAGES)
    codes = []
    for code in value:
        code = str(code).lower()
        if code in FILLER_LANGUAGES and code not in codes:
            codes.append(code)
    return codes


def filler_words(languages, custom="") -> tuple[str, ...]:
    """Fillers to remove for the enabled languages, minus words another enabled language needs."""
    languages = normalize_filler_languages(languages)
    protected = {word for code in languages for word in PROTECTED_WORDS.get(code, ())}
    words = []
    for code in languages:
        for word in FILLER_LANGUAGES[code][1]:
            if word not in protected and word not in words:
                words.append(word)
    for word in normalize_vocabulary(custom):
        if word.lower() not in words:
            words.append(word.lower())
    return tuple(words)


def _filler_pattern(words):
    alternatives = "|".join(re.escape(word) for word in sorted(words, key=len, reverse=True))
    return re.compile(
        rf"(?P<pre>,)?(?P<lead>^|\s+)(?<!{_WORD_EDGE})(?P<word>(?:{alternatives})(?:\s+(?:{alternatives}))*)"
        rf"(?!{_WORD_EDGE})(?P<post>[,;:.!?…]*)(?=\s|$)",
        re.IGNORECASE | re.UNICODE,
    )


def remove_fillers(text: str, words) -> str:
    """Drop hesitations and repair the surrounding punctuation and spacing."""
    words = tuple(word for word in words if word)
    if not text or not words:
        return text
    pattern = _filler_pattern(words)

    def replace(match):
        start = match.start()
        before = text[:start].rstrip()
        pre, lead, post = match.group("pre"), match.group("lead"), match.group("post")
        line_start = "\n" in lead
        sentence_start = line_start or not before or before[-1] in _SENTENCE_END + _MARK
        ending = next((char for char in post if char in _SENTENCE_END), "")
        if pre and not line_start:
            sentence_start = False
        if line_start:
            # Keep the line break; the word that now opens the line gets its capital.
            return (pre or "") + lead + _MARK
        if sentence_start:
            # Mark the spot so the word that now opens the sentence gets its capital.
            return (lead if before else "") + _MARK
        if ending:
            return ending
        return "," if pre else ""

    previous = None
    while previous != text:
        previous = text
        text = pattern.sub(replace, text)
    text = re.sub(rf"{_MARK}\s*(\S+)", lambda match: _capitalize(match.group(1)), text)
    text = text.replace(_MARK, "")
    text = re.sub(r"[ \t]+([,.;:!?…])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


def _capitalize(word: str) -> str:
    """Capitalize a plain lowercase word; leave intentional casing such as iPhone alone."""
    if word[0].islower() and not any(char.isupper() for char in word[1:]):
        return word[0].upper() + word[1:]
    return word


def apply_corrections(text: str, rules) -> str:
    """Replace whole-word misspellings with their preferred spelling."""
    for rule in normalize_corrections(rules):
        pattern = re.compile(rf"(?<!\w){re.escape(rule['from'])}(?!\w)", re.IGNORECASE)
        text = pattern.sub(lambda _match, target=rule["to"]: target, text)
    return text


def clean_transcript(text: str, cfg: dict) -> str:
    """Apply the configured filler removal and dictionary corrections."""
    if not text:
        return text
    if cfg.get("remove_fillers", True):
        text = remove_fillers(
            text, filler_words(cfg.get("filler_languages"), cfg.get("custom_fillers", ""))
        )
    return apply_corrections(text, cfg.get("corrections", ()))
