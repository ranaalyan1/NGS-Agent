"""Unit tests for system doctor and diagnostics."""

from rich.console import Console

from ngs_agent.doctor import DiagnosticCheck, print_diagnostics, run_diagnostics


class TestDoctorDiagnostics:
    def test_run_diagnostics(self):
        checks = run_diagnostics()
        assert len(checks) > 0
        categories = {c.category for c in checks}
        assert "Runtime" in categories
        assert "Bioinformatics" in categories
        assert "LLM Config" in categories

    def test_print_diagnostics(self):
        checks = [
            DiagnosticCheck("Runtime", "Python", "OK", "3.11"),
            DiagnosticCheck("Bioinformatics", "FastQC", "WARN", "Not in PATH", "Install FastQC"),
        ]
        con = Console(record=True)
        print_diagnostics(checks, console=con)
        output = con.export_text()
        assert "NGS-Agent System Doctor" in output
        assert "FastQC" in output
