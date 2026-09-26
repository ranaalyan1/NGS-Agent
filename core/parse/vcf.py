"""Read-only parser for single-sample VCF call-quality evidence."""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

from ..util import sha256_file

MAX_SAMPLES = 1


class VCFParseError(ValueError):
    """The input declares VCF but has no interpretable record structure."""


def parse_vcf(path: str | Path) -> dict[str, Any]:
    """Read a VCF or gzip-compressed VCF without indexed random access."""
    source = Path(path)
    try:
        opener = gzip.open if source.suffix.lower() == ".gz" else open
        with opener(source, "rt", encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except (OSError, EOFError) as exc:
        raise VCFParseError("the file could not be read") from exc

    if not lines or not any(line.startswith("##fileformat=VCF") for line in lines[:10]):
        raise VCFParseError("missing VCF fileformat declaration")

    header_index = next(
        (index for index, line in enumerate(lines) if line.startswith("#CHROM\t")),
        None,
    )
    if header_index is None:
        raise VCFParseError("missing #CHROM header")

    samples = lines[header_index].split("\t")[9:]
    if len(samples) > MAX_SAMPLES:
        return {
            "unsupported": (
                f"{len(samples)} samples exceed the supported maximum of {MAX_SAMPLES}"
            ),
            "lines": lines,
        }

    records: list[tuple[int, list[str]]] = []
    for line_number, line in enumerate(lines[header_index + 1 :], header_index + 2):
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 8:
            raise VCFParseError(f"malformed record at line {line_number}")
        info = fields[7]
        alternate = fields[4]
        if any(allele in ("<NON_REF>", "<*>") for allele in alternate.split(",")) or (
            "END=" in info and alternate == "<NON_REF>"
        ):
            return {
                "unsupported": "gVCF reference blocks are recognised, not judged",
                "lines": lines,
            }
        if "," in alternate:
            return {
                "unsupported": (
                    "multiallelic records require normalisation and are recognised, not judged"
                ),
                "lines": lines,
            }
        reference = fields[3]
        if (
            len(reference) != len(alternate)
            and len(reference) > 1
            and len(alternate) > 1
            and (reference[0] == alternate[0] or reference[-1] == alternate[-1])
        ):
            return {
                "unsupported": (
                    "a non-minimal indel representation was detected; normalisation is required"
                ),
                "lines": lines,
            }
        records.append((line_number, fields))

    if not records:
        raise VCFParseError("VCF contains no variant records")

    return {
        "lines": lines,
        "header_line": header_index + 1,
        "records": records,
        "samples": samples,
        "source_sha256": sha256_file(source),
        "path": str(source),
    }
