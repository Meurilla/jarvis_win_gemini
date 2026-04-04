"""
JARVIS Conversation Intelligence — Active session tracking for every exchange.

Always-on across the entire WebSocket session. Tracks decisions, exchanges,
and the evolving plan. Feeds structured context into every Gemini call so
JARVIS remembers what was agreed upon regardless of message history truncation.

Distinct from planner.py which is only active during task planning flows.
Planner writes its completed decisions INTO this session via log_plan().
"""

import logging
import time
import json
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

log = logging.getLogger("jarvis.conversation")

# Max exchanges to keep in the active window
CONTEXT_WINDOW_MAX = 20

# How many decisions to surface in the system prompt
DECISIONS_IN_PROMPT = 10

# Session idle timeout in seconds (30 minutes)
SESSION_TIMEOUT_SECONDS = 1800


# ---------------------------------------------------------------------------
# Data Models
# ---------------------------------------------------------------------------

@dataclass
class Decision:
    """A single decision or agreement made during the session."""
    key: str
    value: str
    source: str = "conversation"  # "conversation" | "planner" | "user"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PlanSummary:
    """Structured summary of the current or most recent plan."""
    description: str = ""
    task_type: str = ""
    project: str = ""
    working_dir: str = ""
    tech_stack: list[str] = field(default_factory=list)
    features: list[str] = field(default_factory=list)
    constraints: list[str] = field(default_factory=list)
    status: str = "none"  # "none" | "planning" | "building" | "complete"

    def to_dict(self) -> dict:
        return asdict(self)

    def to_text(self) -> str:
        """Format plan as readable text for context injection."""
        if not self.description:
            return "No active plan."

        lines = [f"Task: {self.description}"]
        if self.task_type:
            lines.append(f"Type: {self.task_type}")
        if self.project:
            lines.append(f"Project: {self.project}")
        if self.working_dir:
            lines.append(f"Directory: {self.working_dir}")
        if self.tech_stack:
            lines.append(f"Tech stack: {', '.join(self.tech_stack)}")
        if self.features:
            lines.append("Features:")
            for f in self.features:
                lines.append(f"  - {f}")
        if self.constraints:
            lines.append("Constraints:")
            for c in self.constraints:
                lines.append(f"  - {c}")
        lines.append(f"Status: {self.status}")
        return "\n".join(lines)

    @property
    def is_empty(self) -> bool:
        return not self.description


# ---------------------------------------------------------------------------
# Conversation Session
# ---------------------------------------------------------------------------

class ConversationSession:
    """
    Tracks the full state of one WebSocket session.

    Instantiated once per WebSocket connection in voice_handler().
    Receives every exchange via add_exchange(), structured decisions
    via log_plan() from the planner, and surfaces context via get_context()
    which is injected into every Gemini system prompt.
    """

    def __init__(self):
        self.decisions: list[Decision] = []
        self.exchanges: list[dict] = []
        self.current_plan = PlanSummary()
        self._created_at = datetime.now()
        self._last_activity = datetime.now()
        self._closed = False
        self._exchange_count = 0

    # -- Properties -----------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """True if session is open and not timed out."""
        if self._closed:
            return False
        elapsed = (datetime.now() - self._last_activity).total_seconds()
        if elapsed > SESSION_TIMEOUT_SECONDS:
            log.info("Conversation session timed out")
            self._closed = True
            return False
        return True

    @property
    def exchange_count(self) -> int:
        return self._exchange_count

    @property
    def decision_count(self) -> int:
        return len(self.decisions)

    # -- Core Interface -------------------------------------------------------

    def add_exchange(self, role: str, content: str):
        """
        Record one side of an exchange. Call for both user and assistant turns.

        Args:
            role: "user" or "assistant"
            content: The message text
        """
        self.exchanges.append({
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
        })

        # Cap the window — keep most recent
        if len(self.exchanges) > CONTEXT_WINDOW_MAX:
            self.exchanges = self.exchanges[-CONTEXT_WINDOW_MAX:]

        if role == "user":
            self._exchange_count += 1

        self._last_activity = datetime.now()

    def get_context(self) -> str:
        """
        Return structured context string for injection into Gemini system prompt.

        Kept concise deliberately — this goes into every single call.
        Surfaces decisions and current plan; omits raw exchange log
        (that's already in the conversation history parameter).
        """
        parts = []

        # Active plan
        if not self.current_plan.is_empty:
            parts.append(f"CURRENT PLAN:\n{self.current_plan.to_text()}")

        # Recent decisions — most important, most recent first
        if self.decisions:
            recent = self.decisions[-DECISIONS_IN_PROMPT:]
            lines = ["SESSION DECISIONS:"]
            for d in reversed(recent):
                lines.append(f"  [{d.source}] {d.key}: {d.value}")
            parts.append("\n".join(lines))

        # Session stats — brief
        uptime = int((datetime.now() - self._created_at).total_seconds() / 60)
        parts.append(
            f"SESSION: {self._exchange_count} exchanges, "
            f"{len(self.decisions)} decisions, "
            f"{uptime}m elapsed"
        )

        return "\n\n".join(parts) if parts else ""

    def log_decision(self, key: str, value: str, source: str = "conversation"):
        """
        Record a single decision directly.

        Used internally and by external callers for one-off decisions
        that don't come through the planner.
        """
        # Avoid duplicating the same key/value
        for existing in self.decisions:
            if existing.key == key and existing.value == value:
                return

        self.decisions.append(Decision(key=key, value=value, source=source))
        self._last_activity = datetime.now()
        log.info(f"Decision logged [{source}]: {key} = {value[:60]}")

    def log_plan(self, plan) -> None:
        """
        Receive a completed Plan from TaskPlanner and store its decisions.

        Called at integration point 5 — after planner confirms and before reset.

        Args:
            plan: planner.Plan dataclass instance
        """
        if not plan:
            return

        # Update the living plan summary
        self.current_plan.description = plan.original_request
        self.current_plan.task_type = plan.task_type
        self.current_plan.status = "building"

        if plan.project:
            self.current_plan.project = plan.project
            self.log_decision("project", plan.project, source="planner")

        if plan.project_path:
            self.current_plan.working_dir = plan.project_path
            self.log_decision("working_dir", plan.project_path, source="planner")

        # Log all collected answers as decisions
        for key, value in plan.answers.items():
            if value and str(value).strip():
                # Parse tech stack into list
                if key == "tech_stack":
                    self.current_plan.tech_stack = [
                        s.strip() for s in str(value).split(",")
                    ]
                elif key == "details":
                    # Features/details go into the features list
                    self.current_plan.features = [
                        f.strip() for f in str(value).split(",")
                        if f.strip()
                    ]
                self.log_decision(key, str(value), source="planner")

        self.log_decision(
            "task_launched",
            plan.original_request[:100],
            source="planner",
        )

        self._last_activity = datetime.now()
        log.info(
            f"Plan logged from planner: {plan.task_type} — "
            f"{plan.original_request[:60]}"
        )

    async def modify_plan(self, user_text: str, gemini_client) -> str:
        """
        Parse a natural language plan modification via Gemini Flash
        and update the current plan accordingly.

        Returns a JARVIS-voiced confirmation of what changed.

        Args:
            user_text: The user's modification request
            gemini_client: google.genai.Client instance
        """
        if self.current_plan.is_empty:
            return "There's no active plan to modify, sir."

        system = (
            "You are parsing a plan modification request for JARVIS. "
            "Given the current plan and the user's modification, extract what changed.\n\n"
            f"CURRENT PLAN:\n{self.current_plan.to_text()}\n\n"
            "Respond with JSON only, no markdown:\n"
            '{"field": "tech_stack|features|constraints|project|description|other", '
            '"action": "add|remove|replace|update", '
            '"value": "the new value or item", '
            '"old_value": "what it replaces if applicable or empty string"}'
        )

        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=150,
            )
            response = await gemini_client.aio.models.generate_content(
                model="gemini-2.0-flash-preview",
                contents=user_text,
                config=config,
            )
            raw = response.text.strip()

            # Strip markdown fences if present
            if raw.startswith("```"):
                raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

            data = json.loads(raw)

            field_name = data.get("field", "other")
            action = data.get("action", "update")
            value = data.get("value", "")
            old_value = data.get("old_value", "")

            # Apply the modification
            confirmation = self._apply_modification(field_name, action, value, old_value)

            # Log the decision
            self.log_decision(
                f"modification_{field_name}",
                f"{action}: {value}",
                source="conversation",
            )

            return confirmation

        except json.JSONDecodeError:
            log.warning("modify_plan: Gemini response not valid JSON")
            # Still log it happened
            self.log_decision("modification", user_text[:80], source="conversation")
            return "Noted, sir. I've updated the plan accordingly."
        except Exception as e:
            log.error(f"modify_plan error: {e}")
            return "Had a spot of trouble parsing that modification, sir. Could you rephrase?"

    def _apply_modification(
        self, field: str, action: str, value: str, old_value: str
    ) -> str:
        """Apply a parsed modification to current_plan and return confirmation."""
        plan = self.current_plan

        if field == "tech_stack":
            if action == "replace" and old_value:
                plan.tech_stack = [
                    value if t.lower() == old_value.lower() else t
                    for t in plan.tech_stack
                ]
                return f"Switching from {old_value} to {value}, sir."
            elif action == "add":
                if value not in plan.tech_stack:
                    plan.tech_stack.append(value)
                return f"Adding {value} to the tech stack, sir."
            elif action == "remove":
                plan.tech_stack = [t for t in plan.tech_stack if t.lower() != value.lower()]
                return f"Removing {value} from the tech stack, sir."
            else:
                plan.tech_stack = [value]
                return f"Tech stack updated to {value}, sir."

        elif field == "features":
            if action == "add":
                plan.features.append(value)
                return f"Adding {value} to the feature list, sir."
            elif action == "remove":
                plan.features = [f for f in plan.features if value.lower() not in f.lower()]
                return f"Removing {value} from the features, sir."
            else:
                plan.features.append(value)
                return f"Feature updated, sir."

        elif field == "constraints":
            if action == "add":
                plan.constraints.append(value)
                return f"Noted the constraint: {value}, sir."
            elif action == "remove":
                plan.constraints = [c for c in plan.constraints if value.lower() not in c.lower()]
                return f"Constraint removed, sir."
            else:
                plan.constraints.append(value)
                return f"Constraint noted, sir."

        elif field == "project":
            plan.project = value
            return f"Project updated to {value}, sir."

        elif field == "description":
            plan.description = value
            return f"Task description updated, sir."

        else:
            # Generic — just log it
            return f"Understood, sir. I've noted that change."

    async def query(self, user_text: str, gemini_client) -> str:
        """
        Answer a question about session history or decisions via Gemini Flash,
        formatted as a JARVIS voice response.

        Args:
            user_text: The user's question
            gemini_client: google.genai.Client instance
        """
        context = self.get_context()
        if not context:
            return "Nothing on record yet, sir. We've only just begun."

        system = (
            "You are JARVIS answering a question about the current session. "
            "Answer using ONLY the session context provided — do not invent details. "
            "If the answer isn't in the context, say so plainly. "
            "British butler tone, 1-2 sentences, no markdown."
        )

        prompt = (
            f"SESSION CONTEXT:\n{context}\n\n"
            f"USER QUESTION: {user_text}"
        )

        try:
            from google.genai import types as genai_types

            config = genai_types.GenerateContentConfig(
                system_instruction=system,
                max_output_tokens=200,
            )
            response = await gemini_client.aio.models.generate_content(
                model="gemini-2.0-flash-preview",
                contents=prompt,
                config=config,
            )
            return response.text.strip()

        except Exception as e:
            log.error(f"Session query error: {e}")
            return "I'm having trouble accessing the session records, sir."

    def mark_plan_complete(self):
        """Mark the current plan as complete — called when dispatch finishes."""
        if not self.current_plan.is_empty:
            self.current_plan.status = "complete"
            self.log_decision(
                "task_completed",
                self.current_plan.description[:80],
                source="conversation",
            )

    def close(self, reason: str = "disconnected"):
        """Close the session cleanly."""
        self._closed = True
        log.info(
            f"Conversation session closed ({reason}): "
            f"{self._exchange_count} exchanges, "
            f"{len(self.decisions)} decisions, "
            f"{int((datetime.now() - self._created_at).total_seconds() / 60)}m"
        )