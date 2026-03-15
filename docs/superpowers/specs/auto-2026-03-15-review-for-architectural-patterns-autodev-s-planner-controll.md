Now I have a thorough understanding of all the target modules. Here's the implementation spec:

---

# Implementation Spec: Adopt Anthropic Agent Patterns in Swarm Planner/Controller

## Problem Statement

The swarm planner currently operates as a single monolithic LLM call per cycle: it observes state, reasons, and emits decisions all in one shot. Anthropic's "Building effective agents" guide identifies several composable patterns that improve agent output quality -- specifically **prompt chaining with gates**, **evaluator-optimizer loops**, and **orchestrator-worker delegation patterns** -- that autodev's planner does not yet use.

Key gaps:
1. **No decision validation gate.** The planner emits decisions that go straight to `execute_decisions()` with no quality check. Bad decompositions (overlapping files, impossible dependencies, vague prompts) execute without friction.
2. **No evaluator-optimizer loop.** When agents complete tasks, results flow into learnings but are never graded or scored in swarm mode. The `grading.py` evaluator exists only for legacy mission mode (`DecompositionGrade`, `compute_decomposition_grade`). Swarm mode has no equivalent.
3. **Single-call reasoning.** The planner does observation + analysis + decision-making in one LLM call. Anthropic's chaining pattern suggests separating observation/analysis from decision-making, with a gate between them to catch reasoning errors before they become costly agent spawns.
4. **Worker prompts lack structured output schemas.** Workers emit freeform `AD_RESULT` JSON. Anthropic's tool-use patterns recommend explicit schemas with validation to improve output quality.

These gaps lead to wasted compute (bad task decompositions run to failure), slow convergence (planner repeats mistakes without evaluation feedback), and inconsistent worker output quality.

## Changes Needed

### 1. Decision Validation Gate (`src/autodev/swarm/planner.py`)

Add a lightweight rule-based validation step between `_parse_decisions()` and `execute_decisions()`. This implements Anthropic's "gate" pattern -- a programmatic check between chain steps that catches obvious errors before they consume resources.

**New method: `DrivingPlanner._validate_decisions()`**

```python
def _validate_decisions(
    self, decisions: list[PlannerDecision], state: SwarmState
) -> list[PlannerDecision]:
```

Validation rules (all algorithmic, no LLM call):
- **File overlap check**: If two `spawn` decisions target tasks with overlapping `files_hint`, demote the lower-priority one to `create_task` (don't spawn yet). Uses existing `overlap.py` logic.
- **Duplicate task check**: If `create_task` has a title >80% similar (normalized Levenshtein or substring match) to an existing pending/in-progress task, drop it and log a warning.
- **Spawn budget check**: If total spawns would exceed `max_agents - active_agents`, drop lowest-priority spawns. Currently this is partly in `controller.py` at `_handle_spawn()` lines 161-175 but silently fails -- move the check earlier with explicit logging.
- **Empty prompt check**: Reject `spawn` decisions where `payload["prompt"]` is empty or <50 chars.
- **Circular dependency check**: For `create_task` with `depends_on`, verify no cycles exist in the task graph.

**Integration point in `DrivingPlanner.run()`** (line 85 and 115):
```python
# Before: await self._controller.execute_decisions(decisions)
# After:
decisions = self._validate_decisions(decisions, state)
await self._controller.execute_decisions(decisions)
```

### 2. Swarm Decomposition Evaluator (`src/autodev/swarm/evaluator.py`) -- NEW FILE

Port the evaluator-optimizer pattern from Anthropic's guide to swarm mode. This is a new module that grades planner cycle quality and feeds scores back into the next cycle's prompt.

**Class: `CycleEvaluator`**

```python
@dataclass
class CycleGrade:
    cycle_number: int
    task_quality_score: float      # 0-1: clarity, independence, testability of created tasks
    agent_utilization_score: float  # 0-1: ratio of productive agent time vs idle/wasted
    convergence_score: float       # 0-1: are we moving toward the objective?
    composite_score: float         # weighted average
    feedback: str                  # one-line feedback for planner prompt

class CycleEvaluator:
    def __init__(self) -> None:
        self._history: list[CycleGrade] = []

    def grade_cycle(
        self,
        decisions: list[PlannerDecision],
        results: list[dict[str, Any]],
        state_before: SwarmState,
        state_after: SwarmState,
    ) -> CycleGrade:
```

**Scoring logic** (all algorithmic, no LLM):

- `task_quality_score`: Penalize tasks with no `files_hint` (-0.2), no `description` or description <100 chars (-0.3), overlapping files with existing tasks (-0.2). Reward tasks with `depends_on` properly set (+0.1) and clear test-related keywords in description (+0.1).
- `agent_utilization_score`: `(agents_that_completed_work) / (total_agents_spawned_this_cycle)`. Penalize if agents were killed before 5-minute minimum (-0.3 per).
- `convergence_score`: Compare `state_after.core_test_results["pass"]` vs `state_before`; compare completed task count delta. Positive deltas = high score.
- `composite_score`: Weighted average with configurable weights (default: 0.3 task, 0.3 utilization, 0.4 convergence).

**Integration in `DrivingPlanner._plan_cycle()`** (around line 311):

After executing decisions and before the next cycle, grade the previous cycle and inject feedback into the prompt:
```python
if self._evaluator._history:
    latest = self._evaluator._history[-1]
    state_text += f"\n\n## Previous Cycle Grade\n{latest.feedback}"
```

**Integration in `DrivingPlanner._log_cycle()`** (line 521):

Call `self._evaluator.grade_cycle()` with the before/after states and store the grade.

### 3. Two-Step Planner Chain (`src/autodev/swarm/planner.py`, `src/autodev/swarm/prompts.py`)

Implement Anthropic's prompt chaining pattern: split the single planner LLM call into two steps with a gate between them.

**Step 1: Analysis call** -- Observe state, identify problems, suggest priorities. Output is a structured analysis (not decisions).

**Step 2: Decision call** -- Given the analysis, produce the JSON decision array.

**Gate between steps** -- Validate the analysis makes sense before feeding to step 2. This is the programmatic check.

**New prompt template in `prompts.py`:**

```python
ANALYSIS_PROMPT_TEMPLATE = """\
## Current Swarm State

{state_text}

## Your Task

Analyze the current state. Do NOT make decisions yet. Instead, output a structured analysis:

1. **Status Assessment**: Are we on track? What's working? What's not?
2. **Top 3 Priorities**: What should we focus on next, and why?
3. **Risk Factors**: What could go wrong? What are we missing?
4. **Resource Assessment**: Do we have too many/few agents? Right mix of roles?

Output your analysis as a JSON object:
```json
{{
  "status": "on_track|stagnating|blocked|recovering",
  "priorities": [
    {{"focus": "...", "reason": "...", "impact": "high|medium|low"}}
  ],
  "risks": ["..."],
  "resource_recommendation": "scale_up|scale_down|rebalance|maintain"
}}
```
"""

DECISION_FROM_ANALYSIS_PROMPT = """\
## Analysis

{analysis_json}

## Current State Summary

{state_summary}

## Your Task

Based on the analysis above, produce concrete decisions. Respond with ONLY a JSON array of decisions.
{decision_types_reference}
"""
```

**Modified `_plan_cycle()` in `planner.py`:**

```python
async def _plan_cycle(self, state: SwarmState) -> list[PlannerDecision]:
    # Step 1: Analysis
    analysis_prompt = ANALYSIS_PROMPT_TEMPLATE.format(state_text=state_text)
    analysis_response = await self._call_llm(analysis_prompt)
    analysis = self._parse_analysis(analysis_response)

    # Gate: validate analysis is coherent
    if not analysis or analysis.get("status") not in ("on_track", "stagnating", "blocked", "recovering"):
        # Fall back to single-call mode
        return await self._single_call_plan(state)

    # Step 2: Decisions from analysis
    decision_prompt = DECISION_FROM_ANALYSIS_PROMPT.format(
        analysis_json=json.dumps(analysis, indent=2),
        state_summary=self._compact_state_summary(state),
        decision_types_reference=DECISION_TYPES_REF,
    )
    response = await self._call_llm(decision_prompt)
    return self._parse_decisions(response)
```

**Configuration**: Add `two_step_planning: bool = True` to `SwarmConfig` in `config.py`. When `False`, preserves the current single-call behavior.

**Fallback**: If the analysis call fails or produces unparseable output, fall back to the existing single-call `_plan_cycle()` logic (renamed to `_single_call_plan()`).

### 4. Worker Output Schema Validation (`src/autodev/swarm/controller.py`)

Improve worker output quality by validating `AD_RESULT` against a schema, implementing Anthropic's tool-use patterns for structured output.

**New method in `SwarmController`:**

```python
def _validate_ad_result(self, raw_result: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    """Validate and normalize an AD_RESULT from a worker.

    Returns (normalized_result, warnings).
    """
```

**Validation rules:**
- `status` must be one of `"completed"`, `"failed"`, `"blocked"` (default to `"failed"` if missing)
- `summary` must be non-empty string (flag warning if <20 chars)
- `files_changed` must be a list of strings (coerce from comma-separated string if needed)
- `discoveries` must be a list (coerce from single string if needed)
- `commits` must be a list (default to `[]`)

**Integration in `_handle_agent_death()`** (controller.py ~line 348): validate before recording learnings. Log warnings for malformed results so the planner can see agent output quality issues.

### 5. Prompt Engineering Improvements (`src/autodev/swarm/prompts.py`)

Apply Anthropic's prompt engineering guidance to improve planner output quality:

**5a. Add explicit "think step by step" reasoning block** in `CYCLE_PROMPT_TEMPLATE`:

Add before the decision request:
```
Before deciding, think through these steps:
1. What changed since last cycle? (new completions, failures, reports)
2. What is the current bottleneck?
3. What is the highest-leverage action right now?
4. Will my decisions create file conflicts with active agents?
```

**5b. Add negative examples** to `SYSTEM_PROMPT` (Anthropic recommends showing what NOT to do):

```
## Common Mistakes to Avoid

- DO NOT spawn multiple agents targeting the same files -- this causes merge conflicts
- DO NOT create tasks with vague descriptions like "fix the bug" -- be specific about which bug, in which file, with what symptoms
- DO NOT kill agents that have been running for less than 5 minutes -- they need time to work
- DO NOT retry a failed approach without changing the strategy
```

**5c. Add few-shot examples** for common scenarios in `CYCLE_PROMPT_TEMPLATE`:

Add 2-3 concrete examples showing good decision-making in stagnation, success, and failure scenarios. Keep total prompt growth under 500 tokens.

### 6. Planner Self-Reflection on Failures (`src/autodev/swarm/planner.py`)

When recording learnings from failed agents, add a structured reflection step that maps to Anthropic's evaluator pattern:

**Modified `_record_learnings()` method:**

After recording the raw failure, check if the same task has failed 2+ times. If so, construct a `reflection` entry that asks:
- Was the task description clear enough?
- Was the right agent role assigned?
- Were dependencies satisfied?

This reflection is stored in `SwarmLearnings` under a new section type and surfaced in the planner prompt.

**New method in `SwarmLearnings`:**

```python
def add_reflection(self, task_title: str, failures: list[str], assessment: str) -> bool:
    """Record a structured reflection on repeated failures."""
```

## File Change Summary

| File | Change Type | Description |
|------|-------------|-------------|
| `src/autodev/swarm/planner.py` | Modified | Add `_validate_decisions()`, two-step chaining, evaluator integration, reflection on failures |
| `src/autodev/swarm/prompts.py` | Modified | Add `ANALYSIS_PROMPT_TEMPLATE`, `DECISION_FROM_ANALYSIS_PROMPT`, step-by-step reasoning, negative examples |
| `src/autodev/swarm/evaluator.py` | **New** | `CycleEvaluator`, `CycleGrade` dataclass, algorithmic scoring |
| `src/autodev/swarm/controller.py` | Modified | Add `_validate_ad_result()`, integrate in `_handle_agent_death()` |
| `src/autodev/swarm/models.py` | Modified | Add `CycleGrade` dataclass if not in evaluator.py |
| `src/autodev/swarm/learnings.py` | Modified | Add `add_reflection()` method |
| `src/autodev/config.py` | Modified | Add `two_step_planning: bool` to `SwarmConfig` |
| `src/autodev/swarm/worker_prompt.py` | Modified | Add AD_RESULT schema reference section |

## Testing Requirements

### Unit Tests

**`tests/test_decision_validation.py`** -- NEW
- `test_validate_drops_overlapping_file_spawns`: Two spawn decisions with overlapping `files_hint` -- lower-priority one demoted to `create_task`
- `test_validate_drops_duplicate_tasks`: `create_task` with title matching existing pending task is dropped
- `test_validate_enforces_spawn_budget`: 5 spawn decisions with `max_agents=3` and 1 active -- only 2 survive
- `test_validate_rejects_empty_prompt`: `spawn` with empty/short prompt is rejected
- `test_validate_detects_circular_deps`: `create_task` A depends on B, B depends on A -- both flagged
- `test_validate_passes_clean_decisions`: Valid decisions pass through unchanged

**`tests/test_cycle_evaluator.py`** -- NEW
- `test_grade_high_quality_cycle`: All tasks have files_hint, description >100 chars, no overlap -- score >0.8
- `test_grade_low_quality_cycle`: Vague tasks, overlapping files, killed agents -- score <0.4
- `test_convergence_score_increases_on_test_improvement`: More passing tests in state_after vs state_before
- `test_convergence_score_zero_on_no_change`: Same test count, same completion count
- `test_feedback_string_identifies_weakest_area`: Feedback mentions the lowest-scoring dimension
- `test_history_accumulates`: Multiple `grade_cycle()` calls build up `_history`

**`tests/test_ad_result_validation.py`** -- NEW
- `test_valid_result_passes`: Well-formed AD_RESULT returns no warnings
- `test_missing_status_defaults_to_failed`: Result without `status` gets `"failed"`
- `test_string_files_changed_coerced_to_list`: `"files_changed": "a.py, b.py"` becomes `["a.py", "b.py"]`
- `test_empty_summary_flagged`: Warning logged for empty summary
- `test_malformed_discoveries_coerced`: Single string discovery becomes list

**`tests/test_two_step_planner.py`** -- NEW
- `test_analysis_then_decision_flow`: Mock LLM returns valid analysis, then valid decisions -- both parsed correctly
- `test_analysis_parse_failure_falls_back`: Invalid analysis JSON causes fallback to single-call mode
- `test_analysis_invalid_status_falls_back`: Analysis with `status: "banana"` triggers fallback
- `test_two_step_disabled_uses_single_call`: `two_step_planning=False` skips analysis step

**`tests/test_learnings.py`** (existing) -- ADD
- `test_add_reflection_records_structured_entry`: Reflection entry contains task title, failure list, assessment
- `test_add_reflection_deduplicates`: Same reflection text is not recorded twice

**`tests/test_prompts.py`** (existing or new) -- ADD
- `test_analysis_prompt_contains_state`: `ANALYSIS_PROMPT_TEMPLATE.format()` includes state_text
- `test_decision_prompt_contains_analysis`: `DECISION_FROM_ANALYSIS_PROMPT.format()` includes analysis JSON

### Integration Tests

**`tests/test_planner_integration.py`** (extend existing)
- `test_full_cycle_with_validation_and_grading`: End-to-end test: build state -> plan cycle -> validate decisions -> execute -> grade cycle. Verify no crashes and grade is recorded.
- `test_two_step_chain_end_to_end`: Mock LLM subprocess to return analysis then decisions, verify both are parsed and decisions are validated.

### Verification

All existing tests must continue to pass:
```bash
.venv/bin/python -m pytest -q && .venv/bin/ruff check src/ tests/
```

The two-step planning feature must be backward-compatible: `two_step_planning=False` (or missing from TOML) preserves current single-call behavior exactly.

## Risk Assessment

### Risk 1: Two-Step Planning Doubles LLM Cost
**Impact:** Medium. Each planner cycle now makes 2 LLM calls instead of 1.
**Mitigation:** 
- Two-step is opt-in via `two_step_planning` config flag (defaults to `True` but easy to disable).
- The analysis call can use a cheaper/faster model (`planner_model` already configurable in `SwarmConfig`). Consider using Sonnet for analysis and Opus for decisions.
- Analysis prompt is deliberately shorter than the full cycle prompt (no decision type reference, no examples), keeping token cost lower.
- If cycle interval is 60s+, the additional latency is absorbed by the cooldown.

### Risk 2: Validation Gate Drops Valid Decisions
**Impact:** Low. The gate could be too aggressive.
**Mitigation:**
- All validation is rule-based and deterministic -- easy to debug.
- Validation logs every dropped decision with the reason, making it visible in the TUI dashboard's activity feed.
- Start with conservative thresholds (e.g., 80% title similarity for duplicate detection) and tune based on observed false positives.
- No validation rule blocks `wait`, `escalate`, or `adjust` decisions -- only `spawn` and `create_task`.

### Risk 3: Evaluator Feedback Creates Negative Loop
**Impact:** Low. Bad grades could cause the planner to over-correct.
**Mitigation:**
- Feedback is injected as informational context, not as hard constraints. The planner prompt frame says "Consider this feedback" not "You must fix this."
- Grades are rolling averages over last 3 cycles, not single-cycle snapshots, reducing noise.
- If composite score has been <0.3 for 5+ cycles, the evaluator suggests escalation rather than more self-correction.

### Risk 4: Analysis Parse Failure Rate
**Impact:** Low. The analysis step adds a new JSON parsing requirement.
**Mitigation:**
- Explicit fallback to single-call mode on any parse failure (line-for-line equivalent to current behavior).
- `_parse_analysis()` uses the same truncated-JSON repair logic as `_parse_decisions()`.
- Fallback is logged so it's visible in monitoring.

### Risk 5: Increased Prompt Size
**Impact:** Low. Adding negative examples, step-by-step reasoning, and few-shot examples grows the system prompt.
**Mitigation:**
- Total prompt growth is capped at ~500 tokens for prompt engineering changes (section 5).
- Few-shot examples are concise (3-4 lines each).
- The analysis prompt is separate from the system prompt, so the decision prompt can be kept lean.