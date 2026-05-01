import json
import os
import urllib.error
import urllib.request

from base_agent import BaseAgent


class ReportAgent(BaseAgent):
    def _mock_narrative(self, payload) -> str:
        sections = []
        for key in ["qc", "align", "bwa", "count", "de", "insight", "annotation", "coverage"]:
            if key in payload:
                sections.append(key)
        section_str = ", ".join(sections) if sections else "pipeline"
        return (
            f"Automated report summary generated from {section_str}. "
            "The run completed through the registered agents and produced artifacts for downstream review."
        )

    def _openrouter_narrative(self, payload) -> tuple[str, str]:
        api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
        model = os.environ.get("OPENROUTER_MODEL", "deepseek/deepseek-chat-v3-0324")
        if not api_key:
            return self._mock_narrative(payload), "mock"

        prompt = (
            "You are writing a concise genomics pipeline narrative for an FYP report. "
            "Summarize QC, preprocessing, alignment, quantification/variant calling, and key outputs. "
            "Keep it under 180 words and clearly mention if coverage gate passed.\n\n"
            f"Agent outputs JSON:\n{json.dumps(payload, default=str)[:14000]}"
        )

        request_body = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You write precise NGS pipeline summaries."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0.2,
        }

        req = urllib.request.Request(
            "https://openrouter.ai/api/v1/chat/completions",
            data=json.dumps(request_body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=40) as response:
                raw = response.read().decode("utf-8", errors="ignore")
            parsed = json.loads(raw)
            content = (
                parsed.get("choices", [{}])[0]
                .get("message", {})
                .get("content", "")
                .strip()
            )
            if not content:
                return self._mock_narrative(payload), "mock"
            return content, model
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError):
            return self._mock_narrative(payload), "mock"

    def execute(self, inputs, routing_ctx):
        payload = inputs.get("payload", {})
        narrative, source = self._openrouter_narrative(payload)

        return {
            "agent": "report_agent",
            "status": "ok",
            "payload": {
                "narrative": narrative,
                "source": source,
            },
            "reasoning": f"Narrative generated using {source}",
        }


if __name__ == "__main__":
    ReportAgent().run()
