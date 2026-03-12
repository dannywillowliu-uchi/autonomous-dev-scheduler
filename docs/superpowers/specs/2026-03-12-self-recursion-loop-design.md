# Self-Recursion Loop: Intel Sources, Spec Generation, Scheduling, Metrics

## Problem Statement

The auto-update pipeline exists but has a narrow input (only Claude Code GitHub releases), generates weak objectives (one-liners instead of specs), runs only when manually invoked, and has no way to measure whether self-modifications actually improve performance. The loop isn't closed.

## Goals

1. Broader intel sources: Anthropic blog/docs, frontier lab blogs, GitHub trending, ArXiv
2. LLM-powered spec generation from findings (not one-line objectives)
3. Continuous scheduling: auto-update runs as a daemon or periodic job
4. Metrics feedback loop: track swarm performance over time, correlate with self-modifications

## Non-Goals

- Real-time streaming of sources (periodic batch scan is fine)
- Full academic paper parsing (title + abstract + relevance score is enough)
- Building a separate metrics dashboard (CLI + TSV is sufficient)

---

## Part 1: Broader Intel Sources

### 1.1 Scanner Architecture

Each source gets a scanner function that returns `list[Finding]`. All scanners are registered in `sources.py` and called by `run_scan()`.

**New file: `src/autodev/intelligence/web_sources.py`**

```python
class WebSourceScanner:
	"""Scans web sources for agentic AI developments."""

	SOURCES = {
		"anthropic_blog": {
			"url": "https://www.anthropic.com/news",
			"type": "blog",
			"trust": "high",
		},
		"openai_blog": {
			"url": "https://openai.com/blog",
			"type": "blog",
			"trust": "high",
		},
		"deepmind_blog": {
			"url": "https://deepmind.google/discover/blog/",
			"type": "blog",
			"trust": "high",
		},
		"github_trending": {
			"url": "https://github.com/trending?since=weekly&spoken_language_code=en",
			"type": "trending",
			"trust": "medium",
		},
		"arxiv_agents": {
			"url": "https://arxiv.org/list/cs.AI/recent",
			"type": "papers",
			"trust": "high",
		},
	}

	async def scan(self, client: httpx.AsyncClient) -> list[Finding]:
		"""Scan all web sources for relevant findings."""
		# For each source:
		# 1. Fetch the page
		# 2. Parse entries (blog posts, repos, papers)
		# 3. Score relevance to autonomous development
		# 4. Return findings above threshold

	async def _scan_blog(self, client, url, source_name) -> list[Finding]:
		"""Parse a blog index page for recent posts."""
		# Fetch page, extract article titles + links + dates
		# Score each by AUTOMATION_KEYWORDS (reuse from claude_code.py)
		# Return findings with source=source_name

	async def _scan_github_trending(self, client) -> list[Finding]:
		"""Parse GitHub trending for agent-related repos."""
		# Fetch trending page, extract repo names + descriptions + stars
		# Filter by keywords: agent, swarm, autonomous, llm, tool-use, mcp
		# Return findings with source="github_trending"

	async def _scan_arxiv(self, client) -> list[Finding]:
		"""Parse ArXiv recent papers for agent research."""
		# Fetch RSS/atom feed for cs.AI
		# Filter by title/abstract keywords related to agents
		# Return findings with source="arxiv"
```

### 1.2 Source Registration

**In `sources.py`**: Register web_sources scanner alongside claude_code scanner.

```python
async def run_scan(threshold: float = 0.3) -> IntelReport:
	# existing scanners...
	# Add: web_sources scanner
	from autodev.intelligence.web_sources import WebSourceScanner
	web_scanner = WebSourceScanner()
	async with httpx.AsyncClient(timeout=15.0) as client:
		web_findings = await web_scanner.scan(client)
		all_findings.extend(web_findings)
```

### 1.3 Source Trust Levels

Findings carry a trust level that affects how they're processed downstream:
- `high` (Anthropic, OpenAI, DeepMind, ArXiv): Can generate proposals directly
- `medium` (GitHub trending): Needs LLM validation before proposal generation
- `low` (future: X/Twitter, HN): Requires multiple corroborating sources

Trust level is stored on the Finding object. Add `trust_level: str = "medium"` field to Finding dataclass in `intelligence/models.py`.

---

## Part 2: LLM-Powered Spec Generation

### 2.1 Spec Generator

Instead of one-line objectives, generate full implementation specs from findings.

**New file: `src/autodev/intelligence/spec_generator.py`**

```python
class SpecGenerator:
	"""Generate implementation specs from intel findings."""

	def __init__(self, project_path: Path):
		self._project_path = project_path

	async def generate_spec(self, proposal: AdaptationProposal) -> str:
		"""Generate a detailed implementation spec from a proposal.

		1. Read the source material (finding URL)
		2. Read relevant autodev modules (from proposal.target_modules)
		3. Send to LLM with prompt asking for a structured spec:
		   - Problem statement (what this improves)
		   - Changes needed (specific files, functions, patterns)
		   - Testing requirements
		   - Risk assessment
		4. Return the spec as markdown
		"""

	async def _read_source_context(self, proposal: AdaptationProposal) -> str:
		"""Fetch and summarize the source material."""
		# Use httpx to fetch the finding URL
		# Extract key content (strip HTML, keep text)
		# Truncate to ~2000 chars for LLM context

	async def _read_project_context(self, target_modules: list[str]) -> str:
		"""Read relevant project files for context."""
		# For each target module, read the first 100 lines
		# Include CLAUDE.md architecture section
		# Truncate to ~3000 chars total

	def _build_spec_prompt(self, proposal, source_ctx, project_ctx) -> str:
		"""Build the LLM prompt for spec generation."""
		# Include: proposal details, source material, project architecture
		# Ask for: problem statement, file changes, testing, risk
		# Instruct: be specific about file paths and function names
```

### 2.2 Integration with Auto-Update Pipeline

**In `auto_update.py`**:

Replace `_generate_objective()` with `_generate_spec()`:

```python
async def _generate_spec(self, proposal: AdaptationProposal) -> str:
	"""Generate a full spec for the proposal, then use it as the objective."""
	generator = SpecGenerator(Path(self._config.target.resolved_path))
	spec = await generator.generate_spec(proposal)
	# Write spec to docs/superpowers/specs/auto-{date}-{slug}.md
	# Return the spec content as the swarm objective
```

---

## Part 3: Continuous Scheduling

### 3.1 Daemon Mode

**New file: `src/autodev/scheduler.py`**

```python
class AutoUpdateScheduler:
	"""Run auto-update pipeline on a schedule."""

	def __init__(self, config: MissionConfig, db: Database):
		self._config = config
		self._db = db
		self._interval_hours: float = 24.0  # default: daily
		self._running = False

	async def run_forever(self) -> None:
		"""Main loop: scan, evaluate, execute, sleep, repeat."""
		self._running = True
		while self._running:
			try:
				pipeline = AutoUpdatePipeline(self._config, self._db)
				results = await pipeline.run()
				for r in results:
					logger.info("Processed: %s -> %s", r.title, r.action)
			except Exception:
				logger.exception("Auto-update cycle failed")
			await asyncio.sleep(self._interval_hours * 3600)

	def stop(self) -> None:
		self._running = False
```

### 3.2 CLI Command

**In `cli.py`**:

```bash
# Run once (existing)
autodev auto-update --dry-run

# Run as daemon
autodev auto-update --daemon --interval 24

# Run as daemon with custom interval
autodev auto-update --daemon --interval 12
```

Add `--daemon` flag and `--interval` (hours, default 24) to the auto-update parser.

---

## Part 4: Metrics Feedback Loop

### 4.1 Performance Metrics

Track swarm performance over time to know if self-modifications help or hurt.

**New file: `src/autodev/metrics.py`**

```python
@dataclass
class SwarmMetrics:
	"""Metrics from a single swarm run."""
	run_id: str
	timestamp: str
	test_count: int
	test_pass_rate: float
	total_cost_usd: float
	cost_per_task: float
	agent_success_rate: float
	total_duration_s: float
	tasks_completed: int
	tasks_failed: int

class MetricsTracker:
	"""Track and analyze swarm performance over time."""

	def __init__(self, db: Database, project_path: Path):
		self._db = db
		self._project_path = project_path
		self._metrics_file = project_path / ".autodev-metrics.tsv"

	def record_run(self, metrics: SwarmMetrics) -> None:
		"""Append metrics for a completed run to the TSV file."""
		# Header: run_id, timestamp, test_count, pass_rate, cost, cost_per_task,
		#         agent_success_rate, duration_s, tasks_completed, tasks_failed
		# Append row

	def get_trend(self, last_n: int = 20) -> dict:
		"""Analyze trends across recent runs."""
		# Read last N rows from TSV
		# Return: {
		#   test_count_trend: "increasing" | "stable" | "decreasing",
		#   cost_trend: "increasing" | "stable" | "decreasing",
		#   success_rate_trend: ...,
		#   improvement_velocity: float (tests gained per run),
		#   best_run: SwarmMetrics,
		#   worst_run: SwarmMetrics,
		# }

	def correlate_with_modifications(self) -> list[dict]:
		"""Correlate metrics changes with self-modifications."""
		# Read experiments.tsv and metrics.tsv
		# For each self-modification, compare metrics before vs after
		# Return list of {proposal_title, metric_delta, verdict}
```

### 4.2 Metrics Recording Hook

**In `controller.py`** (in `_generate_completion_report` or cleanup):

```python
# After generating completion report, record metrics
from autodev.metrics import MetricsTracker, SwarmMetrics
tracker = MetricsTracker(self._db, project_path)
tracker.record_run(SwarmMetrics(
	run_id=self._run_id,
	timestamp=_now_iso(),
	test_count=...,  # parse from completion report
	# ... etc
))
```

### 4.3 Metrics CLI

```bash
# Show recent metrics
autodev metrics

# Show trend analysis
autodev metrics --trend

# Correlate with self-modifications
autodev metrics --correlate
```

---

## File Changes Summary

| File | Change |
|------|--------|
| New: `src/autodev/intelligence/web_sources.py` | Blog, GitHub trending, ArXiv scanners |
| New: `src/autodev/intelligence/spec_generator.py` | LLM-powered spec generation from findings |
| New: `src/autodev/scheduler.py` | Auto-update daemon with configurable interval |
| New: `src/autodev/metrics.py` | Performance tracking, trends, correlation |
| `src/autodev/intelligence/models.py` | Add `trust_level` field to Finding |
| `src/autodev/intelligence/sources.py` | Register web_sources scanner |
| `src/autodev/auto_update.py` | Replace objective generation with spec generation |
| `src/autodev/cli.py` | Add --daemon/--interval to auto-update, add metrics subcommand |
| `src/autodev/swarm/controller.py` | Metrics recording hook in cleanup |
| `.gitignore` | Add `.autodev-metrics.tsv` |
| Tests | Unit tests for web scanners, spec generator, scheduler, metrics tracker |

## Testing

- Web source scanners: mock httpx responses for each source type
- Spec generator: mock LLM call, verify spec structure
- Scheduler: mock pipeline.run(), verify loop and interval
- Metrics: verify TSV recording, trend calculation, correlation analysis
- Rate limiting interaction: verify scheduler respects daily limits
- All existing tests must continue to pass
