"""End-to-end orchestrator test with stub tool binaries.

Installs tiny shell-script doubles for fastqc/trimmomatic/hisat2/samtools/
featureCounts/multiqc into a temp PATH, then runs the whole RNA-Seq
orchestration: per-sample parallel DAG, verification, provenance manifest,
and report. This is the CI-verification of the pipeline path.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest
from rich.console import Console

from ngs_agent.agent.orchestrator import AgentOrchestrator
from ngs_agent.config.settings import NGSSettings
from ngs_agent.execution.selector import BackendSelector
from ngs_agent.tools.registry import create_default_registry

STUBS: dict[str, str] = {
    "fastqc": """#!/usr/bin/env bash
outdir="."
prev=""
for arg in "$@"; do
  if [ "$prev" = "--outdir" ]; then outdir="$arg"; fi
  prev="$arg"
done
mkdir -p "$outdir"; outdir=$(cd "$outdir" && pwd)
for arg in "$@"; do
  case "$arg" in
    *.fastq|*.fastq.gz|*.fq|*.fq.gz)
      stem=$(basename "$arg"); stem="${stem%.gz}"; stem="${stem%.fastq}"; stem="${stem%.fq}"
      echo "<html>qc</html>" > "$outdir/${stem}_fastqc.html"
      tmp=$(mktemp -d); mkdir -p "$tmp/${stem}"
      printf 'PASS\\tBasic Statistics\\t%s\\nFAIL\\tAdapter Content\\t%s\\n' \
        "$arg" "$arg" > "$tmp/${stem}/fastqc_summary.txt"
      (cd "$tmp" && zip -qr "$outdir/${stem}_fastqc.zip" "${stem}/fastqc_summary.txt")
      cp "$tmp/${stem}/fastqc_summary.txt" "$outdir/${stem}_fastqc.summary.txt"
      ;;
  esac
done
""",
    "trimmomatic": """#!/usr/bin/env bash
mode=$1; shift
args=()
while [ $# -gt 0 ]; do
  case "$1" in
    [A-Z]*) break ;;
  esac
  args+=("$1"); shift
done
if [ "$mode" = "PE" ] && [ "${#args[@]}" -ge 6 ]; then
  for f in "${args[@]:2:4}"; do
    mkdir -p "$(dirname "$f")"; printf '@R\\nACGT\\n+\\nIIII\\n' > "$f"
  done
  echo "Input Read Pairs: 10000 Both Surviving: 9000 (90.00%) " \
    "Forward Only Surviving: 400 (4.00%) Reverse Only Surviving: 300 (3.00%) " \
    "Dropped: 300 (3.00%)" >&2
else
  mkdir -p "$(dirname "${args[1]}")"; printf '@R\\nACGT\\n+\\nIIII\\n' > "${args[1]}"
  echo "Input Reads: 10000 Surviving: 9500 (95.00%) Dropped: 500 (5.00%)" >&2
fi
""",
    "hisat2": """#!/usr/bin/env bash
sam=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-S" ]; then sam="$arg"; fi
  prev="$arg"
done
[ -z "$sam" ] && exit 1
mkdir -p "$(dirname "$sam")"
printf '@HD\\tVN:1.0\\n@SQ\\tSN:chr1\\tLN:1000\\n'
  'r1\\t0\\tchr1\\t10\\t60\\t50M\\t*\\t0\\t0\\tACGT\\t*\\n' \\
  > "$sam"
echo "overall alignment rate: 96.00%" >&2
""",
    "hisat2_extract_splice_sites.py": """#!/usr/bin/env python3
print("chr1\\t100\\t200\\t+\\t1")
""",
    "hisat2-build": """#!/usr/bin/env bash
for i in 1 2 3 4 5 6 7 8; do echo "idx$i" > "$2.$i.ht2"; done
""",
    "samtools": """#!/usr/bin/env bash
action=$1; shift
case "$action" in
  sort|view)
    out=""; in=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) shift; out="$1" ;;
        -@|-q|-b|-T) ;;
        -*) ;;
        *) in="$1" ;;
      esac
      shift
    done
    [ -z "$out" ] && exit 1
    mkdir -p "$(dirname "$out")"; printf '%s-from-%s\\n' "$action" "$in" > "$out"
    ;;
  index)
    in="${@: -1}"; cp "$in" "$in.bai"
    ;;
  flagstat) echo "5000 + 0 in total" ;;
  *) echo "unknown action" >&2; exit 1 ;;
esac
""",
    "featureCounts": """#!/usr/bin/env bash
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
[ -z "$out" ] && exit 1
mkdir -p "$(dirname "$out")"
printf '#gene\\tS1\\nGENE1\\t100\\n' > "$out"
printf 'Status\\tcount\\nAssigned\\t4000\\n' > "$out.summary"
""",
    "multiqc": """#!/usr/bin/env bash
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "-o" ]; then out="$arg"; fi
  prev="$arg"
done
mkdir -p "$out"; echo "<html>multiqc</html>" > "$out/multiqc_report.html"
""",
}


@pytest.fixture()
def stub_binaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    for name, script in STUBS.items():
        script_path = bin_dir / name
        script_path.write_text(script, encoding="utf-8")
        script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("NGS_TOOL_PREFIX", str(bin_dir))
    return bin_dir


@pytest.fixture()
def experiment(tmp_path: Path) -> Path:
    work = tmp_path / "experiment"
    data = work / "data"
    data.mkdir(parents=True)
    for name in ("S1_R1.fastq.gz", "S1_R2.fastq.gz", "S2_R1.fastq.gz", "S2_R2.fastq.gz"):
        (data / name).write_text("@R\nACGT\n+\nIIII\n", encoding="utf-8")
    (data / "genes.gtf").write_text(
        'chr1\ttest\texon\t100\t200\t.\t+\t.\tgene_id "G1";\n', encoding="utf-8"
    )
    (data / "samplesheet.csv").write_text(
        "sample,fastq_1,fastq_2,strandedness,condition\n"
        "S1,S1_R1.fastq.gz,S1_R2.fastq.gz,unstranded,control\n"
        "S2,S2_R1.fastq.gz,S2_R2.fastq.gz,unstranded,treated\n",
        encoding="utf-8",
    )
    return work


def run_orchestrator(experiment: Path) -> dict[str, object]:
    settings = NGSSettings(
        artifacts_dir=str(experiment / "artifacts"),
        logs_dir=str(experiment / "logs"),
        require_confirmation_for=set(),
        anthropic_api_key="",  # heuristic path; no network in CI
    )
    orchestrator = AgentOrchestrator(
        settings, BackendSelector(), create_default_registry(), Console(quiet=True, width=250)
    )
    return orchestrator.run(
        objective="run differential expression on my RNA-seq data",
        workflow="rnaseq",
        working_directory=experiment,
        confirm_callback=lambda _prompt: True,
    )


@pytest.mark.integration
class TestFullRnaSeqPipeline:
    def test_pipeline_completes_with_provenance_and_report(
        self, stub_binaries: Path, experiment: Path
    ) -> None:
        result = run_orchestrator(experiment)

        assert result["status"] == "complete"
        outcomes = result["outcomes"]
        assert len(outcomes) >= 13  # discover + 5 steps/sample x2 + quantify + aggregate + report
        assert all(outcome["status"] == "ok" for outcome in outcomes), [
            (o["step_name"], o.get("stderr", "")) for o in outcomes if o["status"] != "ok"
        ]
        # Per-sample steps actually ran in parallel.
        assert result["max_observed_parallelism"] >= 2

        # Real outputs on disk.
        for sample in ("S1", "S2"):
            assert (experiment / "results" / sample / "aligned.sorted.bam").exists()
            assert (experiment / "results" / sample / "aligned.sorted.bam.bai").exists()
        assert (experiment / "results" / "counts" / "counts.txt").exists()
        assert (experiment / "results" / "multiqc" / "multiqc_report.html").exists()

        # Provenance manifest: every artifact hashed, none None.
        manifest_path = Path(result["manifest"])
        assert manifest_path.exists()
        records = [json.loads(line) for line in manifest_path.read_text().splitlines() if line]
        assert len(records) >= 10
        for record in records:
            assert isinstance(record["sha256"], str) and len(record["sha256"]) == 64

        # Report with escaped, auditable content.
        report_html = Path(result["report"]["html_report"])
        assert report_html.exists()
        html = report_html.read_text(encoding="utf-8")
        assert "Steps Executed" in html
        assert "hisat2" in html
        assert "Provenance" in html

        # Verification passed with all expected artifacts observed.
        verification = result["verification"]
        assert verification["missing_artifacts"] == []
        assert verification["passed"] is True

    def test_dry_run_executes_nothing(self, stub_binaries: Path, experiment: Path) -> None:
        settings = NGSSettings(
            artifacts_dir=str(experiment / "artifacts"),
            require_confirmation_for=set(),
        )
        orchestrator = AgentOrchestrator(
            settings, BackendSelector(), create_default_registry(), Console(quiet=True, width=250)
        )
        result = orchestrator.run(
            objective="rna-seq please",
            workflow="rnaseq",
            working_directory=experiment,
            dry_run=True,
        )
        assert result["status"] == "dry-run"
        assert not (experiment / "results").exists()

    def test_tool_failure_aborts_with_actionable_outcome(
        self, stub_binaries: Path, experiment: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Break hisat2 so alignment fails for both samples.
        (stub_binaries / "hisat2").write_text(
            "#!/usr/bin/env bash\necho 'index files corrupt' >&2\nexit 2\n"
        )
        (stub_binaries / "hisat2").chmod(0o755)
        result = run_orchestrator(experiment)

        assert result["status"] == "failed"
        outcomes = {outcome["step_name"]: outcome for outcome in result["outcomes"]}
        assert outcomes["align-S1"]["status"] == "failed"
        assert "index files corrupt" in outcomes["align-S1"]["stderr"]
        # Downstream of the failure is skipped; QC steps still completed.
        assert outcomes["sort-S1"]["status"] == "skipped"
        assert outcomes["quantify"]["status"] == "skipped"
        assert outcomes["qc-S1"]["status"] == "ok"
