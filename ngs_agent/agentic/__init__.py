"""Agentic execution core for ngs (planner -> executor -> verifier -> reporter).

This subpackage requires the optional ``agentic`` extra:

    pip install ngs-agent[agentic]
"""
from __future__ import annotations

__all__ = ["launch_cli"]


def launch_cli() -> None:
    """Console-script entry point with a friendly error when extras are missing."""
    try:
        from ngs_agent.agentic.cli.app import main
    except ImportError as exc:
        import sys

        print(
            f"The agentic CLI requires optional dependencies that are not installed ({exc.name}).\n"
            "Install them with:\n\n"
            "    pip install ngs-agent[agentic]\n",
            file=sys.stderr,
        )
        raise SystemExit(1) from exc
    main()
