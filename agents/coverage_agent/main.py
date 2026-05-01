import csv
import os
import tempfile
from pathlib import Path

from base_agent import BaseAgent
from storage import MinioStorage


class CoverageAgent(BaseAgent):
    def _to_float(self, value) -> float | None:
        try:
            return float(value)
        except Exception:
            return None

    def _mean_depth_from_csv(self, csv_path: str) -> float | None:
        values: list[float] = []
        with open(csv_path, "r", encoding="utf-8") as handle:
            reader = csv.DictReader(handle)
            for row in reader:
                if "mean_depth" not in row:
                    continue
                depth = self._to_float(row.get("mean_depth"))
                if depth is not None:
                    values.append(depth)
        if not values:
            return None
        return sum(values) / len(values)

    def execute(self, inputs, routing_ctx):
        threshold = self._to_float(routing_ctx.get("coverage_threshold")) or 30.0
        payload = inputs.get("payload", {})

        mean_depth = self._to_float(payload.get("mean_depth"))
        coverage_csv = payload.get("coverage_depth_csv")

        if mean_depth is None and isinstance(coverage_csv, str) and coverage_csv.startswith("s3://"):
            storage = MinioStorage()
            with tempfile.TemporaryDirectory(prefix="coverage-") as workdir:
                local_csv = storage.download_file(coverage_csv, str(Path(workdir) / "coverage_depth.csv"))
                mean_depth = self._mean_depth_from_csv(local_csv)
        elif mean_depth is None and isinstance(coverage_csv, str) and Path(coverage_csv).exists():
            mean_depth = self._mean_depth_from_csv(coverage_csv)

        if mean_depth is None:
            mean_depth = 25.0
            reasoning = (
                "Coverage input not available, using mock depth estimate of 25x for gate evaluation"
            )
        else:
            reasoning = f"Computed mean target depth at {mean_depth:.2f}x from coverage metrics"

        passed = mean_depth >= threshold
        halt = not passed

        return {
            "agent": "coverage_agent",
            "status": "ok" if passed else "warn",
            "payload": {
                "mean_depth": round(mean_depth, 3),
                "threshold": threshold,
                "passed": passed,
            },
            "reasoning": f"{reasoning}; threshold is {threshold:.1f}x",
            "halt": halt,
            "halt_reason": "Coverage below threshold" if halt else "",
        }


if __name__ == "__main__":
    CoverageAgent().run()
