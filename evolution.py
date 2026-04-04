"""
JARVIS Template Evolution — Analyzes failures and generates improved template versions.

Looks at success/failure data from both task_log and experiments tables,
identifies failure patterns in error output (not prompt text), and creates
new template versions incorporating targeted improvements.

Windows-compatible:
- All file writes use explicit utf-8 encoding
- Uses the shared thread-local DB pool from dispatch_registry
- TEMPLATES_DIR existence is guarded before globbing
- New version filenames follow the ab_testing discovery pattern
"""

import logging
import sqlite3
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import yaml

log = logging.getLogger("jarvis.evolution")

TEMPLATES_DIR = Path(__file__).parent / "templates" / "prompts"

# Minimum failures before evolution is attempted
DEFAULT_MIN_FAILURES = 5

# ---------------------------------------------------------------------------
# Failure pattern definitions
#
# Each pattern matches against task_log.prompt AND any stored error/result
# text. The 'section' and 'fix' fields describe what to add to the template.
# ---------------------------------------------------------------------------

FAILURE_PATTERNS: dict[str, dict] = {
    "import": {
        "keywords": [
            "import error", "importerror", "modulenotfounderror", "no module named",
        ],
        "section": "acceptance_criteria",
        "fix": (
            "- [ ] All imports resolve without errors\n"
            "- [ ] Required packages added to requirements/package files"
        ),
    },
    "file_missing": {
        "keywords": [
            "file not found", "filenotfounderror", "no such file", "missing file",
            "cannot find", "enoent",
        ],
        "section": "acceptance_criteria",
        "fix": (
            "- [ ] All referenced files exist at expected paths\n"
            "- [ ] File creation verified before referencing"
        ),
    },
    "syntax": {
        "keywords": [
            "syntax error", "syntaxerror", "unexpected token", "parsing error",
            "invalid syntax",
        ],
        "section": "acceptance_criteria",
        "fix": (
            "- [ ] Code parses without syntax errors\n"
            "- [ ] Linter passes on all modified files"
        ),
    },
    "wrong_tech": {
        "keywords": [
            "wrong framework", "wrong library", "tech stack mismatch", "incompatible",
            "not what was asked",
        ],
        "section": "requirements",
        "fix": (
            "- Tech stack must be explicitly confirmed before starting\n"
            "- Do not substitute libraries without noting the change"
        ),
    },
    "incomplete": {
        "keywords": [
            "incomplete", "missing section", "not implemented", "todo", "placeholder",
            "not finished", "partially done",
        ],
        "section": "acceptance_criteria",
        "fix": (
            "- [ ] All sections listed in requirements are fully implemented\n"
            "- [ ] No TODO or placeholder content remains in deliverables"
        ),
    },
    "test_failure": {
        "keywords": [
            "test failed", "assertion error", "assertionerror", "test failure",
            "tests did not pass",
        ],
        "section": "acceptance_criteria",
        "fix": (
            "- [ ] All existing tests pass after changes\n"
            "- [ ] New tests added for new functionality"
        ),
    },
    "runtime_error": {
        "keywords": [
            "runtimeerror", "runtime error", "traceback", "exception", "crashed",
            "unhandled exception",
        ],
        "section": "acceptance_criteria",
        "fix": (
            "- [ ] Application runs without unhandled exceptions\n"
            "- [ ] Error handling covers expected failure modes"
        ),
    },
}


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FailureAnalysis:
    task_type: str
    total_failures: int
    common_issues: list[str]
    failure_patterns: list[str]
    suggested_improvements: list[str]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Improvement:
    section_name: str
    current_content: str
    suggested_change: str
    rationale: str

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# DB helper — reuses the shared pool from dispatch_registry
# ---------------------------------------------------------------------------

def _get_db():
    """
    Reuse the thread-local connection pool from dispatch_registry.

    If dispatch_registry hasn't been imported yet (e.g. in tests),
    falls back to opening a direct connection to the same DB file.
    """
    try:
        from dispatch_registry import _get_db as _pool_get_db
        return _pool_get_db()
    except ImportError:
        from pathlib import Path as _Path
        import threading as _threading

        _db_path = _Path(__file__).parent / "data" / "jarvis.db"
        _local = _threading.local()
        if not hasattr(_local, "conn") or _local.conn is None:
            _db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(_db_path), check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            _local.conn = conn
        return _local.conn


# ---------------------------------------------------------------------------
# Failure text collection
# ---------------------------------------------------------------------------

def _collect_failure_texts(task_type: str) -> tuple[list[str], int]:
    """
    Collect text to scan for failure patterns and count total failures.

    Scans task_log.prompt for the task type, and experiments for version
    failures. Returns (texts_to_scan, total_failure_count).

    Note: task_log stores prompts, not error output. We scan prompts here
    as a secondary signal — the primary signal is the experiments table
    which tracks per-version success/failure counts.
    """
    conn = _get_db()
    texts: list[str] = []
    total = 0

    # task_log failures — collect prompt text as weak signal
    try:
        rows = conn.execute(
            "SELECT prompt FROM task_log WHERE task_type = ? AND success = 0",
            (task_type,),
        ).fetchall()
        total += len(rows)
        texts.extend(row["prompt"].lower() for row in rows)
    except Exception as e:
        log.warning(f"Failed to query task_log for {task_type}: {e}")

    # experiments failures — count only, no text to scan
    # (experiments table stores version and success flag, not error text)
    try:
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM experiments WHERE task_type = ? AND success = 0",
            (task_type,),
        ).fetchone()
        if row:
            # Avoid double-counting: only add experiments not already in task_log
            # We can't perfectly correlate them, so we take the max of the two
            exp_failures = row["cnt"]
            if exp_failures > total:
                total = exp_failures
    except sqlite3.OperationalError:
        # experiments table may not exist yet
        pass
    except Exception as e:
        log.warning(f"Failed to query experiments for {task_type}: {e}")

    return texts, total


# ---------------------------------------------------------------------------
# TemplateEvolver
# ---------------------------------------------------------------------------

class TemplateEvolver:
    """Analyzes failures and generates improved template versions."""

    def __init__(
        self,
        templates_dir: Optional[str] = None,
    ):
        self.templates_dir = Path(templates_dir) if templates_dir else TEMPLATES_DIR

    # -- Analysis -------------------------------------------------------------

    def analyze_failures(self, task_type: str) -> FailureAnalysis:
        """
        Analyze failed tasks to identify common failure patterns.

        Scans collected failure text for known error keywords. Patterns are
        matched against prompt text as a weak signal — primarily useful for
        detecting consistent user-described errors (e.g. "getting import error").
        """
        texts, total_failures = _collect_failure_texts(task_type)

        patterns_found: list[str] = []
        issues: list[str] = []

        for pattern_name, pattern_info in FAILURE_PATTERNS.items():
            for text in texts:
                if any(kw in text for kw in pattern_info["keywords"]):
                    if pattern_name not in patterns_found:
                        patterns_found.append(pattern_name)
                        matched_kw = next(
                            kw for kw in pattern_info["keywords"] if kw in text
                        )
                        issues.append(
                            f"{pattern_name}: '{matched_kw}' found in failure data"
                        )
                    break  # One match per pattern is enough

        suggested = [
            f"Add to [{info['section']}]: {info['fix']}"
            for p in patterns_found
            if (info := FAILURE_PATTERNS.get(p))
        ]

        return FailureAnalysis(
            task_type=task_type,
            total_failures=total_failures,
            common_issues=issues,
            failure_patterns=patterns_found,
            suggested_improvements=(
                suggested if suggested
                else ["No patterns detected — more failure data needed"]
            ),
        )

    # -- Improvement suggestions ----------------------------------------------

    def suggest_improvements(self, task_type: str) -> list[Improvement]:
        """
        Generate specific template improvement suggestions.

        Runs analysis once and maps results to template sections.
        Returns an empty list if the template file doesn't exist.
        """
        # Single analysis call — no redundant DB queries
        analysis = self.analyze_failures(task_type)
        if not analysis.failure_patterns:
            return []

        template_path = self._find_latest_template(task_type)
        if not template_path:
            log.warning(f"No template found for task type: {task_type}")
            return []

        template = self._load_template(template_path)
        if not template:
            return []

        sections = {s["name"]: s for s in template.get("sections", [])}
        improvements: list[Improvement] = []

        for pattern_name in analysis.failure_patterns:
            info = FAILURE_PATTERNS.get(pattern_name)
            if not info:
                continue

            target_section = info["section"]
            current = sections.get(target_section, {})
            current_content = current.get("content", "")

            # Skip if this fix is already present in the section
            if info["fix"].strip() in current_content:
                log.debug(f"Fix for '{pattern_name}' already present in {target_section}")
                continue

            improvements.append(Improvement(
                section_name=target_section,
                current_content=current_content[:200],
                suggested_change=info["fix"],
                rationale=(
                    f"Pattern '{pattern_name}' detected across "
                    f"{analysis.total_failures} failures"
                ),
            ))

        return improvements

    # -- Version creation -----------------------------------------------------

    def create_new_version(
        self, task_type: str, improvements: list[Improvement]
    ) -> str:
        """
        Apply improvements to the latest template and save a new version.

        File is named {task_type}_v{N}.yaml so ab_testing._discover_versions()
        finds it correctly. Returns the new version string (e.g. 'v2'),
        or empty string on failure.
        """
        if not improvements:
            log.info(f"No improvements to apply for {task_type}")
            return ""

        template_path = self._find_latest_template(task_type)
        if not template_path:
            log.warning(f"No base template for {task_type} — cannot create new version")
            return ""

        template = self._load_template(template_path)
        if not template:
            return ""

        # Determine next version number
        current_version = template.get("version", "v1")
        try:
            version_num = int(current_version.lstrip("v"))
        except ValueError:
            version_num = 1
        new_version = f"v{version_num + 1}"

        # Apply improvements to sections
        sections = template.get("sections", [])
        applied: list[str] = []
        for improvement in improvements:
            for section in sections:
                if section["name"] == improvement.section_name:
                    section["content"] = (
                        section["content"].rstrip()
                        + "\n"
                        + improvement.suggested_change
                        + "\n"
                    )
                    applied.append(improvement.section_name)
                    break

        if not applied:
            log.warning(f"No sections matched for improvements in {task_type}")
            return ""

        # Update metadata
        template["version"] = new_version
        template["created_at"] = datetime.now().strftime("%Y-%m-%d")
        template["success_rate"] = None

        # Save — explicit utf-8 to avoid cp1252 corruption on Windows
        new_filename = f"{task_type}_{new_version}.yaml"
        new_path = self.templates_dir / new_filename

        try:
            new_path.write_text(
                yaml.dump(template, default_flow_style=False, sort_keys=False,
                          allow_unicode=True),
                encoding="utf-8",
            )
            log.info(
                f"Created template {new_filename} with {len(applied)} improvement(s) "
                f"in section(s): {', '.join(applied)}"
            )
            return new_version
        except Exception as e:
            log.error(f"Failed to write new template {new_filename}: {e}")
            return ""

    # -- Top-level entry point ------------------------------------------------

    def evolve_if_needed(
        self,
        task_type: str,
        min_failures: int = DEFAULT_MIN_FAILURES,
    ) -> Optional[str]:
        """
        Check if evolution is warranted and create a new version if so.

        Returns the new version string (e.g. 'v2') or None if evolution
        was skipped (not enough failures, no patterns found, or write error).
        """
        analysis = self.analyze_failures(task_type)

        if analysis.total_failures < min_failures:
            log.info(
                f"Evolution skipped for '{task_type}': "
                f"{analysis.total_failures}/{min_failures} failures required"
            )
            return None

        improvements = self.suggest_improvements(task_type)
        if not improvements:
            log.info(f"Evolution skipped for '{task_type}': no new improvements to apply")
            return None

        new_version = self.create_new_version(task_type, improvements)
        if new_version:
            log.info(
                f"Evolved '{task_type}' → {new_version} "
                f"({len(improvements)} improvement(s) applied)"
            )
        return new_version or None

    # -- Private helpers ------------------------------------------------------

    def _find_latest_template(self, task_type: str) -> Optional[Path]:
        """
        Find the most recent template file for a task type.

        Guards against missing directory to avoid FileNotFoundError on first run.
        Matches files as {task_type}.yaml or {task_type}_v*.yaml.
        """
        if not self.templates_dir.exists():
            log.warning(f"Templates directory not found: {self.templates_dir}")
            return None

        matches = sorted(self.templates_dir.glob(f"{task_type}*.yaml"))
        return matches[-1] if matches else None

    @staticmethod
    def _load_template(path: Path) -> Optional[dict]:
        """Load and parse a YAML template file. Returns None on error."""
        try:
            return yaml.safe_load(path.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Failed to load template {path}: {e}")
            return None