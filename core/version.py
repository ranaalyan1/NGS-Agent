"""Single source of truth for versions stamped onto every receipt.

A receipt is worthless if you cannot tell which code produced it, so both
numbers are defined here and imported everywhere else. Never inline them.
"""

TOOL_VERSION = "1.0.0"

#: Version of the rule set (QC rules, audit rules, log signatures).
#: Bump this whenever a rule's thresholds or wording change, because it is
#: printed on every report and stored on every receipt.
RULESET_VERSION = "2026-09"
