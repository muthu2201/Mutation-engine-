"""Code novelty: MinHash signatures, novelty rejection and tabu basins.

LLM mutators love to propose the same idea twice. ShinkaEvolve showed that rejecting
near-duplicates *before* paying for evaluation improves sample efficiency. We need a
"code embedding" for that, and we use a dependency-free one that is good enough for
near-duplicate detection: **MinHash over token 4-shingles**. The fraction of equal
MinHash slots between two signatures is an unbiased estimate of the Jaccard similarity of
their shingle sets.

The same signatures implement the blueprint's **tabu-basin "tunneling function"**: once an
island converges, the signature of its elite is recorded as a basin centre, and later
candidates that are too similar to a known basin receive a selection penalty, pushing the
search to cross the barrier rather than re-polish the same optimum.
"""

from __future__ import annotations

import hashlib
import io
import re
import tokenize
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

_MASK64 = (1 << 64) - 1
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+|==|!=|<=|>=|->|[^\sA-Za-z0-9_]")


def code_tokens(source: str) -> list[str]:
    """Tokenise source, dropping comments/whitespace. Python sources use the real
    tokenizer (so comment-only edits are invisible); others use a generic regex."""
    try:
        toks = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.INDENT, tokenize.DEDENT, tokenize.ENDMARKER):
                continue
            toks.append(tok.string)
        return toks
    except (tokenize.TokenError, IndentationError, SyntaxError):
        stripped = re.sub(r"//[^\n]*|/\*.*?\*/|#[^\n]*", " ", source, flags=re.S)
        return _TOKEN_RE.findall(stripped)


def shingles(tokens: Sequence[str], k: int = 4) -> set[str]:
    if len(tokens) < k:
        return {" ".join(tokens)} if tokens else set()
    return {" ".join(tokens[i : i + k]) for i in range(len(tokens) - k + 1)}


def _h(s: str, seed: int) -> int:
    return int.from_bytes(hashlib.blake2b(s.encode(), digest_size=8, salt=seed.to_bytes(8, "little")).digest(), "little")


@dataclass(frozen=True)
class MinHash:
    slots: tuple[int, ...]

    @staticmethod
    def of_items(items: Iterable[str], num_perm: int = 64) -> MinHash:
        base = [_h(it, 0) for it in set(items)]
        if not base:
            return MinHash(tuple([_MASK64] * num_perm))
        # Universal hashing family h_i(x) = (a_i * x + b_i) mod 2^64 with fixed odd a_i.
        slots = []
        for i in range(num_perm):
            a = (_h(f"a{i}", 1) | 1) & _MASK64
            b = _h(f"b{i}", 2)
            slots.append(min(((a * x + b) & _MASK64) for x in base))
        return MinHash(tuple(slots))

    @staticmethod
    def of_code(source: str, num_perm: int = 64) -> MinHash:
        return MinHash.of_items(shingles(code_tokens(source)), num_perm)

    def jaccard(self, other: MinHash) -> float:
        if len(self.slots) != len(other.slots):
            raise ValueError("signature length mismatch")
        same = sum(1 for x, y in zip(self.slots, other.slots, strict=True) if x == y)
        return same / len(self.slots)

    def to_hex(self) -> str:
        return "".join(f"{s:016x}" for s in self.slots)

    @staticmethod
    def from_hex(text: str) -> MinHash:
        return MinHash(tuple(int(text[i : i + 16], 16) for i in range(0, len(text), 16)))


@dataclass
class NoveltyFilter:
    """Reject candidates whose signature is too similar to something already evaluated.

    ``threshold`` is a Jaccard similarity; 0.95 rejects near-identical rewrites while still
    admitting small, real edits (a one-line change in a 40-line function typically lands
    around 0.8-0.9).
    """

    threshold: float = 0.95
    seen: list[MinHash] = field(default_factory=list)

    def is_novel(self, sig: MinHash) -> bool:
        return all(sig.jaccard(s) < self.threshold for s in self.seen)

    def add(self, sig: MinHash) -> None:
        self.seen.append(sig)

    def max_similarity(self, sig: MinHash) -> float:
        return max((sig.jaccard(s) for s in self.seen), default=0.0)


@dataclass
class TabuBasins:
    """Basin centres of converged islands; candidates near them are penalised."""

    radius: float = 0.85  # Jaccard similarity above which a candidate is "inside" a basin
    centres: list[MinHash] = field(default_factory=list)

    def add(self, sig: MinHash) -> None:
        if all(sig.jaccard(c) < 0.99 for c in self.centres):
            self.centres.append(sig)

    def penalty(self, sig: MinHash) -> float:
        """0 outside every basin, rising linearly to 1 at the centre."""
        worst = 0.0
        for c in self.centres:
            sim = sig.jaccard(c)
            if sim >= self.radius:
                worst = max(worst, (sim - self.radius) / max(1e-9, 1.0 - self.radius))
        return worst
