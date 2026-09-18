"""Differential-expression agent (DESeq2).

Two input contracts are accepted:

1. Single merged matrix (legacy / direct use)::

       {"payload": {"count_matrix": <uri>}, "sample_sheet": <uri>}

2. Batch merge (used by the parent pipeline workflow)::

       {"counts": [{"sample_id": ..., "count_matrix": <uri>}, ...],
        "samples": [{"sample_id": ..., "condition": ...}, ...]}

   Each per-sample featureCounts TSV is downloaded and merged on Geneid
   into one matrix whose columns are sample_ids, and a sample sheet is
   generated from the sample list. This is the only statistically valid
   way to run DESeq2 across a cohort.
"""

import csv
import json
import os
import subprocess
import tempfile
from pathlib import Path

from base_agent import BaseAgent
from storage import MinioStorage


class DEAgent(BaseAgent):
    def _materialize(self, value: str, storage: MinioStorage, workdir: str) -> str:
        if value and value.startswith("s3://"):
            return storage.download_file(value, os.path.join(workdir, Path(value).name))
        return value

    def _merge_featurecounts(self, counts: list, samples: list, workdir: str, storage: MinioStorage) -> tuple[str, str]:
        """Merge per-sample featureCounts TSVs into one count matrix + sheet."""
        conditions = {s.get("sample_id"): s.get("condition", "unknown") for s in samples}
        merged: dict[str, dict[str, str]] = {}
        ordered_samples: list[str] = []
        for entry in counts:
            if isinstance(entry, dict):
                sample_id = entry.get("sample_id", "")
                uri = entry.get("count_matrix") or entry.get("payload", {}).get("count_matrix", "")
            else:
                sample_id, uri = "", entry
            if not uri:
                continue
            if not sample_id:
                raise RuntimeError("Each entry in `counts` must include sample_id")
            local = self._materialize(uri, storage, workdir)
            gene_counts = self._read_featurecounts(local)
            ordered_samples.append(sample_id)
            for gene, value in gene_counts.items():
                merged.setdefault(gene, {})[sample_id] = value

        if len(ordered_samples) < 2:
            raise RuntimeError(
                f"DE needs ≥2 count matrices, got {len(ordered_samples)}"
            )

        matrix_path = os.path.join(workdir, "merged_counts.tsv")
        with open(matrix_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle, delimiter="\t")
            writer.writerow(["Geneid", *ordered_samples])
            for gene in sorted(merged):
                writer.writerow([gene, *(merged[gene].get(s, "0") for s in ordered_samples)])

        sheet_path = os.path.join(workdir, "sample_sheet.csv")
        with open(sheet_path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow(["sample_id", "condition"])
            for sample_id in ordered_samples:
                writer.writerow([sample_id, conditions.get(sample_id, "unknown")])

        return matrix_path, sheet_path

    @staticmethod
    def _read_featurecounts(path: str) -> dict[str, str]:
        """Read a featureCounts TSV (skips `#` comments + metadata columns)."""
        gene_counts: dict[str, str] = {}
        with open(path, encoding="utf-8") as handle:
            header: list[str] | None = None
            for raw in handle:
                if not raw.strip() or raw.startswith("#"):
                    continue
                cols = raw.rstrip("\n").split("\t")
                if header is None:
                    header = cols
                    continue
                if len(cols) < 7:
                    continue
                # Last column is the sample's count (single-BAM quantification).
                gene_counts[cols[0]] = cols[-1]
        if not gene_counts:
            raise RuntimeError(f"No gene counts parsed from {path}")
        return gene_counts

    def execute(self, inputs, routing_ctx):
        run_id = routing_ctx.get("run_id", "unknown")

        storage = MinioStorage()
        with tempfile.TemporaryDirectory(prefix="de-") as workdir:
            if inputs.get("counts") and inputs.get("samples"):
                local_count, local_sheet = self._merge_featurecounts(
                    inputs["counts"], inputs["samples"], workdir, storage
                )
            else:
                count_uri = inputs.get("payload", {}).get("count_matrix") or inputs.get("count_matrix")
                sample_sheet_uri = inputs.get("sample_sheet") or routing_ctx.get("sample_sheet")
                if not count_uri:
                    raise RuntimeError("count_matrix is required for DE analysis")
                if not sample_sheet_uri:
                    raise RuntimeError(
                        "sample_sheet is required for DE analysis. "
                        "Single-sample runs skip DE by design; submit a batch "
                        "with ≥2 conditions for differential expression."
                    )
                local_count = self._materialize(count_uri, storage, workdir)
                local_sheet = self._materialize(sample_sheet_uri, storage, workdir)

            out_dir = os.path.join(workdir, "out")
            os.makedirs(out_dir, exist_ok=True)

            cmd = ["Rscript", "/app/de_analysis.R", local_count, local_sheet, out_dir]
            result = subprocess.run(cmd, capture_output=True, text=True)
            if result.returncode != 0:
                raise RuntimeError(f"DESeq2 analysis failed: {result.stderr.strip()}")

            uploaded = {}
            for path in Path(out_dir).glob("**/*"):
                if path.is_file():
                    key = f"{run_id}/de/{path.name}"
                    uploaded[path.name] = storage.upload_file(str(path), key)

            summary_path = Path(out_dir) / "de_summary.json"
            summary = json.loads(summary_path.read_text()) if summary_path.exists() else {}

        return {
            "agent": "de",
            "status": "ok",
            "payload": {
                "artifacts": uploaded,
                "de_summary": summary,
            },
            "reasoning": "DESeq2 completed with statistical outputs and plots",
        }


if __name__ == "__main__":
    DEAgent().run()
