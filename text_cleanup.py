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
    codes = [str(code).lower() for code in value] if isinstance(value, (list, tuple)) else []
    codes = [code for code in codes if code in FILLER_LANGUAGES]
    return codes or list(DEFAULT_FILLER_LANGUAGES)


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
        pre, post = match.group("pre"), match.group("post")
        sentence_start = not before or before[-1] in _SENTENCE_END + _MARK or before.endswith("\n")
        ending = next((char for char in post if char in _SENTENCE_END), "")
        if pre:
            sentence_start = False
        if sentence_start:
            # Mark the spot so the word that now opens the sentence gets its capital.
            return (match.group("lead") if before else "") + _MARK
        if ending:
            return ending
        return "," if pre else ""

    previous = None
    while previous != text:
        previous = text
        text = pattern.sub(replace, text)
    text = re.sub(rf"{_MARK}\s*(\S)", lambda match: match.group(1).upper(), text)
    text = text.replace(_MARK, "")
    text = re.sub(r"[ \t]+([,.;:!?…])", r"\1", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    return text.strip()


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
