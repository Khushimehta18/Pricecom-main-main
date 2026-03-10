from typing import List, Tuple
import difflib

def best_matches(query: str, choices: List[str], limit: int = 5) -> List[Tuple[str, float]]:
    """Return best fuzzy matches using difflib SequenceMatcher ratio.

    Returns list of (choice, score) sorted descending.
    """
    if not query or not choices:
        return []
    scores = []
    qlow = query.lower()
    for c in choices:
        score = difflib.SequenceMatcher(None, qlow, c.lower()).ratio()
        scores.append((c, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    return scores[:limit]
