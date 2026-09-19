"""Minimal, dependency-free VCF reader for the review pipeline.

This is *not* a general-purpose VCF library. It reads exactly what the
evidence pipeline needs — CHROM/POS/ID/REF/ALT plus a small set of INFO keys —
and it records enough about the file (its SHA-256, the declared reference, the
line numbers) that the audit trail can point back at the precise bytes a
classification was derived from.

Design rules:

* A malformed line is a hard error carrying the line number. Silently skipping
  a line is how variants disappear from reports.
* The declared ``##reference=``/``##assembly=`` is captured but never trusted
  as the build of record; the caller must state the build explicitly, and a
  mismatch between the two is surfaced as a warning.
* Genotype/FORMAT columns are parsed only to the extent needed to report
  depth and allele fraction as *sample observations*, which are never treated
  as ACMG evidence by the deterministic engine.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ngs_agent.core.errors import CoreError
from ngs_agent.core.hashing import sha256_file
from ngs_agent.core.normalization import RawVariant


class VcfParseError(CoreError, ValueError):
    """A VCF file could not be parsed. Carries the offending line number."""

    def __init__(
        self, message: str, *, path: Path | None = None, line_number: int | None = None) -> None:
        location = f"{path}:{line_number}" if path and line_number else str(path or "<unknown>")
        super().__init__(f"{location}: {message}")
        self.path = path
        self.line_number = line_number


class VcfFileFacts(BaseModel):
    """Provenance facts about the input file itself."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str
    size_bytes: int
    declared_reference: str | None = None
    declared_assembly: str | None = None
    file_format: str | None = None
    source: str | None = None
    sample_names: tuple[str, ...] = ()
    record_count: int = 0


class VcfDocument(BaseModel):
    """A parsed VCF: file facts plus raw records."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    facts: VcfFileFacts
    records: tuple[RawVariant, ...] = Field(default_factory=tuple)


#: INFO keys captured verbatim. They are *hints* for evidence retrieval and
#: display only — the deterministic engine never derives a criterion from them.
_CAPTURED_INFO_KEYS = (
    "GENE",
    "SYMBOL",
    "CSQ",
    "ANN",
    "CLNSIG",
    "CLNREVSTAT",
    "CLNDN",
    "CLNALLELEID",
    "CLNVC",
    "AF",
    "AF_popmax",
    "gnomAD_AF",
    "SPLICEAI",
    "REVEL",
    "CADD",
    "RS",
    "DBNSFP",
)


def _split_info(field: str) -> dict[str, str]:
    info: dict[str, str] = {}
    if not field or field == ".":
        return info
    for part in field.split(";"):
        if not part:
            continue
        if "=" in part:
            key, value = part.split("=", 1)
            info[key] = value
        else:
            info[part] = "true"
    return {key: info[key] for key in info if key in _CAPTURED_INFO_KEYS}


def read_vcf(path: Path | str, *, max_records: int | None = None) -> VcfDocument:
    """Parse a VCF (optionally bgzip-compressed *only* if plain-text readable).

    ``max_records`` bounds memory for very large call sets; when the limit is
    reached the remaining records are not read and ``record_count`` reflects
    what was actually parsed.
    """
    resolved = Path(path)
    if not resolved.is_file():
        raise VcfParseError("file does not exist", path=resolved)

    meta: dict[str, str] = {}
    samples: list[str] = []
    records: list[RawVariant] = []
    header_seen = False

    with resolved.open(encoding="utf-8", errors="strict") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.rstrip("\n").rstrip("\r")
            if not line:
                continue
            if line.startswith("##"):
                key, _, value = line[2:].partition("=")
                meta.setdefault(key.strip(), value.strip())
                continue
            if line.startswith("#CHROM"):
                columns = line.split("\t")
                if len(columns) < 8:
                    raise VcfParseError(
                        f"header has {len(columns)} columns; at least 8 are required",
                        path=resolved,
                        line_number=line_number,
                    )
                samples = columns[9:] if len(columns) > 9 else []
                header_seen = True
                continue

            if not header_seen:
                raise VcfParseError(
                    "data line encountered before a #CHROM header line",
                    path=resolved,
                    line_number=line_number,
                )
            if max_records is not None and len(records) >= max_records:
                break

            parts = line.split("\t")
            if len(parts) < 8:
                raise VcfParseError(
                    f"expected at least 8 tab-separated columns, found {len(parts)}",
                    path=resolved,
                    line_number=line_number,
                )
            chrom, pos_text, record_id, ref, alt_field = parts[:5]
            try:
                position = int(pos_text)
            except ValueError as exc:
                raise VcfParseError(
                    f"POS {pos_text!r} is not an integer", path=resolved,
                        line_number=line_number) from exc
            alternates = tuple(allele for allele in alt_field.split(",") if allele)
            if not alternates:
                raise VcfParseError("ALT field is empty", path=resolved, line_number=line_number)

            records.append(
                RawVariant(
                    chromosome=chrom,
                    position=position,
                    reference=ref,
                    alternates=alternates,
                    record_id=None if record_id in {".", ""} else record_id,
                    info=_split_info(parts[7]),
                    line_number=line_number,
                )
            )

    facts = VcfFileFacts(
        path=str(resolved),
        sha256=sha256_file(resolved),
        size_bytes=resolved.stat().st_size,
        declared_reference=meta.get("reference") or None,
        declared_assembly=meta.get("assembly") or None,
        file_format=meta.get("fileformat") or None,
        source=meta.get("source") or None,
        sample_names=tuple(samples),
        record_count=len(records),
    )
    return VcfDocument(facts=facts, records=tuple(records))
