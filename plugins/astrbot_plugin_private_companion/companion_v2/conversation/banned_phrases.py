"""Filter companion-authored text with the installation's compiled regexes."""

import logging
import re
import unicodedata


logger = logging.getLogger(__name__)
DEFAULT_PATTERNS = ("慢吞吞", "慢慢悠悠", "慢悠悠", "不紧不慢")
_SPACES = re.compile(r"[^\S\r\n]{2,}")
_REPEATED_PUNCTUATION = re.compile(r"([，。！？、；：,.!?;:])(?:[^\S\r\n]*\1)+")
_CLAUSE_PUNCTUATION = re.compile(r"[,，、;；][^\S\r\n]*([。！？.!?])")
_LEADING_PUNCTUATION = re.compile(r"^[\s，。、；：！？,.!?;:]+")


def _has_content(text):
    return any(not char.isspace() and not unicodedata.category(char).startswith("P") for char in text)


def _remove_matches(text, pattern):
    chunks, end, count = [], 0, 0
    for match in pattern.finditer(text):
        start, stop = match.span()
        if start == stop or start < end:
            continue
        chunks.append(text[end:start])
        end = stop + (text[stop:stop + 1] in ("地", "的", "得"))
        count += 1
    return ("".join((*chunks, text[end:])), count) if count else (text, 0)


class BannedPhraseFilter:
    def __init__(self, patterns):
        if not isinstance(patterns, (list, tuple)):
            raise ValueError("banned_phrases must be a list of regular expressions")
        compiled = []
        for index, pattern in enumerate(patterns):
            if not isinstance(pattern, str):
                raise ValueError(f"banned_phrases[{index}] must be a regular expression string")
            try:
                compiled.append(re.compile(pattern))
            except (re.error, OverflowError, RecursionError):
                # Report where to repair the config without echoing the pattern.
                raise ValueError(f"banned_phrases[{index}] is an invalid regular expression") from None
        self.patterns = tuple(compiled)

    def __bool__(self):
        return bool(self.patterns)

    def segments(self, parts, *, surface, preserve_reply=True):
        """Keep segment positions for media and speech; an empty entry can be dropped."""
        if not self.patterns:
            return parts
        counts = [0] * len(self.patterns)
        cleaned = []
        for original in parts:
            text = original
            for index, pattern in enumerate(self.patterns):
                text, removed = _remove_matches(text, pattern)
                counts[index] += removed
            if text != original:
                text = _SPACES.sub(" ", text).strip()
                text = _REPEATED_PUNCTUATION.sub(r"\1", text)
                text = _CLAUSE_PUNCTUATION.sub(r"\1", text)
                text = _LEADING_PUNCTUATION.sub("", text)
            cleaned.append(text)
        if not any(counts):
            return parts
        for index, count in enumerate(counts):
            if count:
                logger.info("[CompanionV2] 禁用短语命中 surface=%s pattern_index=%d count=%d", surface, index, count)
        cleaned = tuple(text if _has_content(text) else "" for text in cleaned)
        if preserve_reply and not any(cleaned):
            logger.warning("[CompanionV2] 禁用短语将清空回复，保留原文 surface=%s count=%d", surface, sum(counts))
            return parts
        return cleaned

    def text(self, text, *, surface, preserve_reply=True):
        return self.segments((text,), surface=surface, preserve_reply=preserve_reply)[0]


NO_BANNED_PHRASES = BannedPhraseFilter(())
