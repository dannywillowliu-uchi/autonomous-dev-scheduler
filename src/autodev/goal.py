"""GOAL.md fitness function engine.

Parses goal specification files, runs fitness evaluations,
tracks iteration history, and provides action ranking for the planner.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


def _now_iso() -> str:
	return datetime.now(timezone.utc).isoformat()


@dataclass
class GoalComponent:
	"""A named, weighted component of the fitness function."""

	name: str
	command: str
	weight: float = 1.0


@dataclass
class GoalAction:
	"""A suggested action with file hints and impact estimate."""

	description: str
	files_hint: list[str] = field(default_factory=list)
	estimated_impact: str = "medium"  # high, medium, low


@dataclass
class GoalSpec:
	"""Parsed GOAL.md specification."""

	name: str
	description: str = ""
	fitness_command: str = ""
	components: list[GoalComponent] = field(default_factory=list)
	target_score: float = 1.0
	constraints: list[str] = field(default_factory=list)
	actions: list[GoalAction] = field(default_factory=list)


@dataclass
class FitnessResult:
	"""Result of a fitness evaluation."""

	composite: float = 0.0
	components: dict[str, float] = field(default_factory=dict)
	timestamp: str = field(default_factory=_now_iso)
	success: bool = True
	error: str | None = None


@dataclass
class IterationEntry:
	"""A single iteration record (before/after fitness + action taken)."""

	before: FitnessResult
	after: FitnessResult
	action: str
	timestamp: str
	delta: float = 0.0  # after.composite - before.composite


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_goal_file(path: Path) -> GoalSpec:
	"""Parse a GOAL.md file into a GoalSpec.

	Required: Goal name (from heading), plus at least a Fitness or Components section.
	Optional: Description, Target, Constraints, Actions.
	"""
	text = path.read_text()
	return _parse_goal_text(text)


def _parse_goal_text(text: str) -> GoalSpec:
	"""Parse GOAL.md text content into GoalSpec."""
	# Extract goal name from first heading
	name_match = re.search(r"^#\s+Goal:\s*(.+)$", text, re.MULTILINE)
	if not name_match:
		raise ValueError("GOAL.md must have a '# Goal: <name>' heading")
	name = name_match.group(1).strip()

	# Split into sections by ## headings
	sections: dict[str, str] = {}
	section_pattern = re.compile(r"^##\s+(\w+)\s*$", re.MULTILINE)
	matches = list(section_pattern.finditer(text))
	for i, m in enumerate(matches):
		section_name = m.group(1).lower()
		start = m.end()
		end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
		sections[section_name] = text[start:end].strip()

	# Extract description: text between goal heading and first ## section
	desc = ""
	desc_start = name_match.end()
	desc_end = matches[0].start() if matches else len(text)
	desc_candidate = text[desc_start:desc_end].strip()
	if desc_candidate:
		desc = desc_candidate

	# Require at least Fitness or Components
	if "fitness" not in sections and "components" not in sections:
		raise ValueError("GOAL.md must have at least a Fitness or Components section")

	# Parse fitness command
	fitness_command = sections.get("fitness", "").strip()

	# Parse components
	components = _parse_components(sections.get("components", ""))

	# Parse target
	target_score = 1.0
	if "target" in sections:
		target_text = sections["target"].strip()
		try:
			target_score = float(target_text)
		except ValueError:
			# Try to extract first float from the text
			float_match = re.search(r"(\d+\.?\d*)", target_text)
			if float_match:
				target_score = float(float_match.group(1))

	# Parse constraints
	constraints = _parse_list_items(sections.get("constraints", ""))

	# Parse actions
	actions = _parse_actions(sections.get("actions", ""))

	return GoalSpec(
		name=name,
		description=desc,
		fitness_command=fitness_command,
		components=components,
		target_score=target_score,
		constraints=constraints,
		actions=actions,
	)


def _parse_components(text: str) -> list[GoalComponent]:
	"""Parse component list items like: - name (weight: 0.5): command"""
	if not text.strip():
		return []
	components = []
	pattern = re.compile(
		r"^-\s+(.+?)\s+\(weight:\s*([\d.]+)\):\s*(.+)$",
		re.MULTILINE,
	)
	for m in pattern.finditer(text):
		components.append(GoalComponent(
			name=m.group(1).strip(),
			command=m.group(3).strip(),
			weight=float(m.group(2)),
		))
	return components


def _parse_actions(text: str) -> list[GoalAction]:
	"""Parse action list items with optional [files: ...] and [impact: ...] tags."""
	if not text.strip():
		return []
	actions = []
	for line in text.splitlines():
		line = line.strip()
		if not line.startswith("-"):
			continue
		line = line[1:].strip()

		# Extract [files: ...] tag
		files_hint: list[str] = []
		files_match = re.search(r"\[files?:\s*([^\]]+)\]", line)
		if files_match:
			files_hint = [f.strip() for f in files_match.group(1).split(",")]
			line = line[:files_match.start()] + line[files_match.end():]

		# Extract [impact: ...] tag
		impact = "medium"
		impact_match = re.search(r"\[impact:\s*(\w+)\]", line)
		if impact_match:
			impact = impact_match.group(1).strip().lower()
			line = line[:impact_match.start()] + line[impact_match.end():]

		description = line.strip()
		if description:
			actions.append(GoalAction(
				description=description,
				files_hint=files_hint,
				estimated_impact=impact,
			))
	return actions


def _parse_list_items(text: str) -> list[str]:
	"""Parse simple markdown list items."""
	if not text.strip():
		return []
	items = []
	for line in text.splitlines():
		line = line.strip()
		if line.startswith("-"):
			item = line[1:].strip()
			if item:
				items.append(item)
	return items


# ---------------------------------------------------------------------------
# Fitness evaluation
# ---------------------------------------------------------------------------

def run_fitness(spec: GoalSpec, cwd: Path, timeout: int = 60) -> FitnessResult:
	"""Run fitness evaluation.

	If components exist, runs each component command individually and computes
	a weighted composite. Otherwise, runs the top-level fitness command.
	"""
	if spec.components:
		return _run_component_fitness(spec, cwd, timeout)
	if spec.fitness_command:
		return _run_single_fitness(spec.fitness_command, cwd, timeout)
	return FitnessResult(success=False, error="No fitness command or components defined")


def _run_single_fitness(command: str, cwd: Path, timeout: int) -> FitnessResult:
	"""Run a single fitness command and parse the last line as a float."""
	try:
		result = subprocess.run(
			command,
			shell=True,  # nosec B602 -- commands from project GOAL.md, not user input
			cwd=str(cwd),
			capture_output=True,
			text=True,
			timeout=timeout,
		)
	except subprocess.TimeoutExpired:
		return FitnessResult(success=False, error=f"Timeout after {timeout}s: {command}")

	if result.returncode != 0:
		return FitnessResult(
			success=False,
			error=f"Exit code {result.returncode}: {result.stderr.strip()}",
		)

	# Parse last non-empty line as float
	lines = [ln.strip() for ln in result.stdout.strip().splitlines() if ln.strip()]
	if not lines:
		return FitnessResult(success=False, error="No output from fitness command")

	try:
		score = float(lines[-1])
	except ValueError:
		return FitnessResult(
			success=False,
			error=f"Could not parse score from output: {lines[-1]!r}",
		)

	return FitnessResult(composite=score)


def _run_component_fitness(
	spec: GoalSpec, cwd: Path, timeout: int,
) -> FitnessResult:
	"""Run each component command and compute weighted composite."""
	component_scores: dict[str, float] = {}
	total_weight = sum(c.weight for c in spec.components)

	if total_weight == 0:
		return FitnessResult(success=False, error="Total component weight is zero")

	for comp in spec.components:
		result = _run_single_fitness(comp.command, cwd, timeout)
		if not result.success:
			return FitnessResult(
				success=False,
				error=f"Component '{comp.name}' failed: {result.error}",
				components=component_scores,
			)
		component_scores[comp.name] = result.composite

	composite = sum(
		component_scores[c.name] * c.weight for c in spec.components
	) / total_weight

	return FitnessResult(composite=composite, components=component_scores)


# ---------------------------------------------------------------------------
# Iteration log
# ---------------------------------------------------------------------------

class IterationLog:
	"""Persistent iteration history backed by a JSON file."""

	def __init__(self, path: Path):
		self._path = path

	def record(self, before: FitnessResult, after: FitnessResult, action: str) -> None:
		"""Append an iteration entry."""
		entries = self.load()
		entry = IterationEntry(
			before=before,
			after=after,
			action=action,
			timestamp=_now_iso(),
			delta=after.composite - before.composite,
		)
		entries.append(entry)
		self._save(entries)

	def load(self) -> list[IterationEntry]:
		"""Load all iteration entries from disk."""
		if not self._path.exists():
			return []
		try:
			data = json.loads(self._path.read_text())
		except (json.JSONDecodeError, OSError):
			return []
		return [_entry_from_dict(d) for d in data]

	def trend(self) -> list[float]:
		"""Return composite scores over time (after-scores)."""
		return [e.after.composite for e in self.load()]

	def latest(self) -> IterationEntry | None:
		"""Return the most recent entry, or None."""
		entries = self.load()
		return entries[-1] if entries else None

	def _save(self, entries: list[IterationEntry]) -> None:
		self._path.parent.mkdir(parents=True, exist_ok=True)
		data = [_entry_to_dict(e) for e in entries]
		self._path.write_text(json.dumps(data, indent=2))


def _fitness_to_dict(fr: FitnessResult) -> dict:
	return {
		"composite": fr.composite,
		"components": fr.components,
		"timestamp": fr.timestamp,
		"success": fr.success,
		"error": fr.error,
	}


def _fitness_from_dict(d: dict) -> FitnessResult:
	return FitnessResult(
		composite=d.get("composite", 0.0),
		components=d.get("components", {}),
		timestamp=d.get("timestamp", ""),
		success=d.get("success", True),
		error=d.get("error"),
	)


def _entry_to_dict(e: IterationEntry) -> dict:
	return {
		"before": _fitness_to_dict(e.before),
		"after": _fitness_to_dict(e.after),
		"action": e.action,
		"timestamp": e.timestamp,
		"delta": e.delta,
	}


def _entry_from_dict(d: dict) -> IterationEntry:
	return IterationEntry(
		before=_fitness_from_dict(d["before"]),
		after=_fitness_from_dict(d["after"]),
		action=d["action"],
		timestamp=d["timestamp"],
		delta=d.get("delta", 0.0),
	)


# ---------------------------------------------------------------------------
# Stopping criteria
# ---------------------------------------------------------------------------

def check_stopping(
	spec: GoalSpec,
	result: FitnessResult,
	max_iterations: int = 0,
	iteration_count: int = 0,
) -> bool:
	"""Return True if the goal is met or iteration limit is reached."""
	if result.composite >= spec.target_score:
		return True
	if max_iterations > 0 and iteration_count >= max_iterations:
		return True
	return False


# ---------------------------------------------------------------------------
# Action ranking
# ---------------------------------------------------------------------------

_IMPACT_MULTIPLIER = {"high": 3.0, "medium": 2.0, "low": 1.0}


def rank_actions(spec: GoalSpec, current: FitnessResult) -> list[GoalAction]:
	"""Rank actions by estimated potential impact.

	Uses component gaps (1.0 - score) weighted by component weight to estimate
	which actions have the most potential. Actions with higher impact estimates
	and whose file hints overlap with underperforming components rank higher.
	"""
	# Compute per-component gap scores
	component_gaps: dict[str, float] = {}
	for comp in spec.components:
		score = current.components.get(comp.name, 0.0)
		component_gaps[comp.name] = (1.0 - score) * comp.weight

	total_gap = sum(component_gaps.values()) if component_gaps else 1.0

	def action_score(action: GoalAction) -> float:
		multiplier = _IMPACT_MULTIPLIER.get(action.estimated_impact, 2.0)
		# Base score from impact level
		score = multiplier
		# Boost by total gap if no components, or by gap magnitude
		if total_gap > 0:
			score *= total_gap
		return score

	return sorted(spec.actions, key=action_score, reverse=True)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_score_summary(spec: GoalSpec, result: FitnessResult) -> str:
	"""Render a human-readable score summary for planner context."""
	lines = []
	lines.append(f"Goal: {spec.name}")
	lines.append(f"Composite: {result.composite:.3f} / {spec.target_score:.3f}")

	gap = spec.target_score - result.composite
	lines.append(f"Gap: {gap:.3f}")

	if result.components:
		lines.append("Components:")
		for comp in spec.components:
			score = result.components.get(comp.name, 0.0)
			bar_len = int(score * 20)
			bar = "#" * bar_len + "." * (20 - bar_len)
			lines.append(f"  {comp.name} (w={comp.weight}): [{bar}] {score:.3f}")

	if not result.success:
		lines.append(f"Error: {result.error}")

	return "\n".join(lines)
