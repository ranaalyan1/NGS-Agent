"""Interactive HTML and Markdown clinical report generator."""

from __future__ import annotations

import datetime
from pathlib import Path
from typing import List, Optional

from ngs_agent.analyzer import Variant
from ngs_agent.debate import DebateResult
from ngs_agent.qc import QCMetric


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>NGS-Agent Clinical & Variant Report</title>
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --accent: #38bdf8;
      --green: #22c55e;
      --yellow: #eab308;
      --red: #ef4444;
      --border: #334155;
    }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      margin: 0;
      padding: 2rem;
      line-height: 1.5;
    }
    .container {
      max-width: 1200px;
      margin: 0 auto;
    }
    header {
      border-bottom: 1px solid var(--border);
      padding-bottom: 1.5rem;
      margin-bottom: 2rem;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    h1 { margin: 0; font-size: 1.8rem; color: var(--accent); }
    .badge {
      display: inline-block;
      padding: 0.25rem 0.6rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 600;
      text-transform: uppercase;
    }
    .badge-pass, .badge-benign { background: rgba(34, 197, 94, 0.2); color: var(--green); border: 1px solid var(--green); }
    .badge-warn, .badge-vus { background: rgba(234, 179, 8, 0.2); color: var(--yellow); border: 1px solid var(--yellow); }
    .badge-fail, .badge-pathogenic { background: rgba(239, 68, 68, 0.2); color: var(--red); border: 1px solid var(--red); }
    
    .grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
      gap: 1rem;
      margin-bottom: 2rem;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 0.5rem;
      padding: 1.25rem;
    }
    .metric-title { font-size: 0.85rem; color: var(--text-muted); text-transform: uppercase; margin-bottom: 0.25rem; }
    .metric-value { font-size: 1.5rem; font-weight: 700; display: flex; justify-content: space-between; align-items: center; }
    
    table {
      width: 100%;
      border-collapse: collapse;
      margin-bottom: 2rem;
      background: var(--card-bg);
      border-radius: 0.5rem;
      overflow: hidden;
      border: 1px solid var(--border);
    }
    th, td {
      padding: 0.85rem 1rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
    }
    th { background: #162032; color: var(--accent); font-size: 0.85rem; text-transform: uppercase; }
    tr:hover { background: rgba(255, 255, 255, 0.02); }
    
    .debate-section {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 0.5rem;
      padding: 1.5rem;
      margin-bottom: 2rem;
    }
    .opinion-box {
      border-left: 3px solid var(--accent);
      padding-left: 1rem;
      margin: 1rem 0;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>NGS-Agent Clinical & Variant Report</h1>
        <div style="color: var(--text-muted); font-size: 0.9rem;">Automated Multi-Agent Interpretation</div>
      </div>
      <div style="text-align: right; color: var(--text-muted); font-size: 0.85rem;">
        Generated: __DATE__<br>
        Status: <strong>Ready</strong>
      </div>
    </header>

    <h2>Quality Control Overview</h2>
    <div class="grid">
      __QC_CARDS__
    </div>

    <h2>Annotated Variants (__TOTAL_VARIANTS__)</h2>
    <table>
      <thead>
        <tr>
          <th>Gene</th>
          <th>Location</th>
          <th>Consequence</th>
          <th>ClinVar</th>
          <th>AF</th>
          <th>Depth / VAF</th>
          <th>Classification</th>
        </tr>
      </thead>
      <tbody>
        __VARIANT_ROWS__
      </tbody>
    </table>

    __DEBATE_SECTION__
  </div>
</body>
</html>
"""


def generate_html_report(
    variants: List[Variant],
    qc_metrics: Optional[List[QCMetric]] = None,
    debates: Optional[List[DebateResult]] = None,
    output_path: Optional[Path] = None,
) -> str:
    """Generate a self-contained interactive HTML report."""
    now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Build QC Cards
    qc_cards_html = ""
    if qc_metrics:
        for q in qc_metrics:
            badge_class = f"badge-{q.status.lower()}"
            qc_cards_html += f"""
            <div class="card">
              <div class="metric-title">{q.name}</div>
              <div class="metric-value">
                <span>{q.value}</span>
                <span class="badge {badge_class}">{q.status}</span>
              </div>
            </div>
            """
    else:
        qc_cards_html = "<div class='card'>No QC metrics provided</div>"

    # Build Variant Rows
    variant_rows_html = ""
    for v in variants:
        if v.is_pathogenic:
            badge = "<span class='badge badge-pathogenic'>Pathogenic</span>"
        elif v.is_vus:
            badge = "<span class='badge badge-vus'>VUS</span>"
        else:
            badge = "<span class='badge badge-benign'>Other / Benign</span>"

        af_str = f"{v.af:.5f}" if v.af is not None else "—"
        dv_str = f"{v.depth or '—'} / {f'{v.vaf:.0%}' if v.vaf is not None else '—'}"
        loc = f"{v.chrom}:{v.pos} {v.ref}>{v.alt}"

        variant_rows_html += f"""
        <tr>
          <td><strong>{v.gene}</strong></td>
          <td><code>{loc}</code></td>
          <td>{v.consequence}</td>
          <td>{v.clinvar}</td>
          <td>{af_str}</td>
          <td>{dv_str}</td>
          <td>{badge}</td>
        </tr>
        """

    # Build Debate Section
    debate_section_html = ""
    if debates:
        debate_section_html = "<h2>Multi-Agent VUS Debates</h2>"
        for d in debates:
            opinions_html = ""
            for op in d.opinions:
                opinions_html += f"""
                <div class="opinion-box">
                  <strong>{op.persona}</strong> — <span class="badge badge-warn">{op.stance}</span>
                  <p style="margin: 0.25rem 0 0 0; color: var(--text-muted); font-size: 0.9rem;">{op.reasoning}</p>
                </div>
                """
            debate_section_html += f"""
            <div class="debate-section">
              <h3>{d.variant.gene} ({d.variant.chrom}:{d.variant.pos} {d.variant.ref}>{d.variant.alt})</h3>
              {opinions_html}
              <div style="margin-top: 1rem; padding-top: 0.75rem; border-top: 1px solid var(--border);">
                <strong>Consensus:</strong> {d.consensus}<br>
                <strong>Recommendation:</strong> {d.recommendation}
              </div>
            </div>
            """

    html = (
        HTML_TEMPLATE.replace("__DATE__", now_str)
        .replace("__QC_CARDS__", qc_cards_html)
        .replace("__TOTAL_VARIANTS__", str(len(variants)))
        .replace("__VARIANT_ROWS__", variant_rows_html)
        .replace("__DEBATE_SECTION__", debate_section_html)
    )

    if output_path:
        output_path.write_text(html, encoding="utf-8")

    return html
