"""Report writer: Verdict -> one standalone HTML file + one JSON sidecar.

The HTML is the thing you forward to a PI, so:
  * it is a single file with inline CSS and no external assets — no fonts, no
    scripts, no images, no network calls; it renders in any mail client and
    survives being saved to a shared drive;
  * every finding shows its receipts, because a claim without a receipt is an
    opinion;
  * the footer carries the tool version, ruleset version, timestamp and the
    SHA-256 of the exact input, so a forwarded report can always be traced back.

The JSON sidecar is the machine-readable copy: it round-trips through
core.models without loss.
"""

from __future__ import annotations

import html
import json
from pathlib import Path

from .answer import (
    SECTION_MATTERS,
    SECTION_RECEIPTS,
    SECTION_TODO,
    SECTION_WHAT,
    Answer,
    answer_verdict,
)
from .models import DECISION_LABELS, SEVERITY_FAIL, Verdict
from .version import RULESET_VERSION, TOOL_VERSION

CSS = """
:root{--ink:#12181f;--muted:#5c6b7a;--line:#e3e8ee;--bg:#f6f8fa;--card:#ffffff;
--fail:#c0392b;--warn:#b9770e;--info:#1f6f8b;--pass:#1e7d46;}
*{box-sizing:border-box}
body{margin:0;padding:32px 16px;background:var(--bg);color:var(--ink);
font:16px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif;}
main{max-width:820px;margin:0 auto;}
h1{font-size:24px;margin:0 0 4px}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);
margin:32px 0 8px;border-bottom:1px solid var(--line);padding-bottom:6px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
padding:20px 22px;margin-bottom:14px}
.verdict{border-left:6px solid var(--pass)}
.verdict.fail{border-left-color:var(--fail)}
.verdict.warn{border-left-color:var(--warn)}
.verdict.unknown{border-left-color:var(--muted)}
.badge{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.06em;
text-transform:uppercase;padding:3px 9px;border-radius:99px;color:#fff;background:var(--info)}
.badge.fail{background:var(--fail)} .badge.warn{background:var(--warn)}
.badge.info{background:var(--info)} .badge.pass{background:var(--pass)}
.headline{font-size:20px;margin:10px 0 0 0}
.finding{border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:12px}
.finding.fail{border-left:4px solid var(--fail)}
.finding.warn{border-left:4px solid var(--warn)}
.finding.info{border-left:4px solid var(--info)}
.finding h3{font-size:16px;margin:0 0 6px}
.finding p{margin:4px 0}
.finding .lbl{font-weight:600;color:var(--muted)}
ol{padding-left:22px;margin:8px 0}
ul{padding-left:20px;margin:8px 0}
.receipt{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px;
color:var(--muted);word-break:break-all;margin:2px 0}
details{margin-top:10px;padding-top:8px;border-top:1px dashed var(--line)}
summary{cursor:pointer;color:var(--muted);font-size:14px}
footer{margin:32px 0 0;padding:14px 0 8px;border-top:1px solid var(--line);
color:var(--muted);font-size:12px;word-break:break-all}
code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;font-size:12px}
table{border-collapse:collapse;width:100%;font-size:14px}
td,th{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left}
"""

SVG_WIDTH = 720
SVG_HEIGHT = 160


def _esc(text: str) -> str:
    return html.escape(str(text), quote=True)


def _quality_svg(verdict: Verdict) -> str:
    """Inline SVG of the per-base quality curve. No external assets, no JS."""
    facts = verdict.details.get("facts") or {}
    points = facts.get("per_base_quality") or []
    if len(points) < 2:
        return ""
    ys = [p["y"] for p in points]
    lo, hi = 0.0, max(40.0, max(ys) + 2)
    step = SVG_WIDTH / (len(points) - 1)
    coords = []
    for i, y in enumerate(ys):
        x = i * step
        py = SVG_HEIGHT - ((y - lo) / (hi - lo)) * SVG_HEIGHT
        coords.append(f"{x:.1f},{py:.1f}")
    polyline = " ".join(coords)
    q20_y = SVG_HEIGHT - ((20 - lo) / (hi - lo)) * SVG_HEIGHT
    q30_y = SVG_HEIGHT - ((30 - lo) / (hi - lo)) * SVG_HEIGHT
    return (
        f'<svg viewBox="0 0 {SVG_WIDTH} {SVG_HEIGHT}" width="100%" height="{SVG_HEIGHT}" '
        'role="img" aria-label="Quality score along the read">'
        f'<rect x="0" y="0" width="{SVG_WIDTH}" height="{SVG_HEIGHT}" fill="#fbfcfd"/>'
        f'<line x1="0" y1="{q30_y:.1f}" x2="{SVG_WIDTH}" y2="{q30_y:.1f}" '
        'stroke="#cfe3d4" stroke-dasharray="4 4"/>'
        f'<line x1="0" y1="{q20_y:.1f}" x2="{SVG_WIDTH}" y2="{q20_y:.1f}" '
        'stroke="#f0c9c2" stroke-dasharray="4 4"/>'
        f'<polyline points="{polyline}" fill="none" stroke="#1f6f8b" stroke-width="2"/>'
        f'<text x="6" y="{q20_y - 5:.1f}" font-size="11" fill="#c0392b">Q20</text>'
        f'<text x="6" y="{q30_y - 5:.1f}" font-size="11" fill="#1e7d46">Q30</text>'
        "</svg>"
    )


def _badge_class(verdict: Verdict) -> str:
    if verdict.decision == "UNKNOWN":
        return "unknown"
    if any(f.severity == SEVERITY_FAIL for f in verdict.findings):
        return "fail"
    if verdict.findings:
        return "warn"
    return "pass"


def render_html(verdict: Verdict, answer: Answer | None = None) -> str:
    """One self-contained HTML document. Forwardable, printable, offline."""
    answer = answer or answer_verdict(verdict)
    what = answer.section(SECTION_WHAT)
    matters = answer.section(SECTION_MATTERS)
    todo = answer.section(SECTION_TODO)
    receipts = answer.section(SECTION_RECEIPTS)

    decision_label = DECISION_LABELS.get(verdict.decision, verdict.decision)
    badge_class = _badge_class(verdict)

    parts: list[str] = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>NGS-Agent report: {_esc(verdict.subject)}</title>",
        f"<style>{CSS}</style>",
        "</head><body><main>",
        f'<div class="card verdict {badge_class}">',
        f"<h1>{_esc(verdict.subject)}</h1>",
        f'<span class="badge {badge_class}">{_esc(decision_label)}</span>',
        f'<p class="headline">{_esc(verdict.headline)}</p>',
        "</div>",
    ]

    # WHAT THIS IS
    parts.append(f"<h2>{_esc(SECTION_WHAT)}</h2>")
    parts.append('<div class="card">')
    parts.append(f"<p>{_esc(what.lines[0])}</p>")
    if len(what.lines) > 1:
        bottom_line = what.lines[1].replace("Bottom line: ", "")
        parts.append(f"<p><strong>Bottom line.</strong> {_esc(bottom_line)}</p>")
    svg = _quality_svg(verdict)
    if svg:
        parts.append(svg)
        parts.append(
            '<p style="color:var(--muted);font-size:13px">'
            "Quality along the read. Dotted lines mark Q20 and Q30.</p>"
        )
    parts.append("</div>")

    # WHAT MATTERS
    parts.append(f"<h2>{_esc(SECTION_MATTERS)}</h2>")
    severity_order = {"fail": 0, "warn": 1, "info": 2}
    findings = sorted(
        verdict.findings,
        key=lambda f: (severity_order.get(f.severity, 9), f.id),
    )
    if findings:
        for finding in findings:
            parts.append(f'<div class="finding {_esc(finding.severity)}">')
            parts.append(f"<h3>{_esc(finding.title)}</h3>")
            parts.append(f'<p><span class="lbl">What it is.</span> {_esc(finding.what)}</p>')
            parts.append(f'<p><span class="lbl">What it means.</span> {_esc(finding.meaning)}</p>')
            parts.append(f'<p><span class="lbl">Do this.</span> {_esc(finding.action)}</p>')
            parts.append("<details><summary>Receipts and technical details</summary>")
            for receipt in finding.receipts:
                parts.append(
                    f'<p class="receipt">{_esc(receipt.source)} @ {_esc(receipt.locator)}'
                    + (f" — {_esc(receipt.detail)}" if receipt.detail else "")
                    + "</p>"
                )
            parts.append(
                f'<p class="receipt">rule {_esc(finding.id)} · '
                f"severity {_esc(finding.severity)}</p>"
            )
            if finding.details:
                parts.append("<table>")
                for key, value in finding.details.items():
                    parts.append(f"<tr><th>{_esc(key)}</th><td>{_esc(value)}</td></tr>")
                parts.append("</table>")
            parts.append("</details>")
            parts.append("</div>")
    else:
        parts.append('<div class="card">')
        for line in matters.lines:
            parts.append(f"<p>{_esc(line)}</p>")
        parts.append("</div>")

    # WHAT TO DO
    parts.append(f"<h2>{_esc(SECTION_TODO)}</h2>")
    parts.append('<div class="card"><ol>')
    for line in todo.lines:
        parts.append(f"<li>{_esc(line)}</li>")
    parts.append("</ol></div>")

    # RECEIPTS
    parts.append(f"<h2>{_esc(SECTION_RECEIPTS)}</h2>")
    parts.append('<div class="card">')
    for line in receipts.lines:
        parts.append(f'<p class="receipt">{_esc(line)}</p>')
    parts.append("</div>")

    # Footer: versions, timestamp, input hash.
    sha = verdict.details.get("input_sha256") or _sha_from_receipts(verdict)
    parts.append("<footer>")
    parts.append(
        f"ngs-agent {_esc(verdict.tool_version)} · ruleset {_esc(verdict.ruleset_version)} · "
        f"generated {_esc(verdict.timestamp)}"
    )
    if sha:
        parts.append(f"<br>input SHA-256: <code>{_esc(sha)}</code>")
    parts.append(
        "<br>Every statement above is backed by the receipt listed beside it. "
        "Where evidence was missing, the answer says so."
    )
    parts.append("</footer></main></body></html>")
    return "\n".join(parts)


def _sha_from_receipts(verdict: Verdict) -> str:
    for receipt in verdict.receipts:
        if receipt.version.startswith("sha256:"):
            return receipt.version.removeprefix("sha256:")
    return ""


def render_json(verdict: Verdict, answer: Answer | None = None) -> str:
    """The JSON sidecar: the full Verdict, receipts included, plus the text."""
    answer = answer or answer_verdict(verdict)
    payload = {
        "schema": "ngs-agent/verdict/1",
        "tool_version": TOOL_VERSION,
        "ruleset_version": RULESET_VERSION,
        "answer": {block.heading: block.lines for block in answer.blocks},
        "verdict": verdict.to_dict(),
    }
    return json.dumps(payload, indent=2)


def write_report(
    verdict: Verdict, out_dir: str | Path, stem: str | None = None
) -> tuple[Path, Path]:
    """Write report.html + report.json next to each other. Returns both paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    stem = stem or _safe_stem(verdict.subject)
    html_path = out / f"{stem}.report.html"
    json_path = out / f"{stem}.report.json"
    answer = answer_verdict(verdict)
    html_path.write_text(render_html(verdict, answer), encoding="utf-8")
    json_path.write_text(render_json(verdict, answer), encoding="utf-8")
    return html_path, json_path


def _safe_stem(subject: str) -> str:
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in subject).strip("._")
    return safe or "report"
