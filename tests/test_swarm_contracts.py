"""Unit tests for swarm pipeline contracts (no Docker / Temporal needed).

Temporal-dependent imports are skipped when the `swarm` extra is not
installed, so the default `pip install -e \".[dev,llm]\"` CI stays green.
Agent mains are loaded via importlib with lightweight stubs for the
container-only `storage` helper.
"""

import importlib.util
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent


def _load_agent_main(agent_dir: str, extra_stubs: dict | None = None):
    """Import agents/<dir>/main.py with stubbed container-only modules."""
    if "storage" not in sys.modules:
        storage_mod = types.ModuleType("storage")

        class MinioStorage:  # pragma: no cover - stub
            def download_file(self, uri, local):
                raise RuntimeError("no MinIO in unit tests")

            def upload_file(self, local, key):
                return f"s3://test/{key}"

        storage_mod.MinioStorage = MinioStorage
        sys.modules["storage"] = storage_mod
    for name in (extra_stubs or {}):
        sys.modules[name] = (extra_stubs or {})[name]
    base_dir = str(ROOT / "agents" / "base")
    if base_dir not in sys.path:
        sys.path.insert(0, base_dir)
    # anthropic is only needed at call time for most agents; ai_decider
    # imports it at module level, so provide a stub if missing.
    if "anthropic" not in sys.modules:
        try:
            import anthropic  # noqa: F401
        except ImportError:
            anthropic_mod = types.ModuleType("anthropic")

            class Anthropic:  # pragma: no cover - stub
                def __init__(self, *a, **k):
                    raise RuntimeError("no anthropic in unit tests")

            anthropic_mod.Anthropic = Anthropic
            sys.modules["anthropic"] = anthropic_mod
    spec = importlib.util.spec_from_file_location(
        f"{agent_dir}_main", str(ROOT / "agents" / agent_dir / "main.py")
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class TestStaging:
    @pytest.fixture(autouse=True)
    def _temporal(self):
        pytest.importorskip("temporalio")

    def test_echoed_container_paths_remount(self, tmp_path):
        from workflows import activities

        r1 = tmp_path / "R1.fastq"
        r1.write_text("@r\nACGT\n+\nIIII\n")
        mounts: list = []
        hop1 = activities._replace_local_file_paths({"fastq_r1": str(r1)}, mounts, [0])
        assert hop1["fastq_r1"].startswith("/mnt/inputs/")
        assert mounts

        # Agent echoes the container path back; next hop must re-mount it.
        mounts2: list = []
        hop2 = activities._replace_local_file_paths(
            {"payload": {"raw_reads_r1": hop1["fastq_r1"]}}, mounts2, [0]
        )
        assert mounts2, "echoed container path was not re-mounted"
        assert hop2["payload"]["raw_reads_r1"] == hop1["fastq_r1"]

    def test_reference_basename_mounts_family(self, tmp_path):
        from workflows import activities

        for i in (1, 2):
            (tmp_path / f"idx.{i}.ht2").write_text("x")
        mounts: list = []
        out = activities._replace_local_file_paths(
            {"reference_genome": str(tmp_path / "idx")}, mounts, [0]
        )
        assert out["reference_genome"].startswith("/mnt/inputs/")
        assert any(m[0].endswith(".1.ht2") for m in mounts)
        assert any(m[0].endswith(".2.ht2") for m in mounts)

    def test_s3_passthrough(self):
        from workflows import activities

        obj = {"x": "s3://bucket/key"}
        assert activities._replace_local_file_paths(obj, [], [0]) == obj


class TestCache:
    def test_run_id_excluded_from_hash(self):
        pytest.importorskip("boto3")
        pytest.importorskip("redis")
        from shared.cache import CacheManager

        cm = CacheManager.__new__(CacheManager)  # no connections needed for hashing
        h1 = cm.compute_hash("qc", {"routing_ctx": {"run_id": "run-aaa", "x": 1}})
        h2 = cm.compute_hash("qc", {"routing_ctx": {"run_id": "run-bbb", "x": 1}})
        assert h1 == h2


class TestAIDeciderHeuristic:
    def test_fastqc_module_format_fires(self):
        mod = _load_agent_main("ai_decider")
        agent = mod.AIDeciderAgent.__new__(mod.AIDeciderAgent)
        decision = agent._heuristic_decision(
            "##FastQC\n>>Per base sequence quality\tfail\n>>Adapter Content\twarn\n"
        )
        assert decision["trim"] is True

    def test_clean_report_no_trim(self):
        mod = _load_agent_main("ai_decider")
        agent = mod.AIDeciderAgent.__new__(mod.AIDeciderAgent)
        decision = agent._heuristic_decision(">>Basic Statistics\tpass\n")
        assert decision["trim"] is False


class TestGatkBamPath:
    def test_artifacts_bam_path_accepted(self):
        mod = _load_agent_main("gatk_agent")
        agent = mod.GATKAgent.__new__(mod.GATKAgent)
        with pytest.raises(RuntimeError, match="reference_fasta is required"):
            # Must get PAST the BAM check (would raise "BAM input not found").
            agent.execute(
                {"payload": {"artifacts": {"bam_path": "s3://b/x.bam"}}},
                {"run_id": "t"},
            )


class TestCoverageUnknown:
    def test_unknown_does_not_halt(self):
        mod = _load_agent_main("coverage_agent")
        agent = mod.CoverageAgent.__new__(mod.CoverageAgent)
        result = agent.execute({"payload": {}}, {"run_id": "t"})
        assert result["halt"] is False
        assert result["payload"].get("coverage_unknown") is True


class TestAnnotationGzip:
    def test_gzipped_vcf_parses(self, tmp_path):
        pytest.importorskip("pandas")
        pytest.importorskip("matplotlib")
        import gzip

        mod = _load_agent_main("annotation_agent")
        agent = mod.AnnotationAgent.__new__(mod.AnnotationAgent)
        gz = tmp_path / "v.vcf.gz"
        with gzip.open(gz, "wt") as fh:
            fh.write(
                "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
                "1\t100\t.\tA\tG\t50\tPASS\tGENE=BRCA1\n"
            )
        df = agent._parse_vcf(str(gz))
        assert len(df) == 1
        assert df.iloc[0]["gene"] == "BRCA1"


class TestDEMerge:
    def test_featurecounts_merge(self, tmp_path):
        mod = _load_agent_main("de_agent")
        agent = mod.DEAgent.__new__(mod.DEAgent)
        header = "# Program:featureCounts\nGeneid\tChr\tStart\tEnd\tStrand\tLength\tinput.bam\n"
        c1 = tmp_path / "c1.tsv"
        c2 = tmp_path / "c2.tsv"
        c1.write_text(header + "G1\t1\t1\t100\t+\t100\t10\nG2\t1\t101\t200\t+\t100\t20\n")
        c2.write_text(header + "G1\t1\t1\t100\t+\t100\t30\nG2\t1\t101\t200\t+\t100\t5\n")
        matrix, sheet = agent._merge_featurecounts(
            [
                {"sample_id": "S1", "count_matrix": str(c1)},
                {"sample_id": "S2", "count_matrix": str(c2)},
            ],
            [
                {"sample_id": "S1", "condition": "control"},
                {"sample_id": "S2", "condition": "treated"},
            ],
            str(tmp_path),
            storage=None,
        )
        assert Path(matrix).read_text().splitlines()[0] == "Geneid\tS1\tS2"
        assert "S1,control" in Path(sheet).read_text()


class TestTrimPayloadShape:
    def test_ingest_style_payload_passes_fastq_check(self, tmp_path):
        mod = _load_agent_main("trim")
        agent = mod.TrimAgent.__new__(mod.TrimAgent)
        r1 = tmp_path / "r1.fq"
        r2 = tmp_path / "r2.fq"
        r1.write_text("@a\nACGT\n+\nIIII\n")
        r2.write_text("@a\nACGT\n+\nIIII\n")
        # Must NOT raise "requires FASTQ input"; it will fail later at the
        # missing trimmomatic binary (FileNotFoundError) in unit-test envs.
        with pytest.raises((RuntimeError, FileNotFoundError)) as excinfo:
            agent.execute(
                {
                    "payload": {
                        "raw_reads_r1": str(r1),
                        "raw_reads_r2": str(r2),
                        "trim_params": {"LEADING": 5},
                    }
                },
                {"run_id": "t"},
            )
        assert "requires FASTQ input" not in str(excinfo.value)
