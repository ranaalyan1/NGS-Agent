"""Rules: pure functions from facts to findings.

Every rule is ``facts -> Finding | None``. Rules never read files, never call
the network, and never disagree with the numbers in front of them.
"""
