"""Suggesting roles by meaning: ranking, the floor, the text cache, the query check.

Surface under test (written before it exists; the implementer must provide
exactly this) in ``helpdesk.mcp_server.services.rag.role_suggestions``:

``SUGGESTION_FLOOR = 0.70`` (inclusive), ``MAX_SUGGESTIONS = 3``,
``SUGGEST_QUERY_MAX_CHARS = 200``, ``CACHE_CAP = 4096``.
``RoleRow(system_slug, system_name, role_slug, role_name)`` and
``Suggestion(row, score)``, both frozen dataclasses.
``role_text(row)`` is "<system name> <role name>".
``TextVectorCache(cap=CACHE_CAP)`` with ``len()`` and
``vectors(embedder, texts)``: one batched ``embed_dense`` call for the texts it
has not seen (no call when all are known); the oldest entry goes when it is
over ``cap``.
``check_suggest_query(value)`` returns a value of 1 to 200 characters, else
raises ``InvalidSearchArgumentError`` (code ``VALIDATION``).
``suggest(embedder, cache, query, rows)`` embeds the query every time, scores
with ``embedding.cosine``, keeps score >= the floor, best first (ties by
``role_slug``), at most three.

Everything here is pure: a fake embedder with hand-made vectors, no service.
"""

import math
from collections.abc import Mapping, Sequence

import pytest

from helpdesk.mcp_server.services.rag.embedding import SparseVector, cosine
from helpdesk.mcp_server.services.rag.role_suggestions import (
    CACHE_CAP,
    MAX_SUGGESTIONS,
    SUGGEST_QUERY_MAX_CHARS,
    SUGGESTION_FLOOR,
    RoleRow,
    TextVectorCache,
    check_suggest_query,
    role_text,
    role_texts,
    suggest,
)
from helpdesk.shared.core.errors import ErrorCode
from helpdesk.shared.core.naming import InvalidSearchArgumentError

Vector = tuple[float, ...]

QUERY = "fake docs editor"
QUERY_VECTOR: Vector = (1.0, 0.0)


class FakeEmbedder:
    """Hand-made vectors by text; every `embed_dense` call is recorded.

    A text with no vector is a test bug, so it raises instead of guessing.
    The other two protocol methods fail loudly: suggestions use the dense
    model only.
    """

    def __init__(self, vectors: Mapping[str, Vector], default: Vector | None = None) -> None:
        self._vectors = dict(vectors)
        self._default = default
        self.calls: list[list[str]] = []

    def embed_dense(self, texts: Sequence[str]) -> list[Vector]:
        self.calls.append(list(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> Vector:
        if text in self._vectors:
            return self._vectors[text]
        if self._default is not None:
            return self._default
        raise AssertionError(f"no vector made for {text!r}")

    def embed_sparse(self, texts: Sequence[str]) -> list[SparseVector]:
        raise AssertionError("suggestions never use the sparse model")

    def count_tokens(self, text: str) -> int:
        raise AssertionError("suggestions never count tokens")

    def embedded(self, text: str) -> int:
        """How many times `text` went through the model, over all calls."""
        return sum(call.count(text) for call in self.calls)


def _row(slug: str, *, system: str = "Fake Wiki") -> RoleRow:
    return RoleRow(
        system_slug="fake-wiki",
        system_name=system,
        role_slug=slug,
        role_name=f"Fake {slug.title()}",
    )


def _at(cos: float) -> Vector:
    """A unit vector whose cosine with `QUERY_VECTOR` is `cos`."""
    return (cos, math.sqrt(1.0 - cos * cos))


def _embedder_for(scored: Mapping[RoleRow, Vector]) -> FakeEmbedder:
    vectors: dict[str, Vector] = {QUERY: QUERY_VECTOR}
    vectors.update({role_text(row): vector for row, vector in scored.items()})
    return FakeEmbedder(vectors)


# --- the pinned values --------------------------------------------------------


def test_the_values_are_exactly_these() -> None:
    """Catches a floor, cap or length of any other size: each is a fixed number,
    and a range check would admit a wrong one."""
    assert SUGGESTION_FLOOR == 0.70
    assert MAX_SUGGESTIONS == 3
    assert SUGGEST_QUERY_MAX_CHARS == 200
    assert CACHE_CAP == 4096


def test_role_text_is_the_system_name_then_the_role_name() -> None:
    """Catches the two names swapped or joined without the space: the model is
    shown this exact text, so a different shape moves every score."""
    row = RoleRow(
        system_slug="fake-wiki",
        system_name="Fake Wiki",
        role_slug="editor",
        role_name="Fake Editor",
    )

    assert role_text(row) == "Fake Wiki Fake Editor"


# --- the phrases of a system --------------------------------------------------


def _row_about(slug: str, about: str) -> RoleRow:
    return RoleRow(
        system_slug="fake-wiki",
        system_name="Fake Wiki",
        role_slug=slug,
        role_name=f"Fake {slug.title()}",
        system_about=about,
    )


@pytest.mark.parametrize(
    "about",
    [
        "docs, team pages",
        "  docs ,team pages  ",
        "docs,, team pages,",
        " , docs ,  , team pages , ",
    ],
    ids=["plain", "spaces", "no-spaces-and-trailing-comma", "blanks-everywhere"],
)
def test_role_texts_are_the_name_text_then_each_phrase(about: str) -> None:
    """Catches a split that keeps blank phrases or the spaces around commas
    (each would be embedded as noise), a list with the name text last or
    missing, and phrases in another order."""
    row = _row_about("editor", about)

    assert role_texts(row) == ["Fake Wiki Fake Editor", "docs", "team pages"]


@pytest.mark.parametrize("about", ["", "   ", " , ,"], ids=["empty", "spaces", "only-commas"])
def test_a_role_with_no_about_has_only_its_name_text(about: str) -> None:
    """Catches an empty phrase "" slipping in as a second text when a system
    has no phrases (the default of every old row): the list must be exactly
    the name text."""
    row = _row_about("editor", about)

    assert role_texts(row) == [role_text(row)]


@pytest.mark.parametrize(
    ("name_score", "phrase_scores"),
    [(0.2, [0.9]), (0.9, [0.2]), (0.2, [0.3, 0.9]), (0.3, [0.9, 0.2])],
    ids=["phrase-wins", "name-wins", "last-phrase-wins", "first-phrase-wins"],
)
def test_a_role_scores_its_best_text(name_score: float, phrase_scores: list[float]) -> None:
    """Catches a score that is the name text only, a phrase only, the first
    phrase only, or an average (0.55 here, under the floor): the role holds one
    strong text and weak ones, and must be offered at the strong one's score."""
    phrases = [f"phrase {index}" for index in range(len(phrase_scores))]
    row = _row_about("editor", ", ".join(phrases))
    vectors: dict[str, Vector] = {QUERY: QUERY_VECTOR, role_text(row): _at(name_score)}
    vectors.update({p: _at(score) for p, score in zip(phrases, phrase_scores, strict=True)})

    (only,) = suggest(FakeEmbedder(vectors), TextVectorCache(), QUERY, [row])

    assert only.score == pytest.approx(0.9)
    assert only.row == row


def test_a_phrase_below_the_floor_offers_nothing() -> None:
    """Catches a floor loosened for phrases (a phrase at 0.6999 must not offer
    the role) or a best-of that ignores the floor: every text of the role is
    under 0.70, the best one by a hair."""
    row = _row_about("editor", "docs, team pages")
    below: Vector = (6999.0, math.sqrt(1e8 - 6999.0**2))
    assert 0.6998 < cosine(QUERY_VECTOR, below) < SUGGESTION_FLOOR
    vectors: dict[str, Vector] = {
        QUERY: QUERY_VECTOR,
        role_text(row): _at(0.5),
        "docs": _at(0.3),
        "team pages": below,
    }

    result = suggest(FakeEmbedder(vectors), TextVectorCache(), QUERY, [row])

    assert result == []


def test_phrases_shared_by_two_roles_are_embedded_once() -> None:
    """Catches a ranker that embeds a system's phrases again for every role of
    that system (the cache is keyed by text, so the batch must not repeat
    them): two roles, two phrases, each phrase reaches the model once."""
    rows = [_row_about("editor", "docs, team pages"), _row_about("viewer", "docs, team pages")]
    embedder = FakeEmbedder({}, default=QUERY_VECTOR)

    suggest(embedder, TextVectorCache(), QUERY, rows)

    assert embedder.embedded("docs") == 1
    assert embedder.embedded("team pages") == 1
    assert [embedder.embedded(role_text(row)) for row in rows] == [1, 1]


# --- ranking ------------------------------------------------------------------


def test_ranks_best_first_and_caps_at_three() -> None:
    """Catches a list returned in the order the rows came in, one cut at the
    wrong size, and one cut BEFORE sorting: the rows are given worst first with
    five above the floor, so only sort-then-cut gives the top three."""
    scores = {"a": 0.75, "b": 0.80, "c": 0.95, "d": 0.85, "e": 0.90}
    rows = [_row(slug) for slug in scores]
    embedder = _embedder_for({_row(slug): _at(score) for slug, score in scores.items()})

    result = suggest(embedder, TextVectorCache(), QUERY, rows)

    assert [s.row.role_slug for s in result] == ["c", "e", "d"]
    assert [s.score for s in result] == pytest.approx([0.95, 0.90, 0.85])


def test_equal_scores_are_ordered_by_role_slug() -> None:
    """Catches an order that follows the input for equal scores: the same
    question could then be answered in a different order on the next call."""
    rows = [_row("zeta"), _row("alpha"), _row("mid")]
    embedder = _embedder_for({row: _at(0.9) for row in rows})

    result = suggest(embedder, TextVectorCache(), QUERY, rows)

    assert [s.row.role_slug for s in result] == ["alpha", "mid", "zeta"]


def test_the_floor_is_inclusive() -> None:
    """Catches `>` where the floor is inclusive, and a floor that is a little
    off: one role sits at exactly 0.70 (offered), one at 0.6999 (not).

    The first vector pair is checked against the real cosine so the case cannot
    pass or fail on float luck."""
    at_floor = _row("at-floor")
    just_below = _row("just-below")
    exactly: Vector = (7.0, math.sqrt(51.0))
    below: Vector = (6999.0, math.sqrt(1e8 - 6999.0**2))
    assert cosine(QUERY_VECTOR, exactly) == SUGGESTION_FLOOR
    assert 0.6998 < cosine(QUERY_VECTOR, below) < SUGGESTION_FLOOR
    embedder = _embedder_for({at_floor: exactly, just_below: below})

    result = suggest(embedder, TextVectorCache(), QUERY, [just_below, at_floor])

    assert [s.row.role_slug for s in result] == ["at-floor"]


def test_nothing_above_the_floor_gives_an_empty_list() -> None:
    """Catches a ranker that always offers its best guess: roles that mean
    nothing like the words (cosine 0.0 and a negative one) must give no
    suggestion, so today's question is kept."""
    unrelated = _row("unrelated")
    opposite = _row("opposite")
    embedder = _embedder_for({unrelated: (0.0, 1.0), opposite: (-1.0, 0.0)})

    result = suggest(embedder, TextVectorCache(), QUERY, [unrelated, opposite])

    assert result == []


def test_a_suggestion_carries_its_row_and_its_score() -> None:
    """Catches a result that drops the row (the caller could not name the role)
    or reports a score from a different role."""
    row = _row("editor")
    embedder = _embedder_for({row: _at(0.8)})

    (only,) = suggest(embedder, TextVectorCache(), QUERY, [row])

    assert only.row == row
    assert only.score == pytest.approx(0.8)


# --- the cache ----------------------------------------------------------------


def test_role_texts_are_embedded_once_per_process() -> None:
    """Catches a ranker that re-embeds every role on every call (the cost the
    cache exists to remove), and one that skips the QUERY embed on the second
    call because it looked cached: the roles go through the model once over two
    calls, the query twice."""
    rows = [_row("editor"), _row("viewer")]
    embedder = _embedder_for({rows[0]: _at(0.9), rows[1]: _at(0.8)})
    cache = TextVectorCache()

    suggest(embedder, cache, QUERY, rows)
    suggest(embedder, cache, QUERY, rows)

    assert [embedder.embedded(role_text(row)) for row in rows] == [1, 1]
    assert embedder.embedded(QUERY) == 2


def test_the_cache_embeds_only_the_misses_in_one_batch() -> None:
    """Catches one model call per text (the batch is the saving), a batch that
    repeats texts already known, and vectors returned in the wrong order when
    known and new texts are mixed."""
    embedder = FakeEmbedder({"a": (1.0, 0.0), "b": (0.0, 1.0), "c": (1.0, 1.0)})
    cache = TextVectorCache()
    cache.vectors(embedder, ["a"])
    embedder.calls.clear()

    result = cache.vectors(embedder, ["b", "a", "c"])

    assert embedder.calls == [["b", "c"]]
    assert result == [(0.0, 1.0), (1.0, 0.0), (1.0, 1.0)]


def test_a_cache_hit_makes_no_model_call_at_all() -> None:
    """Catches a cache that calls `embed_dense` with an empty batch when every
    text is known: a model call with nothing to do still costs a round."""
    embedder = FakeEmbedder({"a": (1.0, 0.0)})
    cache = TextVectorCache()
    cache.vectors(embedder, ["a"])
    embedder.calls.clear()

    cache.vectors(embedder, ["a"])

    assert embedder.calls == []


def test_the_cache_drops_the_oldest_entry_over_its_cap() -> None:
    """Catches a cache that never evicts, one that evicts the NEWEST entry, and
    one that clears everything: with a cap of 2, adding a third text must lose
    exactly the first and keep the other two."""
    embedder = FakeEmbedder({}, default=(1.0, 0.0))
    cache = TextVectorCache(cap=2)
    cache.vectors(embedder, ["a", "b"])
    cache.vectors(embedder, ["c"])
    assert len(cache) == 2
    embedder.calls.clear()

    cache.vectors(embedder, ["b", "c"])
    assert embedder.calls == [], "the two newest texts were kept"
    cache.vectors(embedder, ["a"])
    assert embedder.calls == [["a"]], "the oldest text was the one dropped"


def test_the_default_cap_is_4096_entries_and_never_more() -> None:
    """Catches a default cap other than 4096 and a cache checked for size only
    between calls: one batch of 4097 new texts must still leave 4096."""
    embedder = FakeEmbedder({}, default=(1.0, 0.0))
    cache = TextVectorCache()

    cache.vectors(embedder, [f"fake text {index}" for index in range(CACHE_CAP + 1)])

    assert len(cache) == 4096


# --- the query check ----------------------------------------------------------


def test_query_length_is_checked_at_the_limit() -> None:
    """Catches `>=` where the cap is inclusive (200 must pass) and a cap of 201
    or more (201 must fail). The result of a passing call is the query itself,
    unchanged."""
    at_limit = "q" * 200

    assert check_suggest_query(at_limit) == at_limit
    with pytest.raises(InvalidSearchArgumentError) as raised:
        check_suggest_query("q" * 201)
    assert raised.value.code is ErrorCode.VALIDATION


@pytest.mark.parametrize("blank", ["", " ", "   ", "\n", "\t \r\n"])
def test_a_blank_query_is_refused(blank: str) -> None:
    """Catches a check that only looks at `len(value) == 0`: a query of spaces
    embeds as noise and would match roles by accident."""
    with pytest.raises(InvalidSearchArgumentError) as raised:
        check_suggest_query(blank)
    assert raised.value.code is ErrorCode.VALIDATION


def test_one_character_is_enough() -> None:
    """Catches a lower bound above one: the shortest legal query must pass."""
    assert check_suggest_query("x") == "x"
