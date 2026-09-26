"""Station 10 call-quality rules; no pathogenicity or gene interpretation."""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Any

from ..models import (
    DECISION_HEALTHY,
    DECISION_REVIEW,
    SEVERITY_WARN,
    Finding,
    Receipt,
)
from ..version import RULESET_VERSION

# Thresholds for every VCF rule live in this module.
DEPTH_LOW = 10
DEPTH_LOW_FRACTION_WARN = 0.20
MISSING_GT_FRACTION_WARN = 0.10
TITV_EXTREME_LOW = 1.0
TITV_EXTREME_HIGH = 3.0
TITV_MIN_SUBSTITUTIONS = 10
HET_HOM_MIN_SITES = 10
HET_HOM_LOW = 0.10
HET_HOM_HIGH = 5.0
PASS_FRACTION_WARN = 0.80

TRANSITIONS = {"A": "G", "G": "A", "C": "T", "T": "C"}
TITV_CONTEXT = (
    "Ti/Tv expectations vary between whole-genome and exome data; no assay type is inferred."
)


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record_values(facts: dict[str, Any]) -> dict[str, Any]:
    records = facts["records"]
    depths: list[tuple[int, int]] = []
    missing = called = heterozygous = homozygous = 0
    transitions = transversions = passed = unfiltered = 0
    missing_lines: list[int] = []

    for line_number, fields in records:
        info = dict(item.split("=", 1) for item in fields[7].split(";") if "=" in item)
        depth = None
        if len(fields) > 9 and "DP" in fields[8].split(":"):
            keys = fields[8].split(":")
            values = fields[9].split(":")
            try:
                depth = int(values[keys.index("DP")])
            except (ValueError, IndexError):
                pass
        if depth is None:
            try:
                depth = int(info["DP"])
            except (KeyError, ValueError):
                pass
        if depth is not None:
            depths.append((depth, line_number))

        if fields[6] == "PASS":
            passed += 1
        elif fields[6] in (".", ""):
            unfiltered += 1

        if len(fields) > 9 and "GT" in fields[8].split(":"):
            keys = fields[8].split(":")
            values = fields[9].split(":")
            try:
                genotype = values[keys.index("GT")].replace("|", "/")
            except IndexError:
                genotype = "./."
            if "." in genotype or not genotype:
                missing += 1
                missing_lines.append(line_number)
            else:
                called += 1
                alleles = genotype.split("/")
                if len(alleles) == 2:
                    if alleles[0] == alleles[1]:
                        homozygous += 1
                    else:
                        heterozygous += 1

        reference, alternate = fields[3].upper(), fields[4].upper()
        if len(reference) == len(alternate) == 1 and reference in "ACGT" and alternate in "ACGT":
            if TRANSITIONS.get(reference) == alternate:
                transitions += 1
            else:
                transversions += 1

    n_sites = len(records)
    return {
        "records": records,
        "depths": depths,
        "missing": missing,
        "called": called,
        "missing_lines": missing_lines,
        "heterozygous": heterozygous,
        "homozygous": homozygous,
        "transitions": transitions,
        "transversions": transversions,
        "passed": passed,
        "unfiltered": unfiltered,
        "n_sites": n_sites,
    }


def _finding(
    rule_id: str,
    title: str,
    what: str,
    meaning: str,
    action: str,
    facts: dict[str, Any],
    line_numbers: list[int],
    metric: str,
    threshold: str,
) -> Finding:
    digest = facts["source_sha256"]
    lines = line_numbers[:8]
    receipts = [
        Receipt(
            source=f"rule:{rule_id}",
            version=RULESET_VERSION,
            timestamp=_now(),
            locator="core/rules/vcf_rules.py",
            detail=f"Threshold: {threshold}",
        ),
        Receipt(
            source=f"file:{digest[:12]}",
            version=f"sha256:{digest}",
            timestamp=_now(),
            locator=f"{facts['path']}:line={','.join(map(str, lines))}",
            detail=metric,
        ),
    ]
    return Finding(
        id=rule_id,
        title=title,
        severity=SEVERITY_WARN,
        what=what,
        meaning=meaning,
        action=action,
        receipts=receipts,
        details={"metric": metric, "threshold": threshold, "line_numbers": lines},
    )


def evaluate_vcf(facts: dict[str, Any]) -> list[Finding]:
    """Evaluate QC-VCF-01 through QC-VCF-05 from parseable records."""
    values = _record_values(facts)
    records = values["records"]
    n_sites = values["n_sites"]
    findings: list[Finding] = []
    depths = values["depths"]

    if depths:
        median_depth = median(depth for depth, _ in depths)
        low_depths = [(depth, line) for depth, line in depths if depth < DEPTH_LOW]
        low_fraction = len(low_depths) / n_sites
        if median_depth < DEPTH_LOW or low_fraction >= DEPTH_LOW_FRACTION_WARN:
            cited_lines = [line for _, line in low_depths] or [line for _, line in depths]
            findings.append(
                _finding(
                    "QC-VCF-01",
                    "Low or uneven call depth",
                    f"Median depth is {median_depth:g}; {len(low_depths)} of {n_sites} sites ({low_fraction:.1%}) are below {DEPTH_LOW}.",
                    "Call confidence may be reduced at these sites; depth alone does not determine variant truth.",
                    "Review the caller's depth settings and the affected sites.",
                    facts,
                    cited_lines,
                    f"median={median_depth:g}; below_fraction={low_fraction:.4f}",
                    f"median >= {DEPTH_LOW}; fraction below {DEPTH_LOW} < {DEPTH_LOW_FRACTION_WARN}",
                )
            )

    genotype_total = values["missing"] + values["called"]
    if genotype_total and values["missing"] / genotype_total > MISSING_GT_FRACTION_WARN:
        fraction = values["missing"] / genotype_total
        findings.append(
            _finding(
                "QC-VCF-02",
                "High genotype missingness",
                f"{values['missing']} of {genotype_total} genotypes ({fraction:.1%}) are missing.",
                "Missing calls reduce the amount of genotype data available for downstream analysis.",
                "Review sample call rate and caller filters.",
                facts,
                values["missing_lines"],
                f"missing_fraction={fraction:.4f}",
                f"missing fraction <= {MISSING_GT_FRACTION_WARN}",
            )
        )

    total_substitutions = values["transitions"] + values["transversions"]
    if total_substitutions >= TITV_MIN_SUBSTITUTIONS and values["transversions"] > 0:
        titv = values["transitions"] / values["transversions"]
        if titv < TITV_EXTREME_LOW or titv > TITV_EXTREME_HIGH:
            findings.append(
                _finding(
                    "QC-VCF-03",
                    "Extreme transition/transversion balance",
                    f"Observed Ti/Tv is {titv:.3g} across {total_substitutions} single-base substitutions.",
                    f"{TITV_CONTEXT} Only extreme outliers are flagged.",
                    "Check the call set and assay context before downstream use.",
                    facts,
                    [line for line, _ in records],
                    f"Ti/Tv={titv:.4g}; transitions={values['transitions']}; transversions={values['transversions']}",
                    f"{TITV_EXTREME_LOW} <= Ti/Tv <= {TITV_EXTREME_HIGH}",
                )
            )

    heterozygous = values["heterozygous"]
    homozygous = values["homozygous"]
    called_diploid = heterozygous + homozygous
    if (
        called_diploid >= HET_HOM_MIN_SITES
        and homozygous > 0
        and (heterozygous / homozygous < HET_HOM_LOW or heterozygous / homozygous > HET_HOM_HIGH)
    ):
        ratio = heterozygous / homozygous
        findings.append(
            _finding(
                "QC-VCF-04",
                "Heterozygous/homozygous ratio outlier",
                f"The het/hom ratio is {ratio:.3g} across {called_diploid} called diploid genotypes.",
                "This is a call-set QC outlier only; it does not establish biological cause.",
                "Review sample identity, ploidy assumptions, and caller settings.",
                facts,
                [line for line, _ in records],
                f"het={heterozygous}; hom={homozygous}; ratio={ratio:.4g}",
                f"{HET_HOM_LOW} <= het/hom <= {HET_HOM_HIGH}",
            )
        )

    passed_fraction = values["passed"] / n_sites
    unfiltered_fraction = values["unfiltered"] / n_sites
    nonpass_filtered = n_sites - values["passed"] - values["unfiltered"]
    if passed_fraction < PASS_FRACTION_WARN or values["unfiltered"]:
        findings.append(
            _finding(
                "QC-VCF-05",
                "Filter profile needs review",
                (
                    f"{values['passed']} of {n_sites} sites ({passed_fraction:.1%}) are PASS; "
                    f"{values['unfiltered']} ({unfiltered_fraction:.1%}) are unfiltered/untagged; "
                    f"{nonpass_filtered} have other FILTER labels."
                ),
                "Filter labels describe caller filtering; unfiltered or non-PASS records need explicit review.",
                "Review FILTER definitions and retain the caller's documented filtering policy.",
                facts,
                [line for line, _ in records],
                f"PASS_fraction={passed_fraction:.4f}; unfiltered_fraction={unfiltered_fraction:.4f}; nonpass_filtered={nonpass_filtered}",
                f"PASS fraction >= {PASS_FRACTION_WARN}; no unfiltered sites",
            )
        )
    return findings


def decide_vcf(findings: list[Finding]) -> tuple[str, str]:
    if findings:
        return (
            DECISION_REVIEW,
            "The VCF call-quality metrics need review; this is not a judgment about variant truth.",
        )
    return (
        DECISION_HEALTHY,
        "VCF QC healthy: no configured call-quality outliers were detected; this says nothing about pathogenicity or variant truth.",
    )


def summarize_vcf(facts: dict[str, Any]) -> dict[str, Any]:
    """Expose observed QC metrics without assigning assay or biological meaning."""
    values = _record_values(facts)
    depths = values["depths"]
    n_sites = values["n_sites"]
    genotype_total = values["missing"] + values["called"]
    homozygous = values["homozygous"]
    return {
        "depth_median": median(depth for depth, _ in depths) if depths else None,
        "depth_fraction_below_threshold": (
            sum(depth < DEPTH_LOW for depth, _ in depths) / n_sites if depths else None
        ),
        "missing_genotype_fraction": (
            values["missing"] / genotype_total if genotype_total else None
        ),
        "titv": (
            values["transitions"] / values["transversions"] if values["transversions"] else None
        ),
        "titv_transitions": values["transitions"],
        "titv_transversions": values["transversions"],
        "het_hom_ratio": (values["heterozygous"] / homozygous if homozygous else None),
        "pass_fraction": values["passed"] / n_sites if n_sites else None,
        "unfiltered_sites": values["unfiltered"],
        "unfiltered_fraction": values["unfiltered"] / n_sites if n_sites else None,
        "titv_context": TITV_CONTEXT,
    }


def unjudged_vcf_metrics(facts: dict[str, Any]) -> list[str]:
    """Template statements for metrics lacking enough observed input evidence."""
    values = _record_values(facts)
    messages: list[str] = []
    if not values["depths"]:
        messages.append("No usable depth field was present, so depth distribution was not judged.")
    elif len(values["depths"]) < values["n_sites"]:
        messages.append(
            f"Depth was absent or unparseable at {values['n_sites'] - len(values['depths'])} sites, so the depth profile is incomplete."
        )
    genotype_total = values["called"] + values["missing"]
    if not genotype_total:
        messages.append(
            "No genotype field was present, so missingness and het/hom balance were not judged."
        )
    elif genotype_total < values["n_sites"]:
        messages.append(
            f"Genotypes were absent or unparseable at {values['n_sites'] - genotype_total} sites, so genotype QC is incomplete."
        )
    elif values["heterozygous"] + values["homozygous"] < HET_HOM_MIN_SITES:
        n = values["heterozygous"] + values["homozygous"]
        messages.append(
            f"Only {n} called diploid genotypes were present; the het/hom ratio needs at least {HET_HOM_MIN_SITES}."
        )
    elif not values["homozygous"]:
        messages.append("No homozygous calls were present, so the het/hom ratio is undefined.")
    substitutions = values["transitions"] + values["transversions"]
    if substitutions < TITV_MIN_SUBSTITUTIONS:
        messages.append(
            f"Only {substitutions} informative single-base substitutions were present; Ti/Tv needs at least {TITV_MIN_SUBSTITUTIONS}."
        )
    elif not values["transversions"]:
        messages.append("No transversions were observed, so Ti/Tv could not be computed.")
    return messages


def metric_receipts(facts: dict[str, Any]) -> list[Receipt]:
    """Receipt every reported metric, including non-finding/healthy metrics."""
    records = facts["records"]
    line_start = records[0][0]
    line_end = records[-1][0]
    digest = facts["source_sha256"]
    thresholds = {
        "QC-VCF-01": f"depth {DEPTH_LOW}; low-depth fraction {DEPTH_LOW_FRACTION_WARN}",
        "QC-VCF-02": f"missing genotype fraction {MISSING_GT_FRACTION_WARN}",
        "QC-VCF-03": f"extreme Ti/Tv outside {TITV_EXTREME_LOW} to {TITV_EXTREME_HIGH}",
        "QC-VCF-04": f"het/hom outside {HET_HOM_LOW} to {HET_HOM_HIGH}",
        "QC-VCF-05": f"PASS fraction {PASS_FRACTION_WARN}",
    }
    receipts: list[Receipt] = []
    for rule_id, threshold in thresholds.items():
        receipts.extend(
            [
                Receipt(
                    source=f"rule:{rule_id}",
                    version=RULESET_VERSION,
                    timestamp=_now(),
                    locator="core/rules/vcf_rules.py",
                    detail=f"Metric evaluated; {threshold}",
                ),
                Receipt(
                    source=f"file:{digest[:12]}",
                    version=f"sha256:{digest}",
                    timestamp=_now(),
                    locator=f"{facts['path']}:lines={line_start}-{line_end}",
                    detail=f"{len(records)} VCF records included in metric evaluation",
                ),
            ]
        )
    return receipts
