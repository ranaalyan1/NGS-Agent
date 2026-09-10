"""Explicit, auditable templates for the NGS run report.

Every claim rendered into the HTML report comes from one of exactly four
inputs, listed here so the report can be audited against them:

1. ``RunMetadata`` — workflow, objective, run id, timestamps.
2. ``StepRow`` — one row per plan step: status, return code, duration, tool,
   and the exact command that ran (from the step outcome's provenance).
3. ``VerificationSummary`` — pass/fail, issues, missing artifacts.
4. ``ProvenanceSummary`` — tool versions and the manifest of artifact
   checksums written next to the report.

All interpolated values are HTML-escaped (``html.escape``), including the
AI-insights block, so model output or tool stderr can never inject markup.
The HTML below is the single source of truth for the report layout — no
hidden rendering layer sits between this module and ``report.html``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, field
from typing import Any

CSS = """
      body {
        font-family: Inter, Arial, sans-serif; margin: 2rem;
        color: #e2e8f0; background: #0f172a;
      }
      .card {
        background: #111827; border: 1px solid #334155; border-radius: 16px;
        padding: 1.5rem; margin-bottom: 1rem;
      }
      h1, h2 { color: #f8fafc; margin-top: 0; }
      code, pre { background: #1e293b; padding: 0.2rem 0.4rem; border-radius: 6px; }
      pre { padding: 1rem; overflow-x: auto; }
      ul { line-height: 1.7; }
      table { border-collapse: collapse; width: 100%; }
      th, td {
        border: 1px solid #334155; padding: 0.5rem 0.7rem;
        text-align: left; font-size: 0.92rem;
      }
      th { background: #1e293b; }
      .ok { color: #4ade80; } .failed { color: #f87171; } .skipped { color: #facc15; }
      .dry-run { color: #93c5fd; } .muted { color: #94a3b8; }
      a { color: #93c5fd; }
"""


@dataclass
class RunMetadata:
    workflow: str
    objective: str
    status: str
    generated_at: str
    run_id: str = "-"
    backend: str = "-"


@dataclass
class StepRow:
    name: str
    status: str
    returncode: int | str
    duration: str
    tool: str
    command: str


@dataclass
class VerificationSummary:
    passed: bool
    planned_steps: int
    executed_steps: int
    missing_artifacts: list[str] = field(default_factory=list)
    issues: list[tuple[str, str]] = field(default_factory=list)  # (severity, message)


@dataclass
class ProvenanceSummary:
    manifest_path: str = ""
    artifact_count: int = 0
    total_bytes: int = 0
    tool_versions: dict[str, str] = field(default_factory=dict)  # tool -> version string


def _esc(value: Any) -> str:
    return html.escape(str(value), quote=True)


def _status_class(status: str) -> str:
    return {
        "ok": "ok",
        "failed": "failed",
        "skipped": "skipped",
        "dry-run": "dry-run",
    }.get(status.lower(), "muted")


def render_html(
    metadata: RunMetadata,
    steps: list[StepRow],
    verification: VerificationSummary,
    provenance: ProvenanceSummary,
    ai_insights: str,
    risks: list[str],
    next_actions: list[str],
) -> str:
    """Render the full HTML report. Pure function of its arguments."""
    insights_block = (
        _esc(ai_insights)
        if ai_insights.strip()
        else "No AI insights available (no API key configured or generation skipped)."
    )
    step_rows = (
        "\n".join(
            f"        <tr>"
            f"<td>{_esc(row.name)}</td>"
            f"<td class='{_status_class(row.status)}'>{_esc(row.status)}</td>"
            f"<td>{_esc(row.returncode)}</td>"
            f"<td>{_esc(row.duration)}</td>"
            f"<td>{_esc(row.tool)}</td>"
            f"<td><code>{_esc(row.command)}</code></td>"
            f"</tr>"
            for row in steps
        )
        or "        <tr><td colspan='6' class='muted'>No steps executed.</td></tr>"
    )

    def _issue_row(severity: str, message: str) -> str:
        css_class = _status_class("failed" if severity.lower() == "error" else "skipped")
        return (
            f"        <tr><td class='{css_class}'>"
            f"{_esc(severity)}</td><td>{_esc(message)}</td></tr>"
        )

    issue_rows = (
        "\n".join(_issue_row(severity, message) for severity, message in verification.issues)
        or "        <tr><td colspan='2' class='muted'>No issues reported.</td></tr>"
    )

    missing = (
        ", ".join(_esc(item) for item in verification.missing_artifacts)
        if verification.missing_artifacts
        else "none"
    )
    risk_items = (
        "\n".join(f"      <li>{_esc(risk)}</li>" for risk in risks)
        or "      <li class='muted'>None recorded.</li>"
    )
    action_items = (
        "\n".join(f"      <li>{_esc(action)}</li>" for action in next_actions)
        or "      <li class='muted'>None recorded.</li>"
    )
    version_rows = (
        "\n".join(
            f"        <tr><td>{_esc(tool)}</td><td><code>{_esc(version)}</code></td></tr>"
            for tool, version in sorted(provenance.tool_versions.items())
        )
        or "        <tr><td colspan='2' class='muted'>No tool versions recorded.</td></tr>"
    )

    manifest_link = (
        f"<a href='{_esc(provenance.manifest_path)}'>{_esc(provenance.manifest_path)}</a>"
        if provenance.manifest_path
        else "<span class='muted'>not written</span>"
    )

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>NGS Report - {_esc(metadata.workflow)}</title>
    <style>{CSS}</style>
  </head>
  <body>
    <div class="card">
      <h1>NGS Run Report</h1>
      <p><strong>Workflow:</strong> {_esc(metadata.workflow)}</p>
      <p><strong>Objective:</strong> {_esc(metadata.objective)}</p>
      <p><strong>Status:</strong> {_esc(metadata.status)}</p>
      <p><strong>Run ID:</strong> {_esc(metadata.run_id)}</p>
      <p><strong>Backend:</strong> {_esc(metadata.backend)}</p>
      <p><strong>Generated:</strong> {_esc(metadata.generated_at)}</p>
    </div>
    <div class="card">
      <h2>AI Insights</h2>
      <pre>{insights_block}</pre>
    </div>
    <div class="card">
      <h2>Steps Executed</h2>
      <table>
        <tr><th>Step</th><th>Status</th><th>RC</th><th>Duration</th><th>Tool</th><th>Command</th></tr>
{step_rows}
      </table>
    </div>
    <div class="card">
      <h2>Verification</h2>
      <p>Planned steps: {_esc(verification.planned_steps)};
        executed steps: {_esc(verification.executed_steps)}</p>
      <p>Missing artifacts: {missing}</p>
      <table>
        <tr><th>Severity</th><th>Issue</th></tr>
{issue_rows}
      </table>
    </div>
    <div class="card">
      <h2>Provenance</h2>
      <p>Manifest: {manifest_link}
        ({_esc(provenance.artifact_count)} artifacts, {_esc(provenance.total_bytes)} bytes)</p>
      <table>
        <tr><th>Tool</th><th>Version</th></tr>
{version_rows}
      </table>
    </div>
    <div class="card">
      <h2>Risks</h2>
      <ul>
{risk_items}
      </ul>
    </div>
    <div class="card">
      <h2>Next Actions</h2>
      <ul>
{action_items}
      </ul>
    </div>
  </body>
</html>
"""


def render_markdown(
    metadata: RunMetadata,
    steps: list[StepRow],
    verification: VerificationSummary,
    provenance: ProvenanceSummary,
    ai_insights: str,
    risks: list[str],
    next_actions: list[str],
) -> str:
    """Render a markdown mirror of the HTML report."""
    missing_artifacts_label = (
        ", ".join(verification.missing_artifacts) if verification.missing_artifacts else "none"
    )
    lines = [
        "# NGS Run Report",
        "",
        f"- Workflow: {metadata.workflow}",
        f"- Objective: {metadata.objective}",
        f"- Status: {metadata.status}",
        f"- Run ID: {metadata.run_id}",
        f"- Backend: {metadata.backend}",
        f"- Generated: {metadata.generated_at}",
        "",
        "## Steps",
        "",
        "| Step | Status | RC | Duration | Tool |",
        "| --- | --- | --- | --- | --- |",
    ]
    for row in steps:
        lines.append(
            f"| {row.name} | {row.status} | {row.returncode} | {row.duration} | {row.tool} |"
        )
    lines += [
        "",
        "## Verification",
        "",
        f"- Planned steps: {verification.planned_steps}; executed: {verification.executed_steps}",
        f"- Missing artifacts: {missing_artifacts_label}",
        "",
    ]
    for severity, message in verification.issues:
        lines.append(f"- [{severity}] {message}")
    lines += [
        "",
        "## Provenance",
        "",
        f"- Manifest: {provenance.manifest_path or 'not written'} "
        f"({provenance.artifact_count} artifacts, {provenance.total_bytes} bytes)",
    ]
    for tool, version in sorted(provenance.tool_versions.items()):
        lines.append(f"- {tool}: {version}")
    if ai_insights.strip():
        lines += ["", "## AI Insights", "", ai_insights]
    if risks:
        lines += ["", "## Risks", ""]
        lines.extend(f"- {risk}" for risk in risks)
    if next_actions:
        lines += ["", "## Next Actions", ""]
        lines.extend(f"- {action}" for action in next_actions)
    return "\n".join(lines) + "\n"
