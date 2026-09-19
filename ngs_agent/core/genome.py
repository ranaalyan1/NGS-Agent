"""Genome build and chromosome identity.

Everything downstream depends on the genome build being *explicit*. A bare
``chr17:43082434`` is not a variant; it is a coordinate in an unstated
assembly, and the same coordinate means a different allele in GRCh37 and
GRCh38. This module refuses to guess.
"""

from __future__ import annotations

from enum import Enum

from ngs_agent.core.errors import GenomeBuildError

#: RefSeq chromosome accessions per build. Used to emit SPDI and genomic HGVS.
#: GRCh38 = GCF_000001405.40 (chr accessions are stable across patches);
#: GRCh37 = GCF_000001405.25.
GRCH38_ACCESSIONS: dict[str, str] = {
    "1": "NC_000001.11",
    "2": "NC_000002.12",
    "3": "NC_000003.12",
    "4": "NC_000004.12",
    "5": "NC_000005.10",
    "6": "NC_000006.12",
    "7": "NC_000007.14",
    "8": "NC_000008.11",
    "9": "NC_000009.12",
    "10": "NC_000010.11",
    "11": "NC_000011.10",
    "12": "NC_000012.12",
    "13": "NC_000013.11",
    "14": "NC_000014.9",
    "15": "NC_000015.10",
    "16": "NC_000016.10",
    "17": "NC_000017.11",
    "18": "NC_000018.10",
    "19": "NC_000019.10",
    "20": "NC_000020.11",
    "21": "NC_000021.9",
    "22": "NC_000022.11",
    "X": "NC_000023.11",
    "Y": "NC_000024.10",
    "MT": "NC_012920.1",
}

GRCH37_ACCESSIONS: dict[str, str] = {
    "1": "NC_000001.10",
    "2": "NC_000002.11",
    "3": "NC_000003.11",
    "4": "NC_000004.11",
    "5": "NC_000005.9",
    "6": "NC_000006.11",
    "7": "NC_000007.13",
    "8": "NC_000008.10",
    "9": "NC_000009.11",
    "10": "NC_000010.10",
    "11": "NC_000011.9",
    "12": "NC_000012.11",
    "13": "NC_000013.10",
    "14": "NC_000014.8",
    "15": "NC_000015.9",
    "16": "NC_000016.9",
    "17": "NC_000017.10",
    "18": "NC_000018.9",
    "19": "NC_000019.9",
    "20": "NC_000020.10",
    "21": "NC_000021.8",
    "22": "NC_000022.10",
    "X": "NC_000023.10",
    "Y": "NC_000024.9",
    "MT": "NC_012920.1",
}

#: Accepted spellings of a chromosome name, mapped to the canonical form.
_CHROMOSOME_ALIASES: dict[str, str] = {
    "m": "MT",
    "chrm": "MT",
    "chrmt": "MT",
    "23": "X",
    "24": "Y",
}


class GenomeBuild(str, Enum):
    """The two human assemblies NGS-Agent supports.

    There is intentionally no ``UNKNOWN`` member: an unspecified build is an
    error, not a state the engine can carry forward.
    """

    GRCH37 = "GRCh37"
    GRCH38 = "GRCh38"

    @property
    def assembly_accession(self) -> str:
        return _ASSEMBLY_ACCESSIONS[self]

    @property
    def accessions(self) -> dict[str, str]:
        return GRCH38_ACCESSIONS if self is GenomeBuild.GRCH38 else GRCH37_ACCESSIONS


_ASSEMBLY_ACCESSIONS: dict[GenomeBuild, str] = {
    GenomeBuild.GRCH37: "GCF_000001405.25",
    GenomeBuild.GRCH38: "GCF_000001405.40",
}

_BUILD_ALIASES: dict[str, GenomeBuild] = {
    "grch37": GenomeBuild.GRCH37,
    "hg19": GenomeBuild.GRCH37,
    "b37": GenomeBuild.GRCH37,
    "37": GenomeBuild.GRCH37,
    "gcf_000001405.25": GenomeBuild.GRCH37,
    "gcf_000001405.1": GenomeBuild.GRCH37,
    "grch38": GenomeBuild.GRCH38,
    "hg38": GenomeBuild.GRCH38,
    "b38": GenomeBuild.GRCH38,
    "38": GenomeBuild.GRCH38,
    "gcf_000001405.40": GenomeBuild.GRCH38,
    "gcf_000001405.39": GenomeBuild.GRCH38,
    "gcf_000001405.38": GenomeBuild.GRCH38,
    "gcf_000001405.26": GenomeBuild.GRCH38,
}


def parse_genome_build(value: str | GenomeBuild | None) -> GenomeBuild:
    """Resolve a build label. Raises rather than defaulting.

    Accepts common aliases (``hg19``/``b37``/``37``, ``hg38``/``b38``/``38``)
    and RefSeq assembly accessions, because a VCF ``##reference=`` or
    ``##assembly=`` line usually carries one of those forms.
    """
    if isinstance(value, GenomeBuild):
        return value
    if value is None or not str(value).strip():
        raise GenomeBuildError(
            "Genome build is required and must be explicit. Supply GRCh37 or GRCh38 "
            "(aliases hg19/b37 and hg38/b38 are accepted). NGS-Agent never assumes a build."
        )
    key = str(value).strip().lower().replace(" ", "")
    build = _BUILD_ALIASES.get(key)
    if build is None:
        raise GenomeBuildError(
            f"Unrecognized genome build {value!r}. Supported: GRCh37 (hg19/b37), GRCh38 (hg38/b38)."
        )
    return build


def normalize_chromosome(raw: str) -> str:
    """Canonicalize a chromosome/contig name.

    ``chr17``, ``17``, ``CHR17`` all resolve to ``17``; ``chrM``, ``MT``, ``M``
    resolve to ``MT``; ``23``/``24`` resolve to ``X``/``Y``. Non-primary
    contigs (``GL000220.1``, ``chrUn_*``, decoys, patches) are passed through
    unchanged and flagged by the caller — NGS-Agent does not classify variants
    on non-primary contigs and says so.
    """
    if raw is None:
        raise GenomeBuildError("Chromosome is required.")
    text = str(raw).strip()
    if not text or text == ".":
        raise GenomeBuildError("Chromosome must not be empty or '.'.")
    lowered = text.lower()
    if lowered in _CHROMOSOME_ALIASES:
        return _CHROMOSOME_ALIASES[lowered]
    stripped = text[3:] if lowered.startswith("chr") else text
    stripped = stripped.strip()
    if stripped.lower() in {"m", "mt"}:
        return "MT"
    if stripped.upper() in GRCH38_ACCESSIONS:
        return stripped.upper()
    if stripped.isdigit():
        number = int(stripped)
        if 1 <= number <= 22:
            return str(number)
        if number in (23, 24):
            return _CHROMOSOME_ALIASES[str(number)]
        raise GenomeBuildError(
            f"Chromosome {raw!r} is outside the primary assembly (1-22, X, Y, MT).")
    # Non-primary contig: return as-is so the caller can record it and refuse.
    return text


def is_primary_contig(chromosome: str) -> bool:
    """True for 1-22, X, Y, MT — the contigs NGS-Agent will classify on."""
    return chromosome in GRCH38_ACCESSIONS


def accession_for(build: GenomeBuild, chromosome: str) -> str | None:
    """RefSeq accession for a canonical chromosome in ``build``, or ``None``."""
    return build.accessions.get(chromosome)
