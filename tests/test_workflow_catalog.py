"""Tests for the workflow catalogue and natural-language intent classification."""

from __future__ import annotations

import pytest

from ngs_agent.bioinformatics.workflows import (
    DEFAULT_WORKFLOW,
    WORKFLOWS,
    WorkflowInferenceError,
    get_workflow,
    infer_workflow,
)


class TestGetWorkflow:
    def test_known_workflows(self) -> None:
        assert get_workflow("rnaseq").key == "rnaseq"
        assert get_workflow("variant").key == "variant"
        assert get_workflow("qc").key == "qc"

    def test_aliases(self) -> None:
        assert get_workflow("rna").key == "rnaseq"
        assert get_workflow("wgs").key == "variant"
        assert get_workflow("WGS").key == "variant"

    def test_unknown_workflow_raises(self) -> None:
        with pytest.raises(WorkflowInferenceError, match="Unknown workflow"):
            get_workflow("definitely-not-a-workflow")


class TestInferWorkflow:
    def test_rnaseq_intent(self) -> None:
        inference = infer_workflow("run differential expression on my RNA-seq data")
        assert inference.workflow.key == "rnaseq"
        assert inference.basis == "intent"
        assert inference.matched_keywords

    def test_variant_intent(self) -> None:
        inference = infer_workflow(
            "call SNVs and indoles from whole exome data".replace("indoles", "indels")
        )
        assert inference.workflow.key == "variant"

    def test_qc_intent(self) -> None:
        inference = infer_workflow("just run fastqc quality control")
        assert inference.workflow.key == "qc"

    def test_file_evidence_breaks_ties(self) -> None:
        # "sequence data" is ambiguous, but a GTF on disk is RNA-Seq evidence.
        inference = infer_workflow("process my sequence data", evidence_files=["data/genes.gtf"])
        assert inference.workflow.key == "rnaseq"
        assert inference.basis == "file-evidence"

    def test_vcf_evidence_is_variant(self) -> None:
        inference = infer_workflow("analyze this", evidence_files=["out.vcf", "out.vcf.gz"])
        assert inference.workflow.key == "variant"

    def test_ambiguous_without_evidence_raises_when_no_default(self) -> None:
        # Matches both rnaseq and variant equally.
        with pytest.raises(WorkflowInferenceError):
            infer_workflow("rna-seq and variant comparison", allow_default=False)

    def test_unmatched_falls_back_to_default(self) -> None:
        inference = infer_workflow("do science to it")
        assert inference.workflow.key == DEFAULT_WORKFLOW
        assert inference.basis == "default"

    def test_every_workflow_has_required_tools(self) -> None:
        for spec in WORKFLOWS.values():
            assert spec.required_tools, f"{spec.key} must declare required tools"
            assert spec.label
            assert spec.description
