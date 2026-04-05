# ✅ Summary of Changes to `api_integration.yaml`

## What Was Wrong
- The template contained a reference to `claude_md` and `CLAUDE.md` in the context section.
- This is a remnant from the original Claude‑based system; the refactored server uses `JARVIS_TASK.md` (referenced as `prompt_file` in the `gather_project_context` function).

## What Was Causing It
- The template was written when the project used `CLAUDE.md` as the prompt file. After porting to Gemini, the file name changed but the template wasn’t updated.

## How I Fixed It
- Changed `claude_md` → `prompt_file` (the key used by the refactored context gatherer).
- Updated the label from `CLAUDE.md contents:` to `JARVIS_TASK.md contents:` for clarity.
- No other changes – the template is already cross‑platform and contains no macOS‑specific code.

## What You’ll Notice Now
- The template correctly references `JARVIS_TASK.md` (the new prompt file) instead of `CLAUDE.md`.
- The context gatherer in the planner will now find the prompt file under the key `prompt_file`, matching the updated test expectations.

# ✅ Summary of Changes to `bug_fix.yaml`

## What Was Wrong
- The template contained a reference to `claude_md` and `CLAUDE.md` in the context section.
- This is a remnant from the original Claude‑based system; the refactored server uses `JARVIS_TASK.md` (referenced as `prompt_file` in the `gather_project_context` function).

## What Was Causing It
- The template was written when the project used `CLAUDE.md` as the prompt file. After porting to Gemini, the file name changed but the template wasn’t updated.

## How I Fixed It
- Changed `claude_md` → `prompt_file` (the key used by the refactored context gatherer).
- Updated the label from `CLAUDE.md contents:` to `JARVIS_TASK.md contents:` for clarity.
- No other changes – the template is already cross‑platform and contains no macOS‑specific code.

## What You’ll Notice Now
- The template correctly references `JARVIS_TASK.md` (the new prompt file) instead of `CLAUDE.md`.
- The context gatherer in the planner will now find the prompt file under the key `prompt_file`, matching the updated test expectations.

# ✅ Summary of Changes to `feature_add.yaml`

## What Was Wrong
- The template contained a reference to `claude_md` and `CLAUDE.md` in the context section.
- This is a remnant from the original Claude‑based system; the refactored server uses `JARVIS_TASK.md` (referenced as `prompt_file` in the `gather_project_context` function).

## What Was Causing It
- The template was written when the project used `CLAUDE.md` as the prompt file. After porting to Gemini, the file name changed but the template wasn’t updated.

## How I Fixed It
- Changed `claude_md` → `prompt_file` (the key used by the refactored context gatherer).
- Updated the label from `CLAUDE.md contents:` to `JARVIS_TASK.md contents:` for clarity.
- No other changes – the template is already cross‑platform and contains no macOS‑specific code.

## What You’ll Notice Now
- The template correctly references `JARVIS_TASK.md` (the new prompt file) instead of `CLAUDE.md`.
- The context gatherer in the planner will now find the prompt file under the key `prompt_file`, matching the updated test expectations.

# ✅ Summary of Changes to `landing_page.yaml`

## What Was Wrong
- The template contained a reference to `claude_md` and `CLAUDE.md` in the context section.
- This is a remnant from the original Claude‑based system; the refactored server uses `JARVIS_TASK.md` (referenced as `prompt_file` in the `gather_project_context` function).

## What Was Causing It
- The template was written when the project used `CLAUDE.md` as the prompt file. After porting to Gemini, the file name changed but the template wasn’t updated.

## How I Fixed It
- Changed `claude_md` → `prompt_file` (the key used by the refactored context gatherer).
- Updated the label from `CLAUDE.md contents:` to `JARVIS_TASK.md contents:` for clarity.
- No other changes – the template is already cross‑platform and contains no macOS‑specific code.

## What You’ll Notice Now
- The template correctly references `JARVIS_TASK.md` (the new prompt file) instead of `CLAUDE.md`.
- The context gatherer in the planner will now find the prompt file under the key `prompt_file`, matching the updated test expectations.

# ✅ Summary of Changes to `refactor.yaml`

## What Was Wrong
- The template contained a reference to `claude_md` and `CLAUDE.md` in the context section.
- This is a remnant from the original Claude‑based system; the refactored server uses `JARVIS_TASK.md` (referenced as `prompt_file` in the `gather_project_context` function).

## What Was Causing It
- The template was written when the project used `CLAUDE.md` as the prompt file. After porting to Gemini, the file name changed but the template wasn’t updated.

## How I Fixed It
- Changed `claude_md` → `prompt_file` (the key used by the refactored context gatherer).
- Updated the label from `CLAUDE.md contents:` to `JARVIS_TASK.md contents:` for clarity.
- No other changes – the template is already cross‑platform and contains no macOS‑specific code.

## What You’ll Notice Now
- The template correctly references `JARVIS_TASK.md` (the new prompt file) instead of `CLAUDE.md`.
- The context gatherer in the planner will now find the prompt file under the key `prompt_file`, matching the updated test expectations.
