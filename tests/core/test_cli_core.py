"""CLI tests for the signed classification path.

The CLI is an interface to the same versioned contract as every other surface,
so these tests check the *contract of the command line*: exit codes a pipeline
can branch on, banners that cannot be piped away, refusals that are legible, and
the guarantee that no command silently contacts the network.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from ngs_agent.cli import main
from ngs_agent.core.cli import REVIEW_COMMANDS

DEMO_VCF = "demo_data/review_demo.vcf"

#: Exit codes are part of the interface: a pipeline must be able to branch on
#: "this run abstained" without parsing prose.
EXIT_OK = 0
EXIT_USAGE = 2
EXIT_ABSTAINED = 3
EXIT_REPLAY_DIVERGED = 4


@pytest.fixture
def runner() -> CliRunner:
    """Click 8.2+ keeps ``result.stdout`` and ``result.stderr`` separate.

    That separation is exactly the property under test: stdout is the payload a
    pipeline consumes, stderr is commentary a human reads. ``result.output`` is
    the two merged and is deliberately not used here.
    """
    return CliRunner()


def stdout_json(result) -> Any:
    """Parse stdout as JSON, failing loudly if it is not byte-exact JSON."""
    assert result.stdout, f"nothing on stdout; stderr was: {result.stderr[:400]}"
    return json.loads(result.stdout)


@pytest.fixture
def audit_dir(tmp_path: Path) -> Path:
    return tmp_path / "audit"


def review(runner: CliRunner, audit_dir: Path, *extra: str, quiet: bool = True):
    args = ["review", DEMO_VCF, "--genome-build", "GRCh38", "--audit-dir", str(audit_dir)]
    if quiet:
        args.append("--quiet")
    return runner.invoke(main, [*args, *extra])


def audit_ids(runner: CliRunner, audit_dir: Path) -> list[str]:
    result = runner.invoke(main, ["audit", "--audit-dir", str(audit_dir), "--json"])
    assert result.exit_code == EXIT_OK, result.stderr
    return [record["audit_id"] for record in stdout_json(result) if record["action"] == "review"]


class TestCommandSurface:
    def test_the_signed_path_commands_are_registered(self):
        names = {command.name for command in REVIEW_COMMANDS}
        assert names == {"normalize", "review", "replay", "sign-off", "audit"}

    def test_help_lists_the_review_commands(self, runner):
        result = runner.invoke(main, ["--help"])
        assert result.exit_code == EXIT_OK
        for name in ("review", "replay", "sign-off", "normalize", "audit"):
            assert name in result.output

    def test_help_states_research_use_only(self, runner):
        result = runner.invoke(main, ["--help"])
        assert "Research use only" in result.output

    def test_review_help_discloses_the_evidence_source_choices(self, runner):
        result = runner.invoke(main, ["review", "--help"])
        assert result.exit_code == EXIT_OK
        for source in ("recorded", "live", "pack", "none"):
            assert source in result.output

    def test_the_legacy_debate_command_is_renamed_not_silently_kept(self, runner):
        result = runner.invoke(main, ["--help"])
        assert "consult" in result.output

    def test_no_command_produces_a_confidence_score(self, runner, audit_dir):
        result = review(runner, audit_dir)
        assert "confidence" not in result.output.lower()


class TestReviewCommand:
    def test_review_emits_the_versioned_contract(self, runner, audit_dir):
        result = review(runner, audit_dir)
        assert result.exit_code == EXIT_ABSTAINED, result.stderr
        payload = stdout_json(result)
        assert len(payload) == 3
        for item in payload:
            assert item["schema_version"]
            assert item["research_use_only"] is True
            assert item["classification"]["requires_human_review"] is True
            assert item["review"]["status"] == "pending"

    def test_golden_labels_through_the_cli(self, runner, audit_dir):
        payload = stdout_json(review(runner, audit_dir))
        found = {item["variant"]["spdi"]: item["classification"] for item in payload}
        assert found["NC_000017.11:43082433:G:A"]["label"] == "pathogenic"
        assert found["NC_000017.11:43082433:G:C"]["label"] == "benign"
        assert found["NC_000017.11:43082433:G:T"]["abstained"] is True

    def test_exit_code_signals_abstention(self, runner, audit_dir):
        """A run that abstained must not exit 0: that reads as 'all clear'."""
        assert review(runner, audit_dir).exit_code == EXIT_ABSTAINED

    def test_exit_code_is_zero_when_nothing_abstains(self, runner, tmp_path):
        vcf = tmp_path / "only_recorded.vcf"
        vcf.write_text(
            "##fileformat=VCFv4.2\n"
            "##reference=GRCh38\n"
            "##contig=<ID=17,length=83257441,assembly=GRCh38>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "17\t43082434\t.\tG\tA\t999\tPASS\tGENE=BRCA1\n",
            encoding="utf-8",
        )
        result = runner.invoke(
            main,
            ["review", str(vcf), "--genome-build", "GRCh38", "--audit-dir", str(tmp_path / "a"), "--quiet"],
        )
        assert result.exit_code == EXIT_OK, result.output

    def test_ruo_banner_goes_to_stderr_even_in_quiet_mode(self, runner, audit_dir):
        """The banner is on stderr, so `--quiet` and piping cannot strip it."""
        result = review(runner, audit_dir)
        assert "RESEARCH USE ONLY" in result.stderr

    def test_stdout_is_byte_exact_json(self, runner, audit_dir):
        """Regression guard: rich word-wrapping used to break the JSON payload.

        A console renderer will insert a line break inside a long string value
        to fit the terminal width. That looks fine on screen and fails to parse,
        so machine-readable output must bypass the renderer entirely.
        """
        result = review(runner, audit_dir)
        assert json.loads(result.stdout), "stdout must parse as JSON with no post-processing"
        # No wrapping artefacts: every line is either a JSON token or a brace.
        assert "\nundergone" not in result.stdout
        payload = json.loads(result.stdout)
        assert payload[0]["disclaimer"].startswith("RESEARCH USE ONLY.")
        assert "\n" not in payload[0]["disclaimer"]

    def test_the_contract_is_on_stdout_and_commentary_on_stderr(self, runner, audit_dir):
        result = review(runner, audit_dir)
        assert result.stdout.lstrip().startswith("[")
        assert not result.stderr.lstrip().startswith("[")

    def test_recorded_source_prints_its_coverage_warning(self, runner, audit_dir):
        result = review(runner, audit_dir, quiet=False)
        assert "bundled recordings" in result.stderr
        assert "abstain" in result.stderr

    def test_live_source_warns_about_egress(self, runner, tmp_path):
        result = runner.invoke(
            main,
            ["review", DEMO_VCF, "--genome-build", "GRCh38", "--source", "live",
             "--audit-dir", str(tmp_path / "a"), "--quiet", "--max-variants", "0"],
        )
        assert "Network egress" in result.stderr
        assert "live network retrieval" in result.stderr
        assert "eutils.ncbi.nlm.nih.gov" in result.stderr

    def test_missing_audit_dir_is_called_out(self, runner, tmp_path):
        result = runner.invoke(
            main, ["review", DEMO_VCF, "--genome-build", "GRCh38", "--quiet"]
        )
        assert "not be auditable" in result.stderr

    def test_json_can_be_written_to_a_file(self, runner, audit_dir, tmp_path):
        destination = tmp_path / "out" / "results.json"
        result = review(runner, audit_dir, "--json", str(destination), quiet=False)
        assert result.exit_code == EXIT_ABSTAINED
        assert destination.is_file()
        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert len(payload) == 3

    def test_a_missing_input_file_is_a_usage_error(self, runner, tmp_path):
        result = runner.invoke(main, ["review", str(tmp_path / "absent.vcf")])
        assert result.exit_code == EXIT_USAGE

    def test_an_unknown_source_is_a_usage_error(self, runner, audit_dir):
        result = runner.invoke(
            main, ["review", DEMO_VCF, "--source", "alphagenome", "--audit-dir", str(audit_dir)]
        )
        assert result.exit_code == EXIT_USAGE

    def test_pack_without_a_path_is_a_usage_error(self, runner, audit_dir):
        result = runner.invoke(
            main, ["review", DEMO_VCF, "--source", "pack", "--audit-dir", str(audit_dir)]
        )
        assert result.exit_code == EXIT_USAGE
        assert "--offline-pack" in (result.output + result.stderr)

    def test_an_unrecognized_genome_build_is_refused(self, runner, audit_dir):
        result = review(runner, audit_dir, "--genome-build", "hg18")
        assert result.exit_code == EXIT_USAGE

    def test_gene_filter_excludes_other_genes(self, runner, audit_dir):
        result = review(runner, audit_dir, "--gene", "TP53")
        assert result.exit_code == EXIT_OK, result.stderr
        assert stdout_json(result) == []
        assert "skipped by --gene" in result.stderr

    def test_gene_filter_keeps_the_matching_gene(self, runner, audit_dir):
        result = review(runner, audit_dir, "--gene", "BRCA1")
        payload = stdout_json(result)
        assert len(payload) == 3
        assert all(item["gene"]["symbol"] == "BRCA1" for item in payload)

    def test_gene_filter_never_relabels_a_variant(self, runner, audit_dir):
        """The dangerous failure mode: a filter flag must not rewrite the gene.

        Gene identity drives gene-level criteria (PVS1 mechanism, PP2, BP1), so
        relabelling BRCA1 as TP53 would change the classification. The gene of
        record must still come from the VCF, and the filter must simply exclude
        the variant instead.
        """
        result = review(runner, audit_dir, "--gene", "TP53")
        assert stdout_json(result) == []
        # Nothing was reviewed at all, so nothing could have been relabelled.
        assert "TP53" not in result.stdout

    def test_gene_of_record_does_not_override_an_annotated_gene(self, runner, audit_dir):
        result = review(runner, audit_dir, "--gene-of-record", "TP53")
        payload = stdout_json(result)
        assert len(payload) == 3
        assert all(item["gene"]["symbol"] == "BRCA1" for item in payload)
        assert all(item["gene"]["resolved_from"] == "vcf_info" for item in payload)

    def test_gene_of_record_applies_when_the_vcf_is_unannotated(self, runner, tmp_path):
        vcf = tmp_path / "no_gene.vcf"
        vcf.write_text(
            "##fileformat=VCFv4.2\n##reference=GRCh38\n"
            "##contig=<ID=17,length=83257441,assembly=GRCh38>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "17\t43082434\t.\tG\tA\t999\tPASS\t.\n",
            encoding="utf-8",
        )
        without = runner.invoke(
            main, ["review", str(vcf), "--genome-build", "GRCh38",
                   "--audit-dir", str(tmp_path / "a"), "--quiet"]
        )
        assert stdout_json(without)[0]["gene"]["resolved_from"] == "not_resolved"

        asserted = runner.invoke(
            main, ["review", str(vcf), "--genome-build", "GRCh38",
                   "--gene-of-record", "BRCA1",
                   "--audit-dir", str(tmp_path / "b"), "--quiet"]
        )
        gene = stdout_json(asserted)[0]["gene"]
        assert gene["symbol"] == "BRCA1"
        # The assertion is disclosed as an operator input, not as file evidence.
        assert gene["resolved_from"] == "cli"
        assert "asserted by the operator" in gene["note"]

    def test_max_variants_caps_the_run(self, runner, audit_dir):
        """The cap counts alleles, and the first record here is multiallelic."""
        result = review(runner, audit_dir, "--max-variants", "1")
        assert len(stdout_json(result)) == 1
        assert "not reviewed" in result.stderr

    def test_max_variants_is_respected_for_a_multiallelic_first_record(self, runner, audit_dir):
        result = review(runner, audit_dir, "--max-variants", "2")
        assert len(stdout_json(result)) == 2

    def test_source_none_abstains_on_everything(self, runner, tmp_path):
        """With no ClinVar configured there is nothing to classify from."""
        result = runner.invoke(
            main, ["review", DEMO_VCF, "--genome-build", "GRCh38", "--source", "none",
                   "--audit-dir", str(tmp_path / "a"), "--quiet"],
        )
        payload = stdout_json(result)
        assert payload
        assert all(item["classification"]["abstained"] is True for item in payload)

    def test_human_readable_rendering_names_the_indeterminate_criteria(self, runner, audit_dir):
        result = review(runner, audit_dir, quiet=False)
        assert "Indeterminate criteria" in result.stderr
        assert "PVS1" in result.stderr


class TestNormalizeCommand:
    def test_normalize_lists_every_allele(self, runner):
        result = runner.invoke(main, ["normalize", DEMO_VCF, "--genome-build", "GRCh38"])
        assert result.exit_code == EXIT_OK
        assert "43082434" in result.output
        assert "collapsed" in result.output

    def test_normalize_json_is_machine_readable(self, runner):
        result = runner.invoke(main, ["normalize", DEMO_VCF, "--genome-build", "GRCh38", "--json"])
        assert result.exit_code == EXIT_OK
        payload = stdout_json(result)
        assert payload["genome_build"] == "GRCh38"
        assert payload["genome_build_source"] == "explicit"
        assert len(payload["variants"]) == 5
        assert all(item["variant_id"].startswith("nga.v1.") for item in payload["variants"])
        assert all(item["complete"] is True for item in payload["variants"])

    def test_normalize_reads_the_build_from_the_vcf_header(self, runner):
        result = runner.invoke(main, ["normalize", DEMO_VCF, "--json"])
        payload = stdout_json(result)
        assert payload["genome_build"] == "GRCh38"
        assert payload["genome_build_source"] == "vcf_header"

    def test_normalize_reports_an_incomplete_alignment(self, runner, tmp_path):
        vcf = tmp_path / "indel.vcf"
        vcf.write_text(
            "##fileformat=VCFv4.2\n##reference=GRCh38\n"
            "##contig=<ID=17,length=83257441,assembly=GRCh38>\n"
            "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
            "17\t43082434\t.\tGA\tG\t999\tPASS\t.\n",
            encoding="utf-8",
        )
        result = runner.invoke(main, ["normalize", str(vcf), "--json"])
        variant = stdout_json(result)["variants"][0]
        assert variant["complete"] is False
        assert any(w["code"] == "indel_left_alignment_unverified" for w in variant["warnings"])


class TestReplayCommand:
    def test_replay_reproduces_a_recorded_decision(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(main, ["replay", target, "--audit-dir", str(audit_dir)])
        assert result.exit_code == EXIT_OK, result.output
        assert "reproduced" in result.stderr and "True" in result.stderr

    def test_replay_shows_the_evidence_snapshot(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(main, ["replay", target, "--audit-dir", str(audit_dir)])
        assert "Evidence snapshot" in result.stderr
        assert "ev.v1." in result.stderr

    def test_replay_appends_its_own_audit_record(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        def records():
            return stdout_json(runner.invoke(main, ["audit", "--audit-dir", str(audit_dir), "--json"]))

        before = len(records())
        runner.invoke(main, ["replay", target, "--audit-dir", str(audit_dir)])
        after = records()
        assert len(after) == before + 1
        assert after[-1]["action"] == "replay"

    def test_replay_json_output(self, runner, audit_dir, tmp_path):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        destination = tmp_path / "replay.json"
        result = runner.invoke(
            main, ["replay", target, "--audit-dir", str(audit_dir), "--json", str(destination)]
        )
        assert result.exit_code == EXIT_OK
        payload = json.loads(destination.read_text(encoding="utf-8"))
        assert payload["reproduced"] is True
        assert payload["rule_set"]

    def test_replay_of_an_unknown_id_is_a_usage_error(self, runner, audit_dir):
        review(runner, audit_dir)
        result = runner.invoke(main, ["replay", "aud.review.nope", "--audit-dir", str(audit_dir)])
        assert result.exit_code == EXIT_USAGE

    def test_replay_requires_the_audit_dir(self, runner):
        result = runner.invoke(main, ["replay", "aud.review.x"])
        assert result.exit_code == EXIT_USAGE


class TestSignOffCommand:
    def test_sign_off_records_a_reviewer(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "approve"],
        )
        assert result.exit_code == EXIT_OK, result.output
        assert "dr.who" in result.stderr
        assert "Sign-off recorded" in result.stderr

    def test_sign_off_requires_a_reviewer_identity(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(
            main, ["sign-off", target, "--audit-dir", str(audit_dir), "--action", "approve"]
        )
        assert result.exit_code == EXIT_USAGE

    def test_rejection_requires_notes(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "reject"],
        )
        assert result.exit_code == EXIT_USAGE

    def test_rejection_with_notes_succeeds(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "reject", "--notes", "Wrong transcript."],
        )
        assert result.exit_code == EXIT_OK

    def test_an_override_requires_a_reason(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "approve", "--decision", "benign"],
        )
        assert result.exit_code == EXIT_USAGE

    def test_an_override_with_a_reason_succeeds(self, runner, audit_dir, tmp_path):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        destination = tmp_path / "signed.json"
        result = runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "approve", "--decision", "benign",
             "--reason", "Laboratory-held segregation data.", "--json", str(destination)],
        )
        assert result.exit_code == EXIT_OK, result.output
        signed = json.loads(destination.read_text(encoding="utf-8"))
        assert signed["review"]["status"] == "approved"
        assert signed["review"]["decision"] == "benign"
        # The engine's own label is never rewritten by a human override.
        assert signed["classification"]["label"] == "pathogenic"
        assert any("OVERRIDE" in item.upper() for item in signed["limitations"])

    def test_sign_off_appears_in_the_audit_log(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "approve"],
        )
        records = stdout_json(
            runner.invoke(main, ["audit", "--audit-dir", str(audit_dir), "--json"])
        )
        signoffs = [record for record in records if record["action"] == "sign_off"]
        assert len(signoffs) == 1
        assert signoffs[0]["reviewer"] == "dr.who"
        assert signoffs[0]["parent_audit_id"] == target

    def test_an_unknown_action_is_a_usage_error(self, runner, audit_dir):
        review(runner, audit_dir)
        target = audit_ids(runner, audit_dir)[0]
        result = runner.invoke(
            main,
            ["sign-off", target, "--audit-dir", str(audit_dir),
             "--reviewer", "dr.who", "--action", "approve-and-publish"],
        )
        assert result.exit_code == EXIT_USAGE


class TestAuditCommand:
    def test_audit_lists_records_and_hashes_the_log(self, runner, audit_dir):
        review(runner, audit_dir)
        result = runner.invoke(main, ["audit", "--audit-dir", str(audit_dir)])
        assert result.exit_code == EXIT_OK
        assert "audit log sha256" in result.stderr
        assert "records" in result.stderr

    def test_audit_json_is_machine_readable(self, runner, audit_dir):
        review(runner, audit_dir)
        result = runner.invoke(main, ["audit", "--audit-dir", str(audit_dir), "--json"])
        records = json.loads(result.output)
        assert len(records) == 3
        assert all(record["action"] == "review" for record in records)
        assert all(record["evidence_snapshot"] for record in records)

    def test_audit_of_an_empty_directory_reports_zero(self, runner, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "audit.jsonl").touch()
        result = runner.invoke(main, ["audit", "--audit-dir", str(empty), "--json"])
        assert result.exit_code == EXIT_OK
        assert stdout_json(result) == []


class TestNoSilentNetwork:
    def test_the_default_source_needs_no_network(self, runner, audit_dir, monkeypatch):
        """The default configuration must work air-gapped."""
        import httpx

        def explode(*args, **kwargs):  # pragma: no cover - must never be called
            raise AssertionError("the default review path attempted a network call")

        monkeypatch.setattr(httpx, "get", explode, raising=False)
        monkeypatch.setattr(httpx, "Client", explode, raising=False)
        result = review(runner, audit_dir)
        assert result.exit_code == EXIT_ABSTAINED
        assert len(stdout_json(result)) == 3

    def test_recordings_are_the_declared_default(self, runner):
        result = runner.invoke(main, ["review", "--help"])
        assert "recorded" in result.output


class TestDeterminismThroughTheCli:
    def test_two_runs_produce_the_same_contract(self, runner, tmp_path):
        def run_once(directory: Path) -> list[dict]:
            result = runner.invoke(
                main,
                ["review", DEMO_VCF, "--genome-build", "GRCh38",
                 "--audit-dir", str(directory), "--quiet"],
            )
            payload = stdout_json(result)
            for item in payload:
                # Run-specific provenance: where *this* run wrote its log.
                item["provenance"].pop("audit_path", None)
                item["provenance"].pop("audit_id", None)
                item["provenance"].pop("generated_at", None)
                item.pop("result_id", None)
            return payload

        assert run_once(tmp_path / "one") == run_once(tmp_path / "two")
