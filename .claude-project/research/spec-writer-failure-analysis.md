# spec-writer-opencode Failure Analysis

**Date**: 2026-03-21
**Agent**: spec-writer-opencode (implementer)
**Task**: Write OpenCode integration spec (ID: 9d27bbb47a48)
**Duration**: ~37.8 minutes (2266s)
**Outcome**: Claim timeout, re-queued as attempt 1/3

---

## Timeline Reconstruction

| Time | Event |
|------|-------|
| 06:07:54 | Swarm launched for OpenCode research mission |
| 06:09:29 | Planner creates 3 tasks, spawns `research-opencode` + `research-autodev-backends` |
| 06:09:32 | Trace streaming error for both research agents: "chunk is longer than limit" |
| 06:14:00 | Planner cycle 3: research-opencode "writing final report" |
| 06:16:00 | `research-autodev-backends` task re-queued (attempt 1/3) -- agent died without AD_RESULT |
| 06:18:12 | Planner cycle 4: research-opencode still "writing final report" at 6m31s |
| 06:20:12 | `research-opencode` task re-queued (attempt 1/3) -- agent died without AD_RESULT |
| **06:21:27** | **spec-writer-opencode spawned** (implementer) for task 9d27bbb47a48 |
| 06:21:29 | Trace streaming error: "Separator is found, but chunk is longer than limit" |
| 06:23:27 | Planner cycle 5 complete, waits 120s for next cycle |
| ~06:23:27 - ~06:57:13 | **34-minute gap** -- planner's LLM call for cycle 6 was extremely slow |
| 06:57:13 | Planner cycle 6: claims agent is "only 2m30s" into task (INCORRECT -- actual: 35m46s) |
| **06:59:13** | **Task claim timed out after 2266s** (limit: 1800s). Re-queued attempt 1/3 |
| 07:00:45 | Planner spawns `investigate-spec-failure` + `spec-writer-v2` as replacements |

---

## Root Cause Analysis

### Primary: Claim timeout (1800s default) exceeded

The spec-writer-opencode agent ran for 2266s (37.8 min), exceeding the 1800s (30 min) claim timeout in `controller.py:61`. The timeout was reached at ~06:51:27 but was not detected until 06:59:13 because the planner's own LLM call was consuming ~34 minutes.

### Contributing Factor 1: Planner LLM call latency starved monitoring

Between cycle 5 completion (06:23:27) and cycle 6 start (06:57:13), there is a **34-minute gap** where the planner was blocked on its own LLM inference. During this window:
- No `_check_claim_timeouts()` ran (it's called inside `monitor_agents()`, which requires a planner cycle)
- The timeout expired at ~06:51:27 but wasn't detected for another ~8 minutes
- The planner's cycle 6 assessment was wrong: it said the agent was "only 2m30s" into the task when it was actually 35+ minutes in

### Contributing Factor 2: Zero visibility into agent activity

- **No inbox messages**: spec-writer-opencode never wrote to `team-lead.json`. The inbox only contains messages from `research-opencode` and `research-autodev-backends`, not the spec writer.
- **No inbox file created**: No `spec-writer-opencode.json` inbox file exists in the inboxes directory.
- **Trace streaming broken**: Every agent in this run hit "Separator is found, but chunk is longer than limit" errors, meaning the controller's `_stream_agent_output` couldn't read agent trace data.

### Contributing Factor 3: Research agents also failed to emit AD_RESULT

Both `research-opencode` and `research-autodev-backends` were re-queued because they failed to emit AD_RESULT markers, despite both completing their work:
- `research-opencode` wrote `.claude-project/research/opencode-architecture-analysis.md` (405 lines, comprehensive)
- `research-autodev-backends` wrote `docs/backend-abstraction-analysis.md` (detailed)
- Both reported completion to the planner inbox
- But neither emitted the AD_RESULT marker, so the controller treated them as failures

This is the same AD_RESULT false-failure pattern documented in the learnings file (2026-03-18).

---

## Key Questions Answered

### Did the agent exhaust max turns (200)?
**Unknown, but unlikely.** The 37-minute runtime suggests the agent was actively working, not stuck in a tight loop that would burn through 200 turns quickly. The termination was caused by the claim timeout (2266s > 1800s limit), not by max turns. However, we have no visibility into turn count because trace streaming was broken.

### Did it try to write code instead of a spec?
**No evidence of this.** No commits from the agent appear in git log. No source files were modified. The agent produced zero visible output -- no spec file, no code changes, no inbox messages. It's as if it was working internally but never wrote anything to disk before the timeout killed it.

### Did it get stuck in a loop?
**Cannot determine definitively.** With trace streaming broken ("chunk is longer than limit"), we have zero visibility into what the agent was doing. The 37-minute runtime with zero output suggests either:
1. The agent was doing extensive reading/research before writing (common for spec tasks)
2. The agent got stuck in a planning/reasoning loop without producing output
3. The agent's Claude Code subprocess was slow to respond (LLM latency)

### Did it produce any partial output that can be salvaged?
**YES -- from the research agents, not the spec writer itself:**
- `.claude-project/research/opencode-architecture-analysis.md` -- 405-line comprehensive analysis of OpenCode's architecture (tool execution, context management, session lifecycle, process model) with comparison tables against autodev
- `docs/backend-abstraction-analysis.md` -- Detailed backend abstraction layer analysis (7 abstract methods, extension points, spawn paths)
- The initial spec outline at `docs/superpowers/specs/auto-2026-03-21-research-opencode-s-architecture-for-reusable-patterns-how-i.md` (auto-update evaluator output, 10 lines)

**The spec writer itself produced ZERO output.** No files were created or modified.

### Was the task description clear enough?
**Partially.** The task was "Write OpenCode integration spec" which depends on the two research reports being available. The planner spawned the spec-writer AFTER the research agents reported completion to the inbox, but BEFORE the research tasks were actually marked complete (they were re-queued due to missing AD_RESULT). The spec writer may have:
1. Not known where to find the research output
2. Spent time re-doing research instead of synthesizing existing reports
3. Not had clear guidance on the expected output format/location

---

## Recommendations

### Immediate (for spec-writer-v2)

1. **Increase claim timeout for spec tasks**: 1800s (30 min) is too short for a synthesis task that must read 400+ lines of research and produce a detailed spec. Recommend 3600s (1 hour) for implementer-type agents on spec tasks.
2. **Include explicit file paths in task description**: Tell the agent exactly where research reports are and where to write the spec.
3. **Require early inbox reports**: Add to worker prompt: "Send a progress report within 5 minutes of starting."

### Systemic

4. **Fix trace streaming "chunk longer than limit" error**: This affected EVERY agent in this run. Without trace data, the controller is blind. Likely needs a larger buffer or chunked reading in `_stream_agent_output`.
5. **Decouple timeout checking from planner cycles**: `_check_claim_timeouts()` runs inside the planner cycle. If the planner's LLM call takes 34 minutes, timeouts aren't checked for 34 minutes. Consider running timeout checks on a separate asyncio timer.
6. **Fix planner elapsed-time calculation**: Cycle 6 claimed the agent was "2m30s" in when it was 35+ minutes in. The planner is being fed incorrect elapsed time data, or its own assessment is wrong.
7. **AD_RESULT emission remains unreliable**: Three agents in this run failed to emit AD_RESULT despite completing work. The fix from ecd6fa2 may not be sufficient, or these agents were running pre-fix code.
