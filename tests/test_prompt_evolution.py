"""Tests for prompt evolution engine (Mission 11)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mission_control.config import PromptEvolutionConfig, load_config
from mission_control.db import Database
from mission_control.models import PromptOutcome, PromptVariant
from mission_control.prompt_evolution import PromptEvolutionEngine

# -- Model tests --

def test_prompt_variant_defaults() -> None:
	v = PromptVariant()
	assert v.component == ""
	assert v.variant_id == ""
	assert v.content == ""
	assert v.win_rate == 0.0
	assert v.sample_count == 0
	assert v.parent_variant_id == ""
	assert v.id  # auto-generated
	assert v.created_at  # auto-generated


def test_prompt_variant_fields() -> None:
	v = PromptVariant(
		id="pv1", component="worker", variant_id="worker-v1",
		content="Be precise", win_rate=0.8, sample_count=10,
		parent_variant_id="worker-v0",
	)
	assert v.id == "pv1"
	assert v.component == "worker"
	assert v.variant_id == "worker-v1"
	assert v.win_rate == 0.8
	assert v.sample_count == 10
	assert v.parent_variant_id == "worker-v0"


def test_prompt_outcome_defaults() -> None:
	o = PromptOutcome()
	assert o.variant_id == ""
	assert o.outcome == ""
	assert o.context == ""
	assert o.id
	assert o.recorded_at


def test_prompt_outcome_fields() -> None:
	o = PromptOutcome(
		id="po1", variant_id="worker-v1", outcome="pass",
		context='{"unit_id": "wu1"}',
	)
	assert o.id == "po1"
	assert o.variant_id == "worker-v1"
	assert o.outcome == "pass"
	assert o.context == '{"unit_id": "wu1"}'


# -- Config tests --

def test_prompt_evolution_config_defaults() -> None:
	cfg = PromptEvolutionConfig()
	assert cfg.enabled is False
	assert cfg.mutation_model == "sonnet"
	assert cfg.exploration_factor == 1.4
	assert cfg.min_samples_before_mutation == 5


def test_prompt_evolution_config_toml_parsing(tmp_path: Path) -> None:
	p = tmp_path / "mission-control.toml"
	p.write_text("""\
[target]
name = "test"
path = "."

[prompt_evolution]
enabled = true
mutation_model = "opus"
exploration_factor = 2.0
min_samples_before_mutation = 10
""")
	config = load_config(p)
	assert config.prompt_evolution.enabled is True
	assert config.prompt_evolution.mutation_model == "opus"
	assert config.prompt_evolution.exploration_factor == 2.0
	assert config.prompt_evolution.min_samples_before_mutation == 10


def test_prompt_evolution_absent_in_toml(tmp_path: Path) -> None:
	p = tmp_path / "mission-control.toml"
	p.write_text('[target]\nname = "test"\npath = "."\n')
	config = load_config(p)
	assert config.prompt_evolution.enabled is False


# -- DB CRUD tests --

@pytest.fixture()
def db() -> Database:
	return Database(":memory:")


def test_insert_and_get_variant(db: Database) -> None:
	v = PromptVariant(
		id="pv1", component="worker", variant_id="worker-v1",
		content="Be precise", win_rate=0.75, sample_count=4,
	)
	db.insert_prompt_variant(v)
	got = db.get_prompt_variant("worker-v1")
	assert got is not None
	assert got.id == "pv1"
	assert got.component == "worker"
	assert got.content == "Be precise"
	assert got.win_rate == 0.75
	assert got.sample_count == 4


def test_get_variant_not_found(db: Database) -> None:
	assert db.get_prompt_variant("nonexistent") is None


def test_update_variant(db: Database) -> None:
	v = PromptVariant(id="pv1", component="worker", variant_id="worker-v1", content="v1")
	db.insert_prompt_variant(v)
	v.win_rate = 0.9
	v.sample_count = 20
	db.update_prompt_variant(v)
	got = db.get_prompt_variant("worker-v1")
	assert got is not None
	assert got.win_rate == 0.9
	assert got.sample_count == 20


def test_get_variants_for_component_ordered(db: Database) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-low",
		content="low", win_rate=0.3, sample_count=10,
	))
	db.insert_prompt_variant(PromptVariant(
		id="pv2", component="worker", variant_id="w-high",
		content="high", win_rate=0.9, sample_count=10,
	))
	db.insert_prompt_variant(PromptVariant(
		id="pv3", component="planner", variant_id="p-mid",
		content="mid", win_rate=0.6,
	))
	variants = db.get_prompt_variants_for_component("worker")
	assert len(variants) == 2
	assert variants[0].variant_id == "w-high"
	assert variants[1].variant_id == "w-low"


def test_insert_and_count_outcomes(db: Database) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-v1", content="test",
	))
	db.insert_prompt_outcome(PromptOutcome(id="o1", variant_id="w-v1", outcome="pass"))
	db.insert_prompt_outcome(PromptOutcome(id="o2", variant_id="w-v1", outcome="pass"))
	db.insert_prompt_outcome(PromptOutcome(id="o3", variant_id="w-v1", outcome="fail"))
	counts = db.count_prompt_outcomes("w-v1")
	assert counts["pass"] == 2
	assert counts["fail"] == 1


def test_get_outcomes_for_variant(db: Database) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-v1", content="test",
	))
	db.insert_prompt_outcome(PromptOutcome(id="o1", variant_id="w-v1", outcome="pass"))
	db.insert_prompt_outcome(PromptOutcome(id="o2", variant_id="w-v1", outcome="fail"))
	outcomes = db.get_prompt_outcomes_for_variant("w-v1")
	assert len(outcomes) == 2


# -- Engine tests --

@pytest.fixture()
def engine(db: Database) -> PromptEvolutionEngine:
	config = PromptEvolutionConfig(enabled=True, exploration_factor=1.4, min_samples_before_mutation=5)
	return PromptEvolutionEngine(db, config)


def test_select_variant_empty(engine: PromptEvolutionEngine) -> None:
	assert engine.select_variant("worker") is None


def test_select_variant_unseen_first(engine: PromptEvolutionEngine, db: Database) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-tested",
		content="tested", win_rate=0.9, sample_count=10,
	))
	db.insert_prompt_variant(PromptVariant(
		id="pv2", component="worker", variant_id="w-unseen",
		content="unseen", win_rate=0.0, sample_count=0,
	))
	selected = engine.select_variant("worker")
	assert selected is not None
	assert selected.variant_id == "w-unseen"


def test_select_variant_ucb1_math(engine: PromptEvolutionEngine, db: Database) -> None:
	"""With C=1.4, verify UCB1 selects correctly."""
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-a",
		content="a", win_rate=0.8, sample_count=20,
	))
	db.insert_prompt_variant(PromptVariant(
		id="pv2", component="worker", variant_id="w-b",
		content="b", win_rate=0.5, sample_count=5,
	))
	# UCB1 scores: N=25, C=1.4
	# A: 0.8 + 1.4 * sqrt(ln(25)/20) = 0.8 + 1.4 * sqrt(3.219/20) = 0.8 + 0.562
	# B: 0.5 + 1.4 * sqrt(ln(25)/5) = 0.5 + 1.4 * sqrt(3.219/5) = 0.5 + 1.124
	# B should win
	selected = engine.select_variant("worker")
	assert selected is not None
	assert selected.variant_id == "w-b"


def test_select_variant_pure_exploitation(db: Database) -> None:
	"""With C=0, should always select highest win_rate."""
	config = PromptEvolutionConfig(enabled=True, exploration_factor=0.0)
	engine = PromptEvolutionEngine(db, config)
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-best",
		content="best", win_rate=0.9, sample_count=10,
	))
	db.insert_prompt_variant(PromptVariant(
		id="pv2", component="worker", variant_id="w-worse",
		content="worse", win_rate=0.3, sample_count=10,
	))
	selected = engine.select_variant("worker")
	assert selected is not None
	assert selected.variant_id == "w-best"


def test_record_outcome_updates_win_rate(engine: PromptEvolutionEngine, db: Database) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-v1",
		content="test",
	))
	engine.record_outcome("w-v1", "pass")
	engine.record_outcome("w-v1", "pass")
	engine.record_outcome("w-v1", "fail")

	v = db.get_prompt_variant("w-v1")
	assert v is not None
	assert abs(v.win_rate - 2 / 3) < 0.01
	assert v.sample_count == 3


def test_record_outcome_nonexistent_variant(engine: PromptEvolutionEngine) -> None:
	# Should not raise
	engine.record_outcome("nonexistent", "pass")


@pytest.mark.asyncio()
async def test_propose_mutation_skips_below_min_samples(
	engine: PromptEvolutionEngine, db: Database,
) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-v1",
		content="test", sample_count=2,
	))
	result = await engine.propose_mutation("worker", ["trace1"])
	assert result is None


@pytest.mark.asyncio()
async def test_propose_mutation_success(
	engine: PromptEvolutionEngine, db: Database,
) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-v1",
		content="original prompt", win_rate=0.6, sample_count=10,
	))

	mock_proc = AsyncMock()
	mock_proc.communicate = AsyncMock(return_value=(b"improved prompt content", b""))
	mock_proc.returncode = 0

	with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
		result = await engine.propose_mutation("worker", ["failure trace 1", "failure trace 2"])

	assert result is not None
	assert result.content == "improved prompt content"
	assert result.parent_variant_id == "w-v1"
	assert result.component == "worker"
	# Verify persisted in DB
	got = db.get_prompt_variant(result.variant_id)
	assert got is not None


@pytest.mark.asyncio()
async def test_propose_mutation_empty_component(engine: PromptEvolutionEngine) -> None:
	result = await engine.propose_mutation("worker", ["trace1"])
	assert result is None


@pytest.mark.asyncio()
async def test_propose_mutation_llm_failure(
	engine: PromptEvolutionEngine, db: Database,
) -> None:
	db.insert_prompt_variant(PromptVariant(
		id="pv1", component="worker", variant_id="w-v1",
		content="original", win_rate=0.6, sample_count=10,
	))

	with patch("asyncio.create_subprocess_exec", side_effect=OSError("no claude")):
		result = await engine.propose_mutation("worker", ["trace"])
	assert result is None
