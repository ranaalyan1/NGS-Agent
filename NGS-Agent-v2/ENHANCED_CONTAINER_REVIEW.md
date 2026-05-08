# NGS-Agent v2 Containerization Review

## Executive Summary

NGS-Agent v2 is a capable RNA-seq automation prototype, but it is not yet ready for high-confidence research use. The repository still mixes a legacy script-driven pipeline with a controller-based orchestration layer, relies on local filesystem assumptions, and presents a cleaner portability story in the README than the implementation actually delivers. In practice, that creates ambiguity around the supported entrypoint, the execution environment, and the reproducibility guarantees.

The most important issues are:

- Dual execution paths make it unclear which CLI is authoritative.
- Local-only defaults and hardcoded assumptions reduce portability.
- The README describes a polished installation and runtime model that the code does not fully support.
- Containerization is described as modular and reproducible, but the build/runtime story is not yet disciplined enough for production use.
- Testing is present, but coverage is too shallow for research-facing workflows.

## Findings

### 1. Dual execution paths create maintenance drift

The repository exposes a legacy script entrypoint and a separate controller pipeline. That split increases maintenance cost and makes it harder to know which interface is authoritative.

The README promotes a script runner for normal usage:

```bash
python scripts/run_ngs_agent.py --input ./data/input --output ./results
python scripts/run_ngs_agent.py --config config/custom.yaml --input ./data/input
python test_system.py
python test_e2e.py
```

See [README.md](NGS-Agent-v2/README.md#L38-L84).

The controller implements the core pipeline logic directly:

```python
qc_result = self.qc_agent.run({'fastq_files': fastq_files}, self.config)
align_result = self.alignment_agent.run(
    {'fastq_r1': trimmed_files[0], 'fastq_r2': trimmed_files[1] if len(trimmed_files) > 1 else None},
    self.config
)
```

See [pipelines/controller.py](NGS-Agent-v2/pipelines/controller.py#L61-L163).

The CLI script also mutates config values and then dispatches the controller:

```python
if args.output:
    config.set('paths.output_dir', args.output)

pipeline = PipelineController(args.config)
results = pipeline.run(fastq_files)
```

See [scripts/run_ngs_agent.py](NGS-Agent-v2/scripts/run_ngs_agent.py#L170-L220).

Impact:

- Users have to guess whether the script or controller is the supported interface.
- Bug fixes can land in one path but not the other.
- Documentation, tests, and runtime behavior can diverge.

Proposed fix:

- Make one CLI the canonical entrypoint and treat the other as compatibility-only.
- Document the older path explicitly as a `Legacy Temporal workflow` if it must remain.
- Move shared orchestration into a single service layer so both surfaces call the same logic.

### 2. Local-only defaults reduce portability

Several code paths assume that execution happens on a local machine with a fixed output directory and local report generation.

The controller returns a hardcoded relative report path:

```python
return {
    'status': 'SUCCESS',
    'results': self.pipeline_results,
    'report_path': './results/report.html',
    'log': self.execution_log,
}
```

See [pipelines/controller.py](NGS-Agent-v2/pipelines/controller.py#L192-L199).

The CLI defaults to local directories and expects a local input folder:

```python
parser.add_argument('--input', '-i', required=True, help='Input directory containing FASTQ files')
parser.add_argument('--output', '-o', default='./results', help='Output directory for results (default: ./results)')
```

See [scripts/run_ngs_agent.py](NGS-Agent-v2/scripts/run_ngs_agent.py#L43-L55).

The README reinforces the same local-first assumptions:

```bash
cd NGS-Agent-v2
conda create -n ngs-agent python=3.11 -y
pip install -r requirements.txt
python scripts/run_ngs_agent.py --input ./data/input --output ./results
```

See [README.md](NGS-Agent-v2/README.md#L38-L70).

Impact:

- Containerized or remote execution becomes brittle.
- Shared-storage and workflow-runner deployments require manual patching.
- Artifact paths are not explicit or configurable enough.

Proposed fix:

- Introduce a config-driven runtime section, for example `runtime.output_dir`, `runtime.work_dir`, and `runtime.backend`.
- Support environment variables such as `NGS_OUTPUT_DIR`, `NGS_WORK_DIR`, `NGS_CONTAINER_BACKEND`, and `NGS_TEMPORAL_ADDRESS`.
- Add CLI flags such as `--output`, `--workdir`, `--backend`, and `--reference` so path and runtime choices are explicit.

Suggested portable config format:

```yaml
runtime:
  backend: podman
  work_dir: /work
  output_dir: /results

services:
  temporal_address: temporal:7233
  minio_endpoint: http://minio:9000
  minio_bucket: ngs-artifacts
```

### 3. README claims outpace implementation

The README describes the project as production-ready and single-command, but the implementation still reflects a transitional architecture.

The opening section says the system is a production-ready autonomous pipeline:

```text
**NGS-Agent v2** is a production-ready, autonomous RNA-Seq analysis pipeline that orchestrates quality control, alignment, quantification, differential expression analysis, and biological interpretation into a single command.
```

See [README.md](NGS-Agent-v2/README.md#L1-L12).

It also presents the pipeline as reproducible and modular, while the actual usage still depends on a local script runner and manual install steps.

Impact:

- Users may assume stronger guarantees than the software provides.
- Reviewers and operators cannot easily separate marketing language from supported behavior.

Proposed fix:

- Use consistent terminology throughout the docs.
- Label the older path as `Legacy Temporal workflow` and the newer command-line interface as the `new ngs CLI`.
- Rewrite the README to distinguish clearly between supported, experimental, and compatibility-only behavior.

### 4. Container story needs to be either real or explicit

The repository positions the system as modular and reproducible, but the container/build story needs to be more explicit before it can be treated as production-ready.

If container support remains in scope, the project should make runtime selection explicit via a config field or CLI flag, rather than implying that one container backend is automatically supported everywhere.

Impact:

- Users can be misled into thinking the container runtime is production-ready when it is still evolving.
- Build and runtime expectations can diverge across local machines, CI, and research servers.

Proposed fix:

- Add a `--backend` flag and a matching `NGS_CONTAINER_BACKEND` environment variable.
- Use `config/default.yaml` or `ngs.toml` to define runtime backends and artifact locations.
- If Dockerfiles remain in the repository, update them to use a multi-stage build, non-root runtime user, and a pinned Conda/Mamba environment.

Example container improvements:

```dockerfile
FROM mambaorg/micromamba:1.5.8 AS build
COPY environment.yml /tmp/environment.yml
RUN micromamba env create -n ngs -f /tmp/environment.yml

FROM ubuntu:24.04
RUN useradd -m -u 10001 ngs
COPY --from=build /opt/conda/envs/ngs /opt/conda/envs/ngs
ENV PATH=/opt/conda/envs/ngs/bin:$PATH
USER ngs
WORKDIR /work
ENTRYPOINT ["ngs"]
```

### 5. Testing is too thin for research use

The README points to two lightweight checks, which is a good start, but not enough for a workflow that manages sequencing data end to end.

```bash
python test_system.py
python test_e2e.py
```

See [README.md](NGS-Agent-v2/README.md#L72-L84).

Impact:

- Regressions in workflow orchestration can survive basic smoke tests.
- Report generation and artifact wiring can fail late.

Proposed fix:

- Add unit tests for config parsing, path discovery, and agent decision logic.
- Add integration tests for the full pipeline controller with mock FASTQ input.
- Add a regression test for report artifact generation.
- Add a container-build or workflow-start test if container execution remains supported.

### 6. Python version and lint targets should be aligned

The original review draft correctly calls out a Python version mismatch risk between linting and packaging. That issue matters because a workflow tool lives or dies by consistent developer and CI environments.

Impact:

- Linting can pass under one Python target while packaging or CI fails under another.
- Developers waste time chasing environment-specific errors.

Proposed fix:

- Pin the Python floor in packaging metadata.
- Make `pylint.yml`, `pyproject.toml`, and CI agree on the same supported version range.
- If lint configuration is split across files, document the source of truth clearly.

## Prioritized Recommendations

### Short-term

- Make the supported CLI and workflow entrypoints explicit.
- Replace ambiguous local-only assumptions with config-driven runtime values.
- Rewrite the README so it matches current behavior instead of aspirational behavior.
- Label any incomplete container support as experimental.

### Medium-term

- Add `--backend`, `--workdir`, `--output`, and `--reference` flags consistently.
- Add environment overrides for service endpoints and artifact roots.
- Expand tests to cover workflow startup, report creation, and error paths.
- Align Python version targets across packaging and linting.

### Long-term

- Merge the legacy and new execution models into one orchestration layer.
- Make container builds reproducible with multi-stage images and a non-root runtime user.
- Add CI coverage for native, container, and workflow execution.
- Publish a versioned operator guide for research and lab deployment.

## Conclusion

NGS-Agent v2 has a strong biological workflow concept, but three blockers remain before it is credible for high-profile research use: the execution model is split, portability is incomplete, and the documentation overstates maturity. Once the project has one canonical CLI, explicit runtime configuration, and a tested container/build path, it will be much easier to operate reliably in real research environments.

## References

- [README.md](NGS-Agent-v2/README.md#L1-L12)
- [README.md](NGS-Agent-v2/README.md#L38-L84)
- [README.md](NGS-Agent-v2/README.md#L120-L153)
- [README.md](NGS-Agent-v2/README.md#L206-L224)
- [pipelines/controller.py](NGS-Agent-v2/pipelines/controller.py#L61-L163)
- [pipelines/controller.py](NGS-Agent-v2/pipelines/controller.py#L192-L199)
- [scripts/run_ngs_agent.py](NGS-Agent-v2/scripts/run_ngs_agent.py#L22-L220)
