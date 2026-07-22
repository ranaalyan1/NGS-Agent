import json
import os
import re
from typing import Any, Dict


class BaseAgent:
    def run(self) -> None:
        inputs = json.loads(os.environ["AGENT_INPUTS"])
        routing_ctx = json.loads(os.environ["ROUTING_CONTEXT"])
        output = self.execute(inputs, routing_ctx)
        print(json.dumps(output), flush=True)

    def execute(self, inputs: Dict[str, Any], routing_ctx: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    @staticmethod
    def _extract_json(text: str) -> dict | None:
        """Pull the first JSON object out of a string.
        
        Handles:
        - Clean JSON objects
        - JSON embedded in text
        - JSON in markdown code blocks
        
        Returns None if no valid dict JSON is found.
        """
        if not text:
            return None
        
        # Try the whole string first, then regex-extract
        for candidate in [text.strip()] + [
            m.group(0) for m in [re.search(r"\{[\s\S]*\}", text)] if m
        ]:
            try:
                obj = json.loads(candidate)
                if isinstance(obj, dict):
                    return obj
            except Exception:
                continue
        
        return None
