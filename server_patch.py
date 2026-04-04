# =============================================================================
# SERVER.PY INTEGRATION PATCH
# Apply each change in order. All changes are additive — nothing gets deleted.
# =============================================================================


# -----------------------------------------------------------------------------
# CHANGE 1: Add import at top of server.py
# Find the existing imports block (around line 20-30) and add:
# -----------------------------------------------------------------------------

from conversation import ConversationSession


# -----------------------------------------------------------------------------
# CHANGE 2: Instantiate ConversationSession in voice_handler()
# Find this existing block inside voice_handler():
#
#   history: list[dict] = []
#   work_session = WorkSession()
#   planner = TaskPlanner()
#
# Add one line after planner:
# -----------------------------------------------------------------------------

    conversation_session = ConversationSession()


# -----------------------------------------------------------------------------
# CHANGE 3: Add session query detector in detect_action_fast()
# Find the existing "task list check" block:
#
#   if any(p in t for p in ["what's on my list", ...]):
#       return {"action": "check_tasks"}
#
# Add this block BEFORE the final `return None`:
# -----------------------------------------------------------------------------

    # Session memory queries
    if any(p in t for p in [
        "what did we decide", "what did we agree", "what was the plan",
        "remind me what", "what have we discussed", "what did you say about",
        "what are we building", "what's the plan", "whats the plan",
        "what did we choose", "what tech stack", "what stack did we",
    ]):
        return {"action": "query_session"}


# -----------------------------------------------------------------------------
# CHANGE 4: Handle query_session in the fast action block inside voice_handler()
# Find the existing action handler block:
#
#   elif action["action"] == "check_usage":
#       response_text = get_usage_summary()
#
# Add this block immediately after:
# -----------------------------------------------------------------------------

                        elif action["action"] == "query_session":
                            response_text = await conversation_session.query(
                                user_text, _gemini_client
                            )


# -----------------------------------------------------------------------------
# CHANGE 5: Record user exchange — add AFTER user_text is confirmed non-empty
# Find this existing line:
#
#   log.info(f"User: {user_text}")
#
# Add immediately after:
# -----------------------------------------------------------------------------

            conversation_session.add_exchange("user", user_text)


# -----------------------------------------------------------------------------
# CHANGE 6: Inject session context into generate_response()
# Find the generate_response() function definition:
#
#   async def generate_response(
#       text: str,
#       task_mgr: ClaudeTaskManager,
#       projects: list[dict],
#       conversation_history: list[dict],
#       last_response: str = "",
#       session_summary: str = "",
#   ) -> str:
#
# Step 6a — Add new parameter to signature:
# -----------------------------------------------------------------------------

#   UPDATED SIGNATURE:
async def generate_response(
    text: str,
    task_mgr: ClaudeTaskManager,
    projects: list[dict],
    conversation_history: list[dict],
    last_response: str = "",
    session_summary: str = "",
    conversation_context: str = "",        # <-- ADD THIS
) -> str:

# Step 6b — Inside generate_response(), find:
#
#   if session_summary:
#       system += f"\n\nSESSION CONTEXT (earlier in this conversation):\n{session_summary}"
#
# Add immediately after:

    if conversation_context:
        system += f"\n\nSESSION DECISIONS & PLAN:\n{conversation_context}"


# -----------------------------------------------------------------------------
# CHANGE 7: Pass conversation context into generate_response() calls
# There are TWO places in voice_handler() that call generate_response().
# Both need the new parameter added.
#
# First call (chat mode, main path):
# Find:
#   response_text = await generate_response(
#       user_text, task_manager,
#       cached_projects, history,
#       last_response=last_jarvis_response,
#       session_summary=session_summary,
#   )
#
# Replace with:
# -----------------------------------------------------------------------------

                            response_text = await generate_response(
                                user_text, task_manager,
                                cached_projects, history,
                                last_response=last_jarvis_response,
                                session_summary=session_summary,
                                conversation_context=conversation_session.get_context(),
                            )

# Second call (work mode, casual question path):
# Find:
#   response_text = await generate_response(
#       user_text, task_manager,
#       cached_projects, history,
#       last_response=last_jarvis_response,
#       session_summary=session_summary,
#   )
#
# Replace with:

                    response_text = await generate_response(
                        user_text, task_manager,
                        cached_projects, history,
                        last_response=last_jarvis_response,
                        session_summary=session_summary,
                        conversation_context=conversation_session.get_context(),
                    )


# -----------------------------------------------------------------------------
# CHANGE 8: Log planner decisions into conversation session at confirmation
# There are TWO places where a confirmed plan triggers a build.
#
# FIRST PLACE — bypass path. Find:
#
#   plan.skipped = True
#   for q in plan.pending_questions[plan.current_question_index:]:
#       ...
#   prompt = await planner.build_prompt()
#   ...
#   planner.reset()
#   response_text = "Building it now, sir."
#
# Add before planner.reset():
# -----------------------------------------------------------------------------

                        conversation_session.log_plan(planner.active_plan)

# SECOND PLACE — confirmation path. Find:
#
#   if result["confirmed"]:
#       prompt = await planner.build_prompt()
#       ...
#       planner.reset()
#       response_text = "On it, sir."
#
# Add before planner.reset():

                            conversation_session.log_plan(planner.active_plan)


# -----------------------------------------------------------------------------
# CHANGE 9: Record JARVIS response exchange + mark plan complete on dispatch
# Find the existing block after TTS is sent:
#
#   log.info(f"JARVIS: {response_text}")
#   last_jarvis_response = response_text
#
# Add between those two lines:
# -----------------------------------------------------------------------------

            conversation_session.add_exchange("assistant", response_text)


# -----------------------------------------------------------------------------
# CHANGE 10: Mark plan complete when dispatch finishes
# In _execute_prompt_project(), find the existing line:
#
#   dispatch_registry.update_status(dispatch_id, "completed", ...)
#
# The dispatch function doesn't have access to conversation_session directly
# (it's a background task). Instead, log it via history which IS passed in.
# Find in _execute_prompt_project():
#
#   if history is not None:
#       history.append({"role": "assistant", "content": f"[Dispatch result..."})
#
# Add after that block:
# -----------------------------------------------------------------------------

        # Note: conversation_session.mark_plan_complete() is handled
        # via the next user message — session_context will reflect
        # the completed dispatch from dispatch_registry automatically.
        # No direct call needed here since we don't have session reference.


# -----------------------------------------------------------------------------
# CHANGE 11: Close session on WebSocket disconnect
# Find the finally block at the bottom of voice_handler():
#
#   finally:
#       task_manager.unregister_websocket(ws)
#
# Add after:
# -----------------------------------------------------------------------------

        conversation_session.close("disconnected")


# =============================================================================
# SUMMARY OF ALL CHANGES
# =============================================================================
#
# 1.  Import ConversationSession
# 2.  Instantiate in voice_handler()
# 3.  Add query_session detector in detect_action_fast()
# 4.  Handle query_session action in voice_handler()
# 5.  Call add_exchange("user") after every transcript
# 6a. Add conversation_context param to generate_response()
# 6b. Inject context into system prompt inside generate_response()
# 7.  Pass conversation_context into both generate_response() calls
# 8.  Call log_plan() before planner.reset() in both confirmation paths
# 9.  Call add_exchange("assistant") after every JARVIS response
# 10. Note on dispatch completion (no direct call needed)
# 11. Call close() in finally block
#
# Total lines added to server.py: ~25
# Lines deleted: 0
# =============================================================================
