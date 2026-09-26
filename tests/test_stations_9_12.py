"""Station 9 watch, Station 10 VCF QC, Stations 11-12 runner signatures."""
from __future__ import annotations
import hashlib
from pathlib import Path
import pytest
from core.assess import assess_path
from core.models import DECISION_UNKNOWN, DECISION_HEALTHY
from core.parse.runner_log import diagnose_runner
from doors.cli import EXIT_FAILED, EXIT_OK, watch_nextflow, render_terminal
from core.answer import answer_verdict

ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/"fixtures"

@pytest.mark.parametrize(("name","rule"),[("low_depth.vcf","QC-VCF-01"),("high_missingness.vcf","QC-VCF-02")])
def test_vcf_rules_depth_and_missingness_have_receipts(name,rule):
    verdict=assess_path(FIX/"vcf"/name)
    assert rule in [f.id for f in verdict.findings]
    finding=next(f for f in verdict.findings if f.id==rule)
    assert finding.has_valid_receipts()
    fr=next(r for r in finding.receipts if r.source.startswith("file:"))
    assert hashlib.sha256((FIX/"vcf"/name).read_bytes()).hexdigest()==fr.version.removeprefix("sha256:")
    assert "line=" in fr.locator

def test_clean_and_gzipped_vcf_are_qc_healthy():
    for path in (FIX/"vcf"/"clean.vcf",FIX/"vcf"/"sample.vcf.gz"):
        v=assess_path(path)
        assert v.decision==DECISION_HEALTHY and not v.findings
        assert "pathogenicity" in v.unknown[0]

def test_gvcf_and_multisample_are_recognised_not_judged():
    for name in ("gvcf.vcf","multi_sample.vcf","unnormalised.vcf"):
        v=assess_path(FIX/"vcf"/name)
        assert v.decision==DECISION_UNKNOWN and not v.findings
        assert "recognised, not judged" in v.headline

def test_titv_het_hom_and_filter_rules_fire_independently(tmp_path):
    # Directly manufacture VCFs so each metric is independently outside its boundary.
    header="##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\ts\n"
    rows=[]
    for i in range(12):
        ref,alt=(("A","G") if i%2==0 else ("C","T")) if i<11 else ("A","C")
        gt="1/1" if i==11 else "0/1"
        rows.append(f"1\t{i+1}\t.\t{ref}\t{alt}\t50\t.\tDP=30\tGT:DP\t{gt}:30\n")
    p=tmp_path/"extreme.vcf";p.write_text(header+"".join(rows))
    ids={f.id for f in assess_path(p).findings}
    assert {"QC-VCF-03","QC-VCF-04","QC-VCF-05"} <= ids

@pytest.mark.parametrize("rule_id", ["QC-VCF-03", "QC-VCF-04", "QC-VCF-05"])
def test_each_remaining_vcf_rule_fires_on_its_own_evidence(tmp_path, rule_id):
    header="##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\tFORMAT\ts\n"
    rows=[]
    for i in range(12):
        if rule_id=="QC-VCF-03":
            ref,alt=("A","G") if i<11 else ("A","C")
        else:
            ref,alt=(("A","G"),("C","T"),("A","C"),("G","T"))[i%4]
        gt="1/1" if (i==11 if rule_id=="QC-VCF-04" else i%2==1) else "0/1"
        filt="." if rule_id=="QC-VCF-05" and i==0 else "PASS"
        rows.append(f"1\t{i+1}\t.\t{ref}\t{alt}\t50\t{filt}\tDP=30\tGT:DP\t{gt}:30\n")
    path=tmp_path/f"{rule_id}.vcf";path.write_text(header+"".join(rows))
    assert rule_id in {f.id for f in assess_path(path).findings}


def test_malformed_vcf_is_recognised_but_not_guessed(tmp_path):
    path=tmp_path/"broken.vcf";path.write_text("##fileformat=VCFv4.2\n")
    verdict=assess_path(path)
    assert verdict.decision==DECISION_UNKNOWN and "ROADMAP.md" in verdict.headline

def test_snakemake_each_signature_and_unknown_have_expected_receipts():
    cases={"missing_input":"SM-INPUT-001","ambiguous_rule":"SM-AMBIG-002","unknown_target":"SM-TARGET-003","wildcards":"SM-WILDCARD-004","conda":"SM-CONDA-005","job_failed":"SM-JOB-006","incomplete":"SM-INCOMPLETE-007","cycle":"SM-CYCLE-008"}
    for name,rule in cases.items():
        v=assess_path(FIX/"logs"/"snakemake"/(name+".log"))
        assert len(v.findings)==1 and v.findings[0].id==rule
        assert v.findings[0].has_valid_receipts()
    unknown=assess_path(FIX/"logs"/"snakemake"/"no_match.log")
    assert unknown.decision==DECISION_UNKNOWN and unknown.details["last_lines"]

def test_snakemake_one_shot_and_eof_diagnosis_are_equivalent():
    path=FIX/"logs"/"snakemake"/"missing_input.log"
    one_shot=assess_path(path)
    at_eof=diagnose_runner(path,"snakemake")
    assert one_shot.kind==at_eof.kind
    assert one_shot.decision==at_eof.decision
    assert one_shot.headline==at_eof.headline
    assert [(f.id,f.details) for f in one_shot.findings]==[(f.id,f.details) for f in at_eof.findings]


def test_cromwell_signature_fixtures_and_unknown():
    cases={"failed_call":"CW-CALL-001","shard_retry":"CW-SHARD-002","backend":"CW-BACKEND-003","localization":"CW-LOCALIZE-004","capture":"CW-CAPTURE-005"}
    for name,rule in cases.items():
        v=assess_path(FIX/"logs"/"cromwell"/(name+".log"))
        assert v.findings and v.findings[0].id==rule and v.findings[0].has_valid_receipts()
    assert assess_path(FIX/"logs"/"cromwell"/"no_match.log").decision==DECISION_UNKNOWN

def test_watcher_detects_midrun_oom_but_holds_partial_line(capsys):
    interrupt=lambda _: (_ for _ in ()).throw(KeyboardInterrupt())
    code=watch_nextflow(str(FIX/"logs"/"growing"/"nextflow.early"),0.01,sleep=interrupt)
    out=capsys.readouterr().out
    assert code==EXIT_OK and "NF-JAVA-006" not in out
    code=watch_nextflow(str(FIX/"logs"/"growing"/"nextflow.part1"),0.01,sleep=interrupt)
    out=capsys.readouterr().out
    assert code==EXIT_FAILED and "LIVE" in out and "NF-JAVA-006" in out
    code=watch_nextflow(str(FIX/"logs"/"growing"/"truncated.part"),0.01,sleep=lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    out=capsys.readouterr().out
    assert code==EXIT_OK and "NF-JAVA-006" not in out

def test_watch_final_card_matches_one_shot_card(tmp_path,capsys):
    path=tmp_path/"nextflow.log"
    path.write_text(" N E X T F L O W  ~  version 24.04.2\n")
    complete=(FIX/"logs"/"growing"/"clean.part").read_text()
    def finish(_): path.write_text(complete)
    assert watch_nextflow(str(path),0.01,sleep=finish)==EXIT_OK
    expected=render_terminal(assess_path(path),answer_verdict(assess_path(path)),colour=False)
    out=capsys.readouterr().out
    assert out.endswith(expected)

def test_core_has_no_network_imports_for_new_stations():
    for p in [ROOT/"core/parse/vcf.py",ROOT/"core/rules/vcf_rules.py",ROOT/"core/parse/runner_log.py"]:
        text=p.read_text().lower()
        assert not any(word in text for word in ("requests", "httpx", "urllib", "socket", "openai", "anthropic"))
