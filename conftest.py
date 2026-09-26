"""Make the repository root importable in every test run, and watch receipts.

The new product lives in top-level packages (``core``, ``doors``) while the
legacy tests import ``ngs_agent``; both need the repo root on sys.path.

The session fixture here also records every ``Finding`` the suite creates, so
``tests/test_receipts_audit.py`` can check the Law of Receipts against findings
produced anywhere, not just the ones a single test happened to build.
"""

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

#: Every Finding constructed during this test session.
FINDINGS_SEEN: list = []


@pytest.fixture(scope="session", autouse=True)
def finding_registry():
    """Record every Finding the session creates, then hand the list back."""
    from core.models import Finding

    original_init = Finding.__init__

    def patched(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        FINDINGS_SEEN.append(self)

    Finding.__init__ = patched
    FINDINGS_SEEN.clear()
    yield FINDINGS_SEEN
    Finding.__init__ = original_init


def pytest_sessionfinish(session, exitstatus):  # noqa: D103 - pytest hook
    bad = [f for f in FINDINGS_SEEN if not f.has_valid_receipts()]
    print(
        f"\nreceipts audit: {len(FINDINGS_SEEN)} findings created this session, "
        f"{len(bad)} without a valid receipt"
    )
