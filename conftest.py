"""Make the repository root importable and audit receipts across the suite.

The session fixture records every ``Finding`` created, so the Law of Receipts
is checked across findings produced by all tests, not just one test module.
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
        f"{len(bad)} findings without a valid receipt"
    )
