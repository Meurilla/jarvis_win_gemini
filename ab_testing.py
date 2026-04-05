"""
JARVIS A/B Testing — Template version selection and experiment tracking.

Randomly assigns template versions for the same task type,
tracks which version was used, and calculates success rates per version.

Improved for async event loop (aiosqlite), better error handling,
and fixed record_result to respect the template_version argument.
"""

import asyncio
import logging
import math
import random
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, List, Tuple, Any

import yaml
import aiosqlite

log = logging.getLogger("jarvis.ab_testing")

TEMPLATES_DIR = Path(__file__).parent / "templates" / "prompts"
DB_PATH = Path(__file__).parent / "jarvis_data.db"

# Minimum tasks per version before declaring a winner
MIN_TASKS_FOR_WINNER = 20
# Minimum success rate difference (as percentage points) to declare a winner
MIN_RATE_DIFFERENCE = 10.0


@dataclass
class PromptTemplate:
    """A loaded prompt template with metadata."""
    task_type: str
    version: str
    file_path: str
    description: str
    sections: List[dict] = field(default_factory=list)
    success_rate: Optional[float] = None
    raw_data: Optional[dict] = field(default=None, repr=False)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("raw_data", None)
        return d


@dataclass
class VersionStats:
    version: str
    success_rate: float
    total_tasks: int
    passed: int
    failed: int
    confidence_interval: Tuple[float, float] = (0.0, 0.0)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["confidence_interval"] = list(self.confidence_interval)
        return d


class ABTester:
    """A/B testing framework for prompt templates (async, aiosqlite)."""

    def __init__(self, db_path: Optional[str] = None, templates_dir: Optional[str] = None):
        self.db_path = db_path or str(DB_PATH)
        self.templates_dir = Path(templates_dir) if templates_dir else TEMPLATES_DIR
        self._db: Optional[aiosqlite.Connection] = None

    async def _ensure_db(self) -> aiosqlite.Connection:
        """Get or create the database connection, ensuring tables exist."""
        if self._db is None:
            self._db = await aiosqlite.connect(self.db_path)
            self._db.row_factory = aiosqlite.Row
            await self._create_tables(self._db)
        return self._db

    async def _create_tables(self, db: aiosqlite.Connection):
        """Create experiments table if not exists."""
        await db.execute("""
            CREATE TABLE IF NOT EXISTS experiments (
                id TEXT PRIMARY KEY,
                task_type TEXT NOT NULL,
                template_version TEXT NOT NULL,
                success INTEGER DEFAULT NULL,
                created_at TEXT NOT NULL,
                completed_at TEXT DEFAULT NULL
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_type ON experiments(task_type)
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_exp_version ON experiments(template_version)
        """)
        await db.commit()

    async def _discover_versions(self, task_type: str) -> List[PromptTemplate]:
        """Find all template versions for a given task type."""
        templates: List[PromptTemplate] = []
        if not self.templates_dir.exists():
            log.warning(f"Templates directory not found: {self.templates_dir}")
            return templates

        # Look for files matching the task type
        for f in sorted(self.templates_dir.glob(f"{task_type}*.yaml")):
            try:
                data = yaml.safe_load(f.read_text(encoding="utf-8"))
                if not data:
                    log.warning(f"Empty YAML file: {f}")
                    continue
                if data.get("task_type") != task_type:
                    log.debug(f"Skipping {f}: task_type mismatch (expected {task_type}, got {data.get('task_type')})")
                    continue
                templates.append(PromptTemplate(
                    task_type=data.get("task_type", task_type),
                    version=data.get("version", "v1"),
                    file_path=str(f),
                    description=data.get("description", ""),
                    sections=data.get("sections", []),
                    success_rate=data.get("success_rate"),
                    raw_data=data,
                ))
            except yaml.YAMLError as e:
                log.error(f"YAML parse error in {f}: {e}")
            except Exception as e:
                log.error(f"Failed to load template {f}: {e}", exc_info=True)

        if not templates:
            log.warning(f"No valid templates found for task type: {task_type}")
        return templates

    async def select_template(self, task_type: str) -> Tuple[PromptTemplate, str]:
        """Select a template version for the given task type.

        Returns (PromptTemplate, experiment_id).
        If multiple versions exist, randomly selects one with equal probability.
        If no templates found, returns a minimal default.
        """
        versions = await self._discover_versions(task_type)

        if not versions:
            log.warning(f"No templates found for task type: {task_type}")
            default = PromptTemplate(
                task_type=task_type,
                version="default",
                file_path="",
                description=f"Default template for {task_type}",
            )
            experiment_id = await self._create_experiment(task_type, "default")
            return default, experiment_id

        # Random selection with equal probability
        selected = random.choice(versions)
        experiment_id = await self._create_experiment(task_type, selected.version)

        log.info(
            f"Selected template {task_type} {selected.version} "
            f"(experiment {experiment_id})"
        )
        return selected, experiment_id

    async def _create_experiment(self, task_type: str, version: str) -> str:
        """Record a new experiment and return its ID."""
        experiment_id = str(uuid.uuid4())[:12]
        try:
            db = await self._ensure_db()
            await db.execute(
                "INSERT INTO experiments (id, task_type, template_version, created_at) "
                "VALUES (?, ?, ?, ?)",
                (experiment_id, task_type, version, datetime.now().isoformat()),
            )
            await db.commit()
        except Exception as e:
            log.error(f"Failed to record experiment {experiment_id}: {e}", exc_info=True)
        return experiment_id

    async def record_result(
        self, experiment_id: str, template_version: str, success: bool
    ):
        """Record the outcome of an A/B experiment.

        Args:
            experiment_id: The experiment ID from select_template().
            template_version: The template version that was used.
            success: Whether the task succeeded.
        """
        try:
            db = await self._ensure_db()
            # First, fetch the stored template_version to validate
            cursor = await db.execute(
                "SELECT template_version FROM experiments WHERE id = ?",
                (experiment_id,)
            )
            row = await cursor.fetchone()
            if not row:
                log.warning(f"Experiment {experiment_id} not found, cannot record result")
                return

            stored_version = row["template_version"]
            if stored_version != template_version:
                log.warning(
                    f"Version mismatch for experiment {experiment_id}: "
                    f"stored={stored_version}, provided={template_version}. "
                    f"Using stored version for consistency."
                )
                # Optionally, you could raise an error, but we'll just log and continue

            await db.execute(
                "UPDATE experiments SET success = ?, completed_at = ? WHERE id = ?",
                (int(success), datetime.now().isoformat(), experiment_id),
            )
            await db.commit()
            log.info(
                f"Recorded experiment {experiment_id}: "
                f"version={stored_version}, {'passed' if success else 'failed'}"
            )
        except Exception as e:
            log.error(f"Failed to record result for {experiment_id}: {e}", exc_info=True)

    async def get_version_stats(self, task_type: str) -> Dict[str, VersionStats]:
        """Get per-version success rates with confidence intervals."""
        stats: Dict[str, VersionStats] = {}
        try:
            db = await self._ensure_db()
            async with db.execute(
                "SELECT template_version, success, COUNT(*) as cnt "
                "FROM experiments WHERE task_type = ? AND success IS NOT NULL "
                "GROUP BY template_version, success",
                (task_type,),
            ) as cursor:
                rows = await cursor.fetchall()

            # Aggregate by version
            version_data: Dict[str, Dict[str, int]] = {}
            for row in rows:
                v = row["template_version"]
                if v not in version_data:
                    version_data[v] = {"passed": 0, "failed": 0}
                if row["success"]:
                    version_data[v]["passed"] += row["cnt"]
                else:
                    version_data[v]["failed"] += row["cnt"]

            for version, data in version_data.items():
                total = data["passed"] + data["failed"]
                rate = (data["passed"] / total * 100) if total > 0 else 0.0
                ci = self._wilson_interval(data["passed"], total)
                stats[version] = VersionStats(
                    version=version,
                    success_rate=rate,
                    total_tasks=total,
                    passed=data["passed"],
                    failed=data["failed"],
                    confidence_interval=ci,
                )
        except Exception as e:
            log.error(f"Failed to get version stats for {task_type}: {e}", exc_info=True)
        return stats

    async def promote_winner(self, task_type: str) -> Optional[str]:
        """Identify the winning template version if data supports it.

        Requirements:
        - At least MIN_TASKS_FOR_WINNER tasks per version
        - At least MIN_RATE_DIFFERENCE percentage-point gap
        """
        stats = await self.get_version_stats(task_type)

        # Need at least 2 versions with enough data
        qualified = {
            v: s for v, s in stats.items()
            if s.total_tasks >= MIN_TASKS_FOR_WINNER
        }

        if len(qualified) < 2:
            return None

        # Sort by success rate descending
        ranked = sorted(
            qualified.values(), key=lambda s: s.success_rate, reverse=True
        )
        best = ranked[0]
        second = ranked[1]

        if best.success_rate - second.success_rate >= MIN_RATE_DIFFERENCE:
            log.info(
                f"Winner for {task_type}: {best.version} "
                f"({best.success_rate:.1f}% vs {second.success_rate:.1f}%)"
            )
            return best.version

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _wilson_interval(
        successes: int, total: int, z: float = 1.96
    ) -> Tuple[float, float]:
        """Wilson score interval for binomial proportion (~95% confidence).

        Returns interval as percentages (0-100).
        """
        if total == 0:
            return (0.0, 0.0)

        p = successes / total
        denom = 1 + z * z / total
        centre = (p + z * z / (2 * total)) / denom
        spread = (
            z
            * math.sqrt((p * (1 - p) + z * z / (4 * total)) / total)
            / denom
        )

        lower = max(0.0, centre - spread) * 100
        upper = min(1.0, centre + spread) * 100
        return (round(lower, 2), round(upper, 2))

    async def close(self):
        """Close the database connection gracefully."""
        if self._db:
            await self._db.close()
            self._db = None
            log.debug("ABTester database connection closed")


# ----------------------------------------------------------------------
# Optional: Context manager for easier integration
# ----------------------------------------------------------------------

class ABTestingContext:
    """Async context manager for ABTester."""
    def __init__(self, db_path: Optional[str] = None, templates_dir: Optional[str] = None):
        self.tester = ABTester(db_path, templates_dir)

    async def __aenter__(self):
        await self.tester._ensure_db()  # initialize connection
        return self.tester

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.tester.close()

"""
Changelog
Version 2.0 (2026-04-05)
Breaking Changes
Async‑first: All methods are now async and must be awaited.
(Previously synchronous – required to avoid blocking the event loop.)

Database driver: Replaced sqlite3 with aiosqlite.
Requires installation: pip install aiosqlite.

Bug Fixes
Fixed record_result: Now validates the provided template_version against the stored version in the DB. Logs a warning on mismatch (instead of silently ignoring the argument).

YAML loading: Added explicit encoding="utf-8" and better error handling (distinguishes YAML parse errors from general IO errors). Logs filename on failure.

Missing directory handling: Logs a warning if templates_dir doesn’t exist.

Improvements
Connection management: Uses a single persistent connection with lazy initialisation. Added close() method and an async context manager (ABTestingContext) for clean shutdown.

Logging: More detailed and consistent logging (info, warning, error) with exc_info=True where appropriate.

SQL indexing: Index creation now uses IF NOT EXISTS – safer for repeated runs.

Type hints: Fully annotated with modern Python 3.9+ types (List, Dict, Tuple, Optional).

Removed / Deprecated
Removed synchronous db attribute and the old sqlite3 connection.

Removed the unused raw_data field from to_dict() output (already excluded).

Dependencies
Added aiosqlite to requirements.

pyyaml remains required.
"""