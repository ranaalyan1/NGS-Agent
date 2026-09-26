"""NGS-Agent core.

Everything that decides anything lives here. Doors (``doors/``) import from
this package and render; they never decide.
"""

from .version import RULESET_VERSION, TOOL_VERSION

__all__ = ["RULESET_VERSION", "TOOL_VERSION"]
