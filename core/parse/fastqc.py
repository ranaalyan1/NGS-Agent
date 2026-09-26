"""FastQC parser: a FastQC zip in, facts out.

FastQC ships its results as a zip containing ``<sample>_fastqc/fastqc_data.txt``
plus a ``summary.txt``. We read the module tables verbatim and keep the line
number of every extracted number, so a rule can cite ``fastqc_data.txt:line=87``
instead of asking anyone to trust it.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from ..models import FastQCFacts, ModuleStatus, Point
from ..util import sha256_file

DATA_MEMBER_SUFFIX = "fastqc_data.txt"
SUMMARY_MEMBER_SUFFIX = "summary.txt"

# Module names as FastQC writes them.
MOD_BASIC = "Basic Statistics"
MOD_QUALITY = "Per base sequence quality"
MOD_ADAPTER = "Adapter Content"
MOD_DUP = "Sequence Duplication Levels"
MOD_GC = "Per sequence GC content"
MOD_N = "Per base N content"
MOD_LEN = "Sequence Length Distribution"


class FastQCParseError(Exception):
    """The zip was a FastQC zip by name but not by content."""


def _bin_start(label: str) -> float:
    """'10-19' -> 10.0 ; '7' -> 7.0 ; '150' -> 150.0."""
    head = label.split("-")[0].strip()
    try:
        return float(head)
    except ValueError:
        return float("nan")


def _num(text: str, default: float | None = None) -> float | None:
    try:
        return float(text)
    except (TypeError, ValueError):
        return default


def _find_member(names: list[str], suffix: str) -> str | None:
    lowered = suffix.lower()
    for name in names:
        if name.lower().endswith(lowered):
            return name
    return None


def parse_fastqc(zip_path: str | Path) -> FastQCFacts:
    """Extract module statuses and metrics from a FastQC zip.

    Raises ``FastQCParseError`` if the archive has no ``fastqc_data.txt``; the
    caller should have sniffed the file first.
    """
    p = Path(zip_path)
    facts = FastQCFacts(source_path=str(p), source_sha256=sha256_file(p))

    try:
        with zipfile.ZipFile(p) as zf:
            names = zf.namelist()
            data_name = _find_member(names, DATA_MEMBER_SUFFIX)
            if data_name is None:
                raise FastQCParseError(f"no {DATA_MEMBER_SUFFIX} inside {p}")
            summary_name = _find_member(names, SUMMARY_MEMBER_SUFFIX)
            text = zf.read(data_name).decode("utf-8", errors="replace")
            summary = ""
            if summary_name:
                summary = zf.read(summary_name).decode("utf-8", errors="replace")
    except zipfile.BadZipFile as exc:
        raise FastQCParseError(f"{p} is not a readable zip: {exc}") from exc

    facts.data_member = data_name
    _parse_data_text(text, facts)
    if summary:
        _apply_summary(summary, facts)
    return facts


def _apply_summary(summary: str, facts: FastQCFacts) -> None:
    """summary.txt is FastQC's own verdict per module; it wins over the header.

    Only overriding, never inventing: a module absent from summary.txt keeps the
    status written in fastqc_data.txt.
    """
    overrides: dict[str, str] = {}
    for line in summary.splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and parts[0].strip() in ("PASS", "WARN", "FAIL"):
            overrides[parts[1].strip()] = parts[0].strip().lower()
    for module in facts.modules:
        if module.name in overrides:
            module.status = overrides[module.name]


def _parse_data_text(text: str, facts: FastQCFacts) -> None:
    modules: list[ModuleStatus] = []
    tables: dict[str, list[tuple[int, str]]] = {}
    headers: dict[str, tuple[int, str]] = {}
    current: str | None = None

    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.startswith("##FastQC"):
            parts = raw.split("\t")
            if len(parts) > 1:
                facts.fastqc_version = parts[1].strip()
            continue
        if raw.startswith(">>END_MODULE"):
            current = None
            continue
        if raw.startswith(">>") and not raw.startswith(">>END"):
            parts = raw[2:].split("\t")
            name = parts[0].strip()
            status = parts[1].strip().lower() if len(parts) > 1 else ""
            modules.append(ModuleStatus(name=name, status=status))
            tables[name] = []
            current = name
            continue
        if current is None:
            continue
        if raw.startswith("#"):
            headers.setdefault(current, (lineno, raw))
        elif raw.strip():
            tables[current].append((lineno, raw))

    facts.modules = modules
    _read_basic_stats(tables.get(MOD_BASIC, []), facts)
    facts.per_base_quality = _curve(tables.get(MOD_QUALITY, []), value_col=1)
    facts.adapter_content = _curve(tables.get(MOD_ADAPTER, []), value_col=None, combine="max")
    facts.gc_curve = _curve(tables.get(MOD_GC, []), value_col=1)
    facts.n_content = _curve(tables.get(MOD_N, []), value_col=1)
    _read_duplication(headers.get(MOD_DUP), tables.get(MOD_DUP, []), facts)
    _read_length_distribution(tables.get(MOD_LEN, []), facts)


def _read_basic_stats(rows: list[tuple[int, str]], facts: FastQCFacts) -> None:
    for _, raw in rows:
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        key, value = parts[0].strip(), parts[1].strip()
        if key == "Total Sequences":
            parsed = _num(value)
            if parsed is not None:
                facts.total_sequences = int(parsed)
        elif key == "%GC":
            facts.gc_percent = _num(value)
        elif key == "Sequence length":
            if "-" in value:
                lo, _, hi = value.partition("-")
                lo_n, hi_n = _num(lo), _num(hi)
                if lo_n is not None and hi_n is not None:
                    facts.read_length_range = (int(lo_n), int(hi_n))
                    facts.read_length = None
            else:
                parsed = _num(value)
                if parsed is not None:
                    facts.read_length = int(parsed)


def _read_duplication(
    header: tuple[int, str] | None,
    rows: list[tuple[int, str]],
    facts: FastQCFacts,
) -> None:
    """FastQC reports the share of the library that survives deduplication.

    We store the complement (duplication %) because that is the number people
    quote, and we say which line it came from.
    """
    if header is not None:
        lineno, raw = header
        parts = raw.split("\t")
        if len(parts) >= 2 and "Deduplicated Percentage" in parts[0]:
            dedup = _num(parts[1])
            if dedup is not None:
                facts.duplication_percent = round(100.0 - dedup, 4)
                facts.duplication_line = lineno
                return
    # Fallback: sum the "Percentage of total" column (column 3) of the
    # duplication-level rows above 1x.
    total = 0.0
    seen = False
    for _, raw in rows:
        parts = raw.split("\t")
        if len(parts) >= 3 and parts[0].strip() not in ("1", "1x"):
            value = _num(parts[2])
            if value is not None:
                total += value
                seen = True
    if seen:
        facts.duplication_percent = round(total, 4)


def _read_length_distribution(rows: list[tuple[int, str]], facts: FastQCFacts) -> None:
    """Only used when Basic Statistics did not state a length."""
    if facts.read_length is not None or facts.read_length_range is not None:
        return
    lows: list[float] = []
    highs: list[float] = []
    for _, raw in rows:
        label = raw.split("\t")[0].strip()
        if "-" in label:
            lo, _, hi = label.partition("-")
            lo_n, hi_n = _num(lo), _num(hi)
            if lo_n is not None and hi_n is not None:
                lows.append(lo_n)
                highs.append(hi_n)
        else:
            single = _num(label)
            if single is not None:
                lows.append(single)
                highs.append(single)
    if lows:
        facts.read_length_range = (int(min(lows)), int(max(highs)))


def _curve(
    rows: list[tuple[int, str]],
    *,
    value_col: int | None,
    combine: str = "first",
) -> list[Point]:
    """Turn a module table into a curve, keeping the source line of each point."""
    points: list[Point] = []
    for lineno, raw in rows:
        parts = raw.split("\t")
        if len(parts) < 2:
            continue
        label = parts[0].strip()
        x = _bin_start(label)
        if x != x:  # NaN
            continue
        if value_col is not None:
            if len(parts) <= value_col:
                continue
            y = _num(parts[value_col])
        else:
            values = [v for v in (_num(c) for c in parts[1:]) if v is not None]
            if not values:
                continue
            y = max(values) if combine == "max" else values[0]
        if y is None or y != y or y in (float("inf"), float("-inf")):
            # Missing / NaN / infinite cells are skipped, never averaged away.
            continue
        points.append(Point(x=x, y=y, label=label, line=lineno))
    return points
