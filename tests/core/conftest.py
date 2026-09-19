"""Fixtures for the evidence-backed review core.

Nothing here talks to the network. Every fixture is either a hand-written
structured observation (which is what an adapter would have produced) or a
recording bundled with the package. If a test needs ClinVar it uses the recorded
adapter, so a golden result cannot drift when NCBI updates.
"""

from __future__ import annotations

import pytest

from ngs_agent.core.acmg.engine import AcmgEngine
from ngs_agent.core.genome import GenomeBuild
from ngs_agent.core.normalization import normalize_variant

# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------

#: BRCA1 c.4327C>T p.Arg1443Ter -- GRCh38 chr17:43082434 G>A. ClinVar
#: VCV000017675, Pathogenic, reviewed by expert panel.
BRCA1_R1443TER = dict(
    genome_build=GenomeBuild.GRCH38,
    chromosome="17",
    position=43082434,
    reference="G",
    alternate="A",
)

#: BRCA1 c.4327C>G p.Arg1443Gly -- same position, same dbSNP id, OPPOSITE
#: classification (VCV000017676, Benign, expert panel). The golden test that
#: proves locus != allele.
BRCA1_R1443GLY = {**BRCA1_R1443TER, "alternate": "C"}

#: Not in ClinVar at all. Must abstain, never "benign by absence".
BRCA1_R1443_UNRECORDED = {**BRCA1_R1443TER, "alternate": "T"}


@pytest.fixture
def brca1_nonsense():
    """GRCh38 chr17:43082434 G>A -- recorded as Pathogenic by an expert panel."""
    return normalize_variant(**BRCA1_R1443TER)


@pytest.fixture
def brca1_missense_locus():
    """GRCh38 chr17:43082434 G>C -- same locus, recorded as Benign."""
    return normalize_variant(**BRCA1_R1443GLY)


@pytest.fixture
def brca1_unrecorded():
    """GRCh38 chr17:43082434 G>T -- no ClinVar record at all."""
    return normalize_variant(**BRCA1_R1443_UNRECORDED)


@pytest.fixture
def engine() -> AcmgEngine:
    """The default rule set: ACMG/AMP 2015 exactly as published.

    Tests that assert ClinGen SVI behaviour must use :func:`svi_engine` and say
    so. Relying on the default would silently change meaning if the default rule
    set ever changed.
    """
    return AcmgEngine()


@pytest.fixture
def svi_engine() -> AcmgEngine:
    """ACMG/AMP 2015 with the ClinGen SVI 2020 modifiers applied."""
    return AcmgEngine(rule_set="acmg-amp-2015+clingen-svi-2020")
