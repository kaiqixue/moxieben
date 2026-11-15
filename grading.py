"""Strict transcription comparison: differences explain a score; they never determine partial credit."""
import re
from difflib import SequenceMatcher


def normalize(text):
    return ' '.join(text.split())


def compare(actual, expected, variants):
    actual = normalize(actual)
    accepted = [normalize(expected), *(normalize(v) for v in variants)]
    matched = actual in accepted
    target = actual if matched else accepted[0]
    # Keep punctuation as separate tokens; similarity is used only to display differences.
    tokenize = lambda s: re.findall(r"\w+|[^\w\s]|\s+", s, flags=re.UNICODE)
    a, b = tokenize(target), tokenize(actual)
    diff = [{'op': op, 'expected': ''.join(a[i:j]), 'actual': ''.join(b[k:l])}
            for op, i, j, k, l in SequenceMatcher(None, a, b, autojunk=False).get_opcodes()]
    return {'matched': matched, 'diff': diff}
