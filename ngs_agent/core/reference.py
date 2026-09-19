"""Reference-sequence providers.

Left-alignment and any future reference-aware check need exactly one
capability: "give me bases ``start..end`` on this contig". That capability is
a protocol so a deployment can satisfy it with a local FASTA, an in-memory
test double, or a customer-hosted refget endpoint — without the normalization
code knowing or caring which.

The contract is strict about one thing: **unavailable means ``None``**. A
provider must never return a placeholder base (``N``) for a position it could
not read, because the normalizer would then happily "align" against fiction.
"""

from __future__ import annotations

from pathlib import Path

from ngs_agent.core.errors import CoreError


class ReferenceError(CoreError):
    """The reference source is malformed or its index disagrees with the FASTA."""


class InMemoryReference:
    """A reference built from explicit contig sequences.

    Intended for tests, golden cases, and tiny targeted regions. Coordinates
    are 1-based inclusive, matching VCF.
    """

    def __init__(self, contigs: dict[str, str], *, name: str = "in-memory") -> None:
        self.name = name
        self._contigs = {chrom: seq.upper() for chrom, seq in contigs.items()}

    def fetch(self, chromosome: str, start: int, end: int) -> str | None:
        sequence = self._contigs.get(chromosome)
        if sequence is None:
            return None
        if start < 1 or end < start or end > len(sequence):
            return None
        return sequence[start - 1 : end]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"InMemoryReference(contigs={sorted(self._contigs)}, name={self.name!r})"


class IndexedFastaReference:
    """Reference access from a FASTA plus its ``.fai`` index.

    Only the standard ``samtools faidx`` layout is supported
    (``name, length, offset, line_bases, line_width``). Reads are performed
    with ``seek``/``read`` so a 3 GB genome costs nothing to open.

    This implementation deliberately avoids a hard dependency on ``pysam`` so
    the signed classification path has no C-extension requirement. If
    ``pysam`` is present a deployment may substitute its own provider.
    """

    def __init__(self, fasta_path: Path | str, *, name: str | None = None) -> None:
        self.fasta_path = Path(fasta_path)
        self.index_path = self.fasta_path.with_suffix(self.fasta_path.suffix + ".fai")
        if not self.fasta_path.is_file():
            raise ReferenceError(f"Reference FASTA not found: {self.fasta_path}")
        if not self.index_path.is_file():
            raise ReferenceError(
                f"Reference index not found: {self.index_path}. Build it with "
                "`samtools faidx <fasta>`; NGS-Agent will not scan an unindexed genome."
            )
        self.name = name or self.fasta_path.name
        self._index = self._read_index()

    def _read_index(self) -> dict[str, tuple[int, int, int, int]]:
        index: dict[str, tuple[int, int, int, int]] = {}
        with self.index_path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                parts = line.split("\t")
                if len(parts) < 5:
                    raise ReferenceError(
                        f"{self.index_path}:{line_number}: expected 5 tab-separated fields, "
                        f"found {len(parts)}"
                    )
                try:
                    contig = parts[0]
                    length = int(parts[1])
                    offset = int(parts[2])
                    line_bases = int(parts[3])
                    line_width = int(parts[4])
                except ValueError as exc:
                    raise ReferenceError(f"{self.index_path}:{line_number}: {exc}") from exc
                if line_bases <= 0 or line_width <= 0 or line_width < line_bases:
                    raise ReferenceError(
                        f"{self.index_path}:{line_number}: inconsistent line_bases/line_width"
                    )
                index[contig] = (length, offset, line_bases, line_width)
        if not index:
            raise ReferenceError(f"{self.index_path} contains no contigs")
        return index

    def contigs(self) -> list[str]:
        return sorted(self._index)

    def fetch(self, chromosome: str, start: int, end: int) -> str | None:
        entry = self._index.get(chromosome)
        if entry is None:
            return None
        length, offset, line_bases, line_width = entry
        if start < 1 or end < start or end > length:
            return None
        # samtools faidx offsets are 0-based byte offsets into the FASTA body.
        first_line, first_col = divmod(start - 1, line_bases)
        last_line, last_col = divmod(end - 1, line_bases)
        byte_start = offset + first_line * line_width + first_col
        byte_end = offset + last_line * line_width + last_col + 1
        with self.fasta_path.open("rb") as handle:
            handle.seek(byte_start)
            raw = handle.read(byte_end - byte_start)
        return raw.replace(b"\n", b"").replace(b"\r", b"").decode("ascii").upper()


class NoReference:
    """Explicit "no reference available" provider.

    Exists so callers can pass a provider object and still get ``None`` for
    every fetch, rather than branching on ``None`` providers everywhere.
    """

    name = "none"

    def fetch(self, chromosome: str, start: int, end: int) -> str | None:  # noqa: ARG002
        return None


def build_reference(
    *,
    fasta: Path | str | None = None,
    contigs: dict[str, str] | None = None,
) -> InMemoryReference | IndexedFastaReference | None:
    """Construct a provider from CLI-style inputs. Returns ``None`` if neither."""
    if contigs:
        return InMemoryReference(contigs)
    if fasta:
        return IndexedFastaReference(fasta)
    return None
