import json
import os
from typing import Any


class BaseAgent:
    def run(self) -> None:
        inputs = json.loads(os.environ["AGENT_INPUTS"])
        routing_ctx = json.loads(os.environ["ROUTING_CONTEXT"])
        output = self.execute(inputs, routing_ctx)
        print(json.dumps(output), flush=True)

    def execute(self, inputs: dict[str, Any], routing_ctx: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError
