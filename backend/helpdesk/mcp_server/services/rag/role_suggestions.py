"""Roles close in meaning to some typed words: the ranking behind `suggest_roles`.

Pure: the caller reads the roles and hands in the embedder. Each role is
compared as "<system name> <role name>" and as each phrase saying what its
system is for, and scores its best match. Only the dense model is
used, since a missed name is a matter of meaning, not of shared keywords.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from helpdesk.mcp_server.services.rag.embedding import Embedder, cosine
from helpdesk.shared.core.naming import InvalidSearchArgumentError

SUGGESTION_FLOOR = 0.70
"""Inclusive. Lower offers roles that are not close; higher says "nothing here"
for a fair guess. A starting value, tuned with the golden set."""

MAX_SUGGESTIONS = 3
"""Few enough to read in one reply."""

SUGGEST_QUERY_MAX_CHARS = 200
"""Inclusive. The words are a system and a role name, never a sentence."""

CACHE_CAP = 4096
"""The most role texts kept. A tenant has tens; the cap only stops growth."""

Vector = tuple[float, ...]


@dataclass(frozen=True)
class RoleRow:
    """One role, reduced to what is shown and what is compared."""

    system_slug: str
    system_name: str
    role_slug: str
    role_name: str
    system_about: str = ""
    """Comma-separated phrases; compared, never shown."""


@dataclass(frozen=True)
class Suggestion:
    row: RoleRow
    score: float


def role_text(row: RoleRow) -> str:
    """The text a role is embedded as."""
    return f"{row.system_name} {row.role_name}"


def role_texts(row: RoleRow) -> list[str]:
    """Every text a role is compared as: its name text, then each phrase of its
    system, with spaces trimmed and blank phrases dropped."""
    phrases = [phrase.strip() for phrase in row.system_about.split(",")]
    return [role_text(row), *(phrase for phrase in phrases if phrase)]


class TextVectorCache:
    """Vectors by text, oldest out first. Kept for the life of the process, so a
    role is embedded once, not on every miss."""

    def __init__(self, cap: int = CACHE_CAP) -> None:
        self._cap = cap
        self._vectors: dict[str, Vector] = {}

    def __len__(self) -> int:
        return len(self._vectors)

    def vectors(self, embedder: Embedder, texts: Sequence[str]) -> list[Vector]:
        """One vector per text, in order. Only unseen texts reach the model, in
        one batch."""
        missing = list(dict.fromkeys(text for text in texts if text not in self._vectors))
        fresh = dict(zip(missing, embedder.embed_dense(missing), strict=True)) if missing else {}
        for text, vector in fresh.items():
            self._vectors[text] = vector
        while len(self._vectors) > self._cap:
            del self._vectors[next(iter(self._vectors))]
        return [fresh[text] if text in fresh else self._vectors[text] for text in texts]


def check_suggest_query(value: str) -> str:
    """Refuse an empty or too long query before it reaches the model."""
    if len(value) > SUGGEST_QUERY_MAX_CHARS:
        raise InvalidSearchArgumentError(
            f"query is longer than {SUGGEST_QUERY_MAX_CHARS} characters: {len(value)}"
        )
    if not value.strip():
        raise InvalidSearchArgumentError("query is empty")
    return value


def suggest(
    embedder: Embedder, cache: TextVectorCache, query: str, rows: Sequence[RoleRow]
) -> list[Suggestion]:
    """The roles at or above the floor, best first, at most three. A role
    scores its best text. Equal scores go by role slug, so the order does not
    depend on how the rows were read."""
    query_vector = embedder.embed_dense([query])[0]
    texts = [role_texts(row) for row in rows]
    vectors = cache.vectors(embedder, [text for row_texts in texts for text in row_texts])
    scored: list[Suggestion] = []
    start = 0
    for row, row_texts in zip(rows, texts, strict=True):
        own = vectors[start : start + len(row_texts)]
        start += len(row_texts)
        scored.append(Suggestion(row=row, score=max(cosine(query_vector, v) for v in own)))
    kept = [item for item in scored if item.score >= SUGGESTION_FLOOR]
    kept.sort(key=lambda item: (-item.score, item.row.role_slug))
    return kept[:MAX_SUGGESTIONS]
