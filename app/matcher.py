"""
Matcher Module - Local vocabulary hit detection.

Detects which unit vocabulary the student used in their speech. Pure code
logic, no LLM calls. The results are display/report statistics only — they
drive nothing in the session loop.

Design:
  - Phrases: substring match; single words: word-boundary match with
    tolerant suffixes
  - Cumulative tracking: SessionMatcher keeps unique hits across turns
"""
import re


def normalize_spoken_text(text: str) -> str:
    """Normalize spoken text for vocabulary matching.

    Keeps matching tolerant to common ASR/spelling variants such as
    "P.E. teacher", "P. E. teacher" or "p. e. teacher" -> "pe teacher"
    while staying fully content-agnostic.
    """
    # Merge spaced dotted initials first ("P. E." -> "P.E."), otherwise the
    # later punctuation removal leaves "p e" behind and breaks matching.
    t = _collapse_dotted_initials(text).lower()
    # Remove punctuation characters that can split or decorate words.
    t = re.sub(r"[.,;:!?\"'’]", "", t)
    # Collapse whitespace.
    return re.sub(r"\s+", " ", t).strip()


def _collapse_dotted_initials(text: str) -> str:
    """Merge abbreviations whose initials are separated by dots and spaces.

    "P. E." / "P.E." / "p. e." all become "P.E."-style runs. Only letters
    directly attached to a dot are merged, so ordinary words and articles
    ("a", "I") are never affected.
    """
    prev = None
    while prev != text:
        prev = text
        text = re.sub(
            r"\b([A-Za-z])\.\s+([A-Za-z])(?=\.|\s|$)",
            r"\1.\2",
            text,
        )
    return text


def match_vocabulary(text: str, required_vocab: list[str]) -> list[str]:
    """
    Check which required vocabulary items appear in the student's text.

    - Multi-word phrases: substring match
    - Single words: word-boundary match (with common suffix tolerance)
    - Compound items ("option a/option b"): split by "/" and match alternatives

    Returns: list of matched vocabulary items (preserving order from required).
    """
    if not text or not required_vocab:
        return []

    text_lower = normalize_spoken_text(text)
    matched = []

    for word in required_vocab:
        word_lower = normalize_spoken_text(word)
        if not word_lower:
            continue

        # Handle compound items like "his/her" → match "his" OR "her"
        if "/" in word_lower:
            alternatives = [a.strip() for a in word_lower.split("/") if a.strip()]
            for alt in alternatives:
                if " " in alt:
                    if alt in text_lower:
                        matched.append(word)
                        break
                else:
                    pattern = r"\b" + re.escape(alt) + r"(?:s|es|ed|ing|ly)?\b"
                    if re.search(pattern, text_lower):
                        matched.append(word)
                        break
        elif " " in word_lower:
            # Multi-word phrase → substring match
            if word_lower in text_lower:
                matched.append(word)
        else:
            # Single word → word-boundary match
            pattern = r"\b" + re.escape(word_lower) + r"(?:s|es|ed|ing|ly)?\b"
            if re.search(pattern, text_lower):
                matched.append(word)

    return matched


class SessionMatcher:
    """
    Tracks cumulative vocabulary hits across all turns in a session.

    Usage:
        matcher = SessionMatcher(required_vocab)
        for each student turn:
            result = matcher.check_turn(text)
        # matcher.cumulative_vocab_hits has all unique hits so far
    """

    def __init__(self, required_vocab: list[str]):
        self.required_vocab = required_vocab
        self.cumulative_vocab_hits: set[str] = set()
        self.total_turns = 0

    def check_turn(self, text: str) -> dict:
        """Check a single turn and update cumulative state."""
        self.total_turns += 1
        hits = match_vocabulary(text, self.required_vocab)
        self.cumulative_vocab_hits.update(hits)

        vocab_missed = sorted(
            set(self.required_vocab) - self.cumulative_vocab_hits,
            key=lambda w: self.required_vocab.index(w) if w in self.required_vocab else 999
        )

        return {
            "turn_vocab_hits": hits,
            "cumulative_vocab_hits": sorted(
                self.cumulative_vocab_hits,
                key=lambda w: self.required_vocab.index(w) if w in self.required_vocab else 999
            ),
            "cumulative_vocab_missed": vocab_missed,
            "accuracy": round(len(self.cumulative_vocab_hits) / len(self.required_vocab), 4)
                        if self.required_vocab else 0.0,
        }

    def get_final_report(self) -> dict:
        """Generate the end-of-session report. Pure computation, no LLM."""
        return {
            "accuracy": round(len(self.cumulative_vocab_hits) / len(self.required_vocab), 4)
                        if self.required_vocab else 0.0,
            "vocabulary_used": sorted(
                self.cumulative_vocab_hits,
                key=lambda w: self.required_vocab.index(w) if w in self.required_vocab else 999
            ),
            "vocabulary_missed": sorted(
                set(self.required_vocab) - self.cumulative_vocab_hits,
                key=lambda w: self.required_vocab.index(w) if w in self.required_vocab else 999
            ),
            "vocab_detail": [
                {"word": w, "hit": w in self.cumulative_vocab_hits}
                for w in self.required_vocab
            ],
            "total_turns": self.total_turns,
        }
