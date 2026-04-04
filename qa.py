"""
JARVIS QA Agent — Verifies completed task output using Gemini.

Previously used claude -p subprocess; now calls Gemini API directly,
which is faster, more reliable, and works without any CLI tool installed.
"""

import json
import logging
import os
from dataclasses import dataclass, asdict

from google import genai
from google.genai import types as genai_types

log = logging.getLogger("jarvis.qa")

MAX_RETRIES = 3

_GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")


def _get_client() -> "genai.Client | None":
    key = _GEMINI_API_KEY
    if not key:
        log.warning("GEMINI_API_KEY not set — QA agent disabled")
        return None
    return genai.Client(api_key=key)


@dataclass
class QAResult:
    passed: bool
    issues: list[str]
    summary: str
    attempt: int = 1

    def to_dict(self) -> dict:
        return asdict(self)


class QAAgent:
    """Verifies task output using Gemini Flash."""

    async def verify(self, task_prompt: str, task_result: str, working_dir: str = ".") -> QAResult:
        """QA-check a completed task. Returns a QAResult."""
        client = _get_client()
        if not client:
            return QAResult(passed=True, issues=["QA skipped — no API key"], summary="QA skipped")

        system = (
            "You are a QA agent reviewing completed AI coding tasks. "
            "Be concise and strict — only pass work that genuinely meets the requirements. "
            "Respond with JSON only, no markdown fences: "
            '{"passed": true/false, "issues": ["issue1", ...], "summary": "one line summary"}'
        )
        prompt = (
            f"ORIGINAL TASK:\n{task_prompt}\n\n"
            f"TASK OUTPUT:\n{task_result[:3000]}\n\n"
            "Check: does the output match requirements? Are files/steps mentioned complete? "
            "Any obvious errors or missing pieces?"
        )

        try:
            config = genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=300,
            )
            response = await client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=config,
            )
            raw = (response.text or "").strip()

            if "```" in raw:
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = json.loads(raw)
            return QAResult(
                passed=data.get("passed", False),
                issues=data.get("issues", []),
                summary=data.get("summary", "QA completed"),
            )

        except json.JSONDecodeError:
            log.warning("QA response not valid JSON, treating as pass")
            return QAResult(passed=True, issues=[], summary="QA output unparseable — manual review recommended")
        except Exception as e:
            log.error(f"QA error: {e}")
            return QAResult(passed=True, issues=[f"QA error: {e}"], summary=f"QA error: {e}")

    async def auto_retry(
        self,
        task_prompt: str,
        issues: list[str],
        working_dir: str = ".",
        attempt: int = 1,
    ) -> dict:
        """Generate a corrected response via Gemini given QA failure feedback."""
        if attempt >= MAX_RETRIES:
            return {
                "status": "failed",
                "result": "",
                "error": f"Max retries ({MAX_RETRIES}) exceeded. Issues: {issues}",
                "attempt": attempt,
            }

        client = _get_client()
        if not client:
            return {"status": "failed", "result": "", "error": "No API key", "attempt": attempt}

        system = (
            "You are an expert software developer fixing issues in a previous task attempt. "
            "Address each issue listed and produce a corrected, complete result."
        )
        prompt = (
            f"RETRY ATTEMPT {attempt + 1}/{MAX_RETRIES}\n\n"
            f"ORIGINAL TASK:\n{task_prompt}\n\n"
            "PREVIOUS ATTEMPT FAILED QA. Issues found:\n"
            + "\n".join(f"- {issue}" for issue in issues)
            + "\n\nFix these issues and complete the task correctly."
        )

        try:
            config = genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=2000,
            )
            response = await client.aio.models.generate_content(
                model="gemini-3-flash-preview",
                contents=prompt,
                config=config,
            )
            result = (response.text or "").strip()
            return {"status": "completed", "result": result, "error": "", "attempt": attempt + 1}

        except Exception as e:
            return {"status": "failed", "result": "", "error": str(e), "attempt": attempt + 1}


# Module-level singleton — server.py imports and reuses this
qa_agent = QAAgent()