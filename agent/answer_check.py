"""Check that identifiers and metrics quoted in an answer appear in the tool results behind it.

Covers hex identifiers (run ids, md5, commits, pod hashes; at least 7 characters with a
digit and a letter) and metric-like decimals (0.xxx or 1.xxx with 3+ decimals). A decimal is
accepted when it equals some tool-result number rounded to the decimals the answer used.
shortcut: other claims (states, counts, prose) are not checked; extend when they matter.
"""

import json
import re
from typing import Any

_HEX = re.compile(r"\b(?=[0-9a-f]*\d)(?=[0-9a-f]*[a-f])[0-9a-f]{7,40}\b", re.I)
_METRIC = re.compile(r"(?<![\w.])[01]\.\d{3,}(?!\w|\.\d)")
_NUMBER = re.compile(r"\d+\.\d+")


def unverified_values(answer: str, evidence: list[Any], other_text: str = "") -> list[str]:
    """Values in ``answer`` that occur in neither the tool results nor ``other_text``."""
    corpus = (json.dumps(evidence, default=str) + " " + other_text).lower()
    numbers = [float(n) for n in _NUMBER.findall(corpus)]
    missing = [t for t in dict.fromkeys(_HEX.findall(answer)) if t.lower() not in corpus]
    for token in dict.fromkeys(_METRIC.findall(answer)):
        tolerance = 0.5 * 10 ** -len(token.split(".")[1]) + 1e-9
        if not any(abs(n - float(token)) <= tolerance for n in numbers):
            missing.append(token)
    return missing
