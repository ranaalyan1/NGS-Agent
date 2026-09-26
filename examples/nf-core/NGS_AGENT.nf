// Optional nf-core integration: set params.ngs_agent=true and provide both files.
// NGS-Agent only reads staged inputs; Docker's root filesystem is read-only.
process NGS_AGENT {
    tag 'advisory-qc'
    publishDir "${params.outdir ?: 'results'}/ngs-agent", mode: 'copy', pattern: 'ngs-agent-*.report.html'
    container 'ghcr.io/ranaalyan1/ngs-agent:latest'
    containerOptions '--read-only --tmpfs /tmp:rw,noexec,nosuid,size=16m'

    input:
    path nextflow_log
    path multiqc_output
    val failOnError

    output:
    path 'ngs-agent-*.report.html'

    script:
    """
    python - <<'PY'
from pathlib import Path
from core.assess import assess_path
from core.report import write_report
inputs = [Path('${nextflow_log}'), Path('${multiqc_output}')]
failed = False
for source in inputs:
    try:
        verdict = assess_path(source)
    except Exception as error:
        from core.models import KIND_UNKNOWN, unknown_verdict
        verdict = unknown_verdict(source.name, KIND_UNKNOWN, 'Input could not be interpreted safely; see ROADMAP.md.')
    state = 'failed' if any(f.severity == 'fail' for f in verdict.findings) else verdict.decision.lower()
    stem = ''.join(c if c.isalnum() or c in '._-' else '_' for c in source.name)
    write_report(verdict, '.', f'ngs-agent-{stem}-{state}')
    failed = failed or state == 'failed'
raise SystemExit(1 if '${failOnError}' == 'true' and failed else 0)
PY
    """
}

// Include/invoke only when enabled; this process is never part of the default run.
// NGS_AGENT(Channel.value(file(params.nextflow_log)), Channel.value(file(params.multiqc_output)), params.ngs_agent_fail_on_error ?: false)
