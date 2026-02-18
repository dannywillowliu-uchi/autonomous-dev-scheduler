# Mission State
Objective: Implement two features from BACKLOG.md:

1. P7 - DYNAMIC AGENT COMPOSITION: Define worker specializations as markdown templates (test-writer.md, refactorer.md, debugger.md) that can be loaded at runtime. Let the planner select which specialist to assign to each work unit based on task type. Add a specialist_templates/ config directory with at least 3 specialist profiles. Wire specialist selection into worker.py dispatch based on work unit metadata. Add comprehensive tests.

2. P8 - RUNTIME TOOL SYNTHESIS: Add mid-task reflection checkpoint in workers. After initial analysis, workers assess "Would creating a custom tool accelerate this work?" Workers can create project-specific helpers (custom linters, test generators, analyzers) that persist for the duration of the round. Add tool persistence mechanism with cleanup at round end. Add comprehensive tests.

Read BACKLOG.md for full specs. Each feature: implement, add tests, ensure all existing tests pass.

## Remaining
The planner should focus on what hasn't been done yet.
Do NOT re-target files in the 'Files Modified' list unless fixing a failure.
