"""MultiQC parser: a combined quality summary in, per-sample facts out.

MultiQC aggregates per-sample QC (usually FastQC) into one report. Labs keep
it in three shapes, and all three are read here — content decides, never the
file name:

* ``multiqc_data.json`` — the machine-readable sidecar: general-stats numbers
  per sample plus, when the report carries them, the per-position plot data
  (quality, adapter, GC, N content) that the curve rules need;
* a general-stats table (``multiqc_general_stats.txt``) — one tab-separated
  row per sample with the summary numbers;
* ``multiqc_report.html`` — the forwarded report: the General Statistics table
  is recovered from the markup with the standard library HTML parser.

What each shape can support differs, and the facts say so honestly: table and
HTML sources carry no per-position curves, so the curve rules stay silent on
them instead of guessing. Anything unparseable raises ``MultiQCParseError``
and the caller answers "unknown" with the reason attached.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

from ..models import MultiQCFacts, MultiQCSample, Point
from ..util import sha256_file
from .nextflow_log import read_log_text

FORMAT_JSON = "json"
FORMAT_GENERAL_STATS = "general_stats"
FORMAT_HTML = "html"

JSON_MARKERS = (
    "report_general_stats_data",
    "report_plot_data",
    "report_saved_raw_data",
    "report_multiqc_version",
)

#: Plot identifiers (current and older spellings) for the four FastQC curves.
QUALITY_PLOT_IDS = (
    "fastqc_per_base_sequence_quality_plot",
    "fastqc_per_base_sequence_quality",
    "per_base_sequence_quality",
)
ADAPTER_PLOT_IDS = (
    "fastqc_adapter_content_plot",
    "fastqc_adapter_content",
    "adapter_content",
)
GC_PLOT_IDS = (
    "fastqc_per_sequence_gc_content_plot",
    "fastqc_per_sequence_gc_content_plot_counts",
    "fastqc_per_sequence_gc_content",
    "per_sequence_gc_content",
)
N_PLOT_IDS = (
    "fastqc_per_base_n_content_plot",
    "fastqc_per_base_n_content",
    "per_base_n_content",
)


class MultiQCParseError(Exception):
    """The file looked like MultiQC output but its data could not be read."""


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _num(value: Any) -> float | None:
    """A metric cell as a float. Dicts, lists and prose are not numbers."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number if number == number else None
    if isinstance(value, str):
        text = value.strip().replace(",", "").rstrip("%").strip()
        if text in ("", "-", "NA", "N/A", "null", "None"):
            return None
        # Display units some tables append ("150 bp", "20.0 M"): the number leads.
        head = text.split()[0] if text.split() else ""
        try:
            number = float(head)
        except ValueError:
            return None
        return number if number == number else None
    return None


def _bin_start(label: str) -> float:
    """'10-19' -> 10.0 ; '7' -> 7.0. Same bins FastQC itself writes."""
    head = str(label).split("-")[0].strip()
    try:
        return float(head)
    except ValueError:
        return float("nan")


def _candidates(name: str) -> set[str]:
    """Plausible spellings of one metric header, lower-cased.

    MultiQC namespaces its columns per module (``FastQC_percent_duplicates``)
    while older exports and hand-made tables use the bare name, so every
    lookup tries the full name, the suffix after the module prefix, and the
    display spelling with spaces and units removed.
    """
    lowered = name.strip().lower()
    out = {lowered}
    if "_" in lowered:
        out.add(lowered.split("_", 1)[1])
    if " - " in lowered:
        out.add(lowered.split(" - ", 1)[1])
    squashed = lowered.replace(" ", "").replace("%", "percent").replace("(millions)", "")
    out.add(squashed)
    return out


# Canonical metric names, each with every header spelling we accept.
DUP_KEYS = {"percent_duplicates", "percentdups", "pct_duplication", "duplication_rate"}
GC_KEYS = {"percent_gc", "percentgc", "after_filtering_gc_content", "gc_content"}
LEN_KEYS = {
    "avg_sequence_length",
    "median_sequence_length",
    "average_sequence_length",
    "averagereadlength",
    "medianreadlength",
    "length",
}
TOTAL_KEYS = {"total_sequences", "totalsequences", "mseqs", "total_reads"}
FAILS_KEYS = {"percent_fails", "percentfails", "percent_failed"}
QUAL_KEYS = {"avg_sequence_quality", "median_sequence_quality", "mean_quality"}
ADAPT_KEYS = {"pct_adapter", "percent_adapter", "percentadapter", "adapter_content"}

_METRIC_TABLE = (
    ("duplication_percent", DUP_KEYS),
    ("gc_percent", GC_KEYS),
    ("read_length", LEN_KEYS),
    ("total_sequences", TOTAL_KEYS),
    ("fails_percent", FAILS_KEYS),
    ("mean_quality", QUAL_KEYS),
    ("adapter_percent", ADAPT_KEYS),
)


def _match_metric(header: str) -> str | None:
    spellings = _candidates(header)
    for canonical, keys in _METRIC_TABLE:
        if spellings & keys:
            return canonical
    return None


def _metric_from_dict(metrics: dict[str, Any], canonical: str) -> float | None:
    for key, value in metrics.items():
        if _match_metric(str(key)) == canonical:
            number = _num(value)
            if number is not None:
                return number
    return None


def _fill_sample(sample: MultiQCSample, metrics: dict[str, Any]) -> None:
    dup = _metric_from_dict(metrics, "duplication_percent")
    if dup is not None:
        sample.duplication_percent = dup
    gc = _metric_from_dict(metrics, "gc_percent")
    if gc is not None:
        sample.gc_percent = gc
    length = _metric_from_dict(metrics, "read_length")
    if length is not None:
        sample.read_length = int(length)
    total = _metric_from_dict(metrics, "total_sequences")
    if total is not None:
        sample.total_sequences = int(total)
    fails = _metric_from_dict(metrics, "fails_percent")
    if fails is not None:
        sample.fails_percent = fails
    qual = _metric_from_dict(metrics, "mean_quality")
    if qual is not None:
        sample.mean_quality = qual
    adapt = _metric_from_dict(metrics, "adapter_percent")
    if adapt is not None:
        sample.adapter_percent = adapt


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def parse_multiqc(path: str | Path) -> MultiQCFacts:
    """Extract per-sample facts from MultiQC output in any of its shapes."""
    p = Path(path)
    facts = MultiQCFacts(source_path=str(p), source_sha256=sha256_file(p))
    try:
        text = read_log_text(p)
    except OSError as exc:
        raise MultiQCParseError(f"{p} could not be read: {exc}") from exc
    if not text.strip():
        raise MultiQCParseError(f"{p} is empty")

    stripped = text.lstrip()
    if stripped.startswith("{") and any(marker in text for marker in JSON_MARKERS):
        facts.format = FORMAT_JSON
        _parse_json(text, facts)
    elif _is_general_stats(text):
        facts.format = FORMAT_GENERAL_STATS
        _parse_general_stats(text, facts)
    elif _is_html(text):
        facts.format = FORMAT_HTML
        _parse_html(text, facts)
    else:
        raise MultiQCParseError(
            f"{p} was recognised as MultiQC output but none of its three readable "
            "shapes (data JSON, general-stats table, report HTML) could be parsed"
        )
    if not facts.samples:
        raise MultiQCParseError(f"{p} carries no per-sample numbers to judge")
    return facts


def _is_general_stats(text: str) -> bool:
    for line in text.splitlines():
        if not line.strip():
            continue
        cells = line.split("\t")
        return len(cells) >= 3 and cells[0].strip().lower() in ("sample", "sample name")
    return False


def _is_html(text: str) -> bool:
    lowered = text[:4096].lower()
    return "<html" in lowered or "<table" in lowered


# --------------------------------------------------------------------------
# JSON: multiqc_data.json
# --------------------------------------------------------------------------
def _parse_json(text: str, facts: MultiQCFacts) -> None:
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MultiQCParseError(f"invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise MultiQCParseError("top-level JSON is not an object")
    version = data.get("report_multiqc_version") or data.get("multiqc_version") or ""
    facts.multiqc_version = str(version)

    merged = _merge_general_stats(data.get("report_general_stats_data"))
    if not merged:
        # Some reports only carry plot data; samples are recovered from there.
        merged = {name: {} for name in _samples_from_plots(data.get("report_plot_data"))}
    plots = data.get("report_plot_data")
    for name in sorted(merged):
        metrics = merged[name]
        sample = MultiQCSample(name=name)
        if isinstance(metrics, dict):
            _fill_sample(sample, metrics)
        if isinstance(plots, dict):
            sample.per_base_quality = _series_for_plot(plots, QUALITY_PLOT_IDS, name)
            sample.adapter_content = _series_for_plot(plots, ADAPTER_PLOT_IDS, name)
            sample.gc_curve = _series_for_plot(plots, GC_PLOT_IDS, name)
            sample.n_content = _series_for_plot(plots, N_PLOT_IDS, name)
        facts.samples.append(sample)


def _merge_general_stats(stats: Any) -> dict[str, dict[str, Any]]:
    """General-stats numbers as sample -> metrics, across MultiQC versions.

    v1 stores one dict per module in a list; newer exports store one dict;
    either way the per-module dicts map sample names to metric dicts, and
    merging them is right because modules report different metrics.
    """
    merged: dict[str, dict[str, Any]] = {}
    if isinstance(stats, dict):
        modules: list[Any] = [stats]
    elif isinstance(stats, list):
        modules = stats
    else:
        return merged
    for module in modules:
        if isinstance(module, dict):
            for sample, metrics in module.items():
                if isinstance(metrics, dict):
                    merged.setdefault(str(sample), {}).update(metrics)
        elif isinstance(module, list):
            # Row-shaped exports: [{"sample": ..., "metric": ...}, ...].
            for row in module:
                if not isinstance(row, dict):
                    continue
                name = row.get("sample") or row.get("Sample") or row.get("sample_name")
                if name:
                    merged.setdefault(str(name), {}).update(row)
    return merged


def _samples_from_plots(plots: Any) -> list[str]:
    names: set[str] = set()
    if not isinstance(plots, dict):
        return []
    for plot in plots.values():
        for dataset in _datasets_of(plot):
            if isinstance(dataset, dict):
                names.update(str(k) for k in dataset)
            elif isinstance(dataset, list):
                for series in dataset:
                    if isinstance(series, dict) and "name" in series:
                        names.add(str(series["name"]))
    return sorted(names)


def _datasets_of(plot: Any) -> list[Any]:
    if isinstance(plot, dict) and isinstance(plot.get("datasets"), list):
        return plot["datasets"]
    if isinstance(plot, dict):
        return [plot]
    if isinstance(plot, list):
        return [plot]
    return []


def _series_for_plot(plots: dict[str, Any], plot_ids: tuple[str, ...], sample: str) -> list[Point]:
    for plot_id in plot_ids:
        if plot_id in plots:
            pairs = _extract_series(plots[plot_id], sample)
            if pairs:
                return _to_points(pairs)
    # Fall back to any plot whose identifier carries the same tail.
    tail = plot_ids[-1]
    for plot_id, plot in plots.items():
        if plot_id not in plot_ids and tail in str(plot_id):
            pairs = _extract_series(plot, sample)
            if pairs:
                return _to_points(pairs)
    return []


def _extract_series(plot: Any, sample: str) -> list[tuple[Any, Any]]:
    for dataset in _datasets_of(plot):
        if isinstance(dataset, dict):
            if sample in dataset:
                pairs = _coerce_series(dataset[sample])
                if pairs:
                    return pairs
        elif isinstance(dataset, list):
            for series in dataset:
                if isinstance(series, dict) and str(series.get("name")) == sample:
                    pairs = _coerce_series(series.get("data", series))
                    if pairs:
                        return pairs
    return []


def _coerce_series(value: Any) -> list[tuple[Any, Any]]:
    """A per-sample series in any of the shapes MultiQC versions emit."""
    if isinstance(value, dict) and "data" in value and len(value) <= 3:
        coerced = _coerce_series(value["data"])
        if coerced:
            return coerced
    if isinstance(value, dict):
        pairs: list[tuple[Any, Any]] = []
        for x, y in value.items():
            if isinstance(y, dict):
                # One line per adapter: the worst adapter at each position.
                leaves = [_num(leaf) for leaf in y.values()]
                leaves = [leaf for leaf in leaves if leaf is not None]
                y = max(leaves) if leaves else None
            number = _num(y)
            if number is not None:
                pairs.append((x, number))
        return pairs
    if isinstance(value, list):
        if value and all(isinstance(item, (list, tuple)) and len(item) >= 2 for item in value):
            pairs = []
            for item in value:
                number = _num(item[1])
                if number is not None:
                    pairs.append((item[0], number))
            return pairs
        pairs = [(index, number) for index, number in enumerate(_num(v) for v in value)]
        return [(x, y) for x, y in pairs if y is not None]
    return []


def _to_points(pairs: list[tuple[Any, Any]]) -> list[Point]:
    points: list[Point] = []
    for x, y in pairs:
        try:
            fx = float(x)
        except (TypeError, ValueError):
            fx = _bin_start(x)
        if fx != fx:
            continue
        points.append(Point(x=fx, y=float(y), label=str(x)))
    points.sort(key=lambda p: p.x)
    return points


# --------------------------------------------------------------------------
# General-stats table: multiqc_general_stats.txt
# --------------------------------------------------------------------------
def _parse_general_stats(text: str, facts: MultiQCFacts) -> None:
    lines = text.splitlines()
    header: list[str] = []
    header_line = 0
    for lineno, line in enumerate(lines, start=1):
        if line.strip():
            header = [cell.strip().strip('"') for cell in line.split("\t")]
            header_line = lineno
            break
    if not header:
        raise MultiQCParseError("general-stats table has no header row")
    columns = [_match_metric(cell) for cell in header]
    millions = {"total_sequences"} if _header_says_millions(header) else set()

    for lineno in range(header_line + 1, len(lines) + 1):
        line = lines[lineno - 1]
        if not line.strip():
            continue
        cells = [cell.strip().strip('"') for cell in line.split("\t")]
        name = cells[0] if cells else ""
        if not name:
            continue
        sample = MultiQCSample(name=name, row=lineno)
        for cell, canonical in zip(cells[1:], columns[1:], strict=False):
            if canonical is None:
                continue
            number = _num(cell)
            if number is None:
                continue
            if canonical in millions:
                number *= 1_000_000
            _set_canonical(sample, canonical, number)
        facts.samples.append(sample)


def _header_says_millions(header: list[str]) -> bool:
    joined = " ".join(header).lower()
    return "m seqs" in joined or "millions" in joined


def _set_canonical(sample: MultiQCSample, canonical: str, number: float) -> None:
    if canonical == "duplication_percent":
        sample.duplication_percent = number
    elif canonical == "gc_percent":
        sample.gc_percent = number
    elif canonical == "read_length":
        sample.read_length = int(number)
    elif canonical == "total_sequences":
        sample.total_sequences = int(number)
    elif canonical == "fails_percent":
        sample.fails_percent = number
    elif canonical == "mean_quality":
        sample.mean_quality = number
    elif canonical == "adapter_percent":
        sample.adapter_percent = number


# --------------------------------------------------------------------------
# HTML: multiqc_report.html (General Statistics table, recovered from markup)
# --------------------------------------------------------------------------
class _TableCollector(HTMLParser):
    """Every <table> in the document as plain rows of cell text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if any(cell.strip() for cell in self._row):
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None


def _parse_html(text: str, facts: MultiQCFacts) -> None:
    collector = _TableCollector()
    try:
        collector.feed(text[:2_000_000])
    except Exception as exc:
        raise MultiQCParseError(f"report HTML could not be read: {exc}") from exc
    table = _find_stats_table(collector.tables)
    if table is None:
        raise MultiQCParseError("no General Statistics table found in the report HTML")
    header, rows = table[0], table[1:]
    columns = [_match_metric(cell) for cell in header]
    for index, row in enumerate(rows, start=1):
        if not row or not row[0].strip():
            continue
        sample = MultiQCSample(name=row[0].strip())
        for cell, canonical in zip(row[1:], columns[1:], strict=False):
            if canonical is None:
                continue
            number = _num(cell)
            if number is None:
                continue
            if canonical == "total_sequences" and _header_says_millions(header):
                number *= 1_000_000
            _set_canonical(sample, canonical, number)
        sample.row = index
        facts.samples.append(sample)


def _find_stats_table(tables: list[list[list[str]]]) -> list[list[str]] | None:
    for table in tables:
        if not table:
            continue
        first = [cell.strip().lower() for cell in table[0]]
        if first and first[0] in ("sample", "sample name") and len(table) > 1:
            return table
    return None
