"""Tests for episodic-to-semantic memory system (Mission 12)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from mission_control.config import EpisodicMemoryConfig, load_config
from mission_control.db import Database
from mission_control.memory import MemoryManager
from mission_control.models import (
	EpisodicMemory,
	Epoch,
	Handoff,
	Mission,
	Plan,
	SemanticMemory,
	WorkUnit,
)
from mission_control.planner_context import build_planner_context

# -- Model tests --

def test_episodic_memory_defaults() -> None:
	em = EpisodicMemory()
	assert em.event_type == ""
	assert em.content == ""
	assert em.outcome == ""
	assert em.scope_tokens == ""
	assert em.confidence == 1.0
	assert em.access_count == 0
	assert em.ttl_days == 30
	assert em.id
	assert em.created_at
	assert em.last_accessed


def test_episodic_memory_fields() -> None:
	em = EpisodicMemory(
		id="em1", event_type="merge_success", content="Tests passed",
		outcome="pass", scope_tokens="auth.py,models.py",
		confidence=0.9, access_count=2, ttl_days=15,
	)
	assert em.id == "em1"
	assert em.event_type == "merge_success"
	assert em.scope_tokens == "auth.py,models.py"
	assert em.confidence == 0.9
	assert em.ttl_days == 15


def test_semantic_memory_defaults() -> None:
	sm = SemanticMemory()
	assert sm.content == ""
	assert sm.source_episode_ids == ""
	assert sm.confidence == 1.0
	assert sm.application_count == 0
	assert sm.id
	assert sm.created_at


def test_semantic_memory_fields() -> None:
	sm = SemanticMemory(
		id="sm1", content="Always run tests before merging",
		source_episode_ids="em1,em2,em3",
		confidence=0.85, application_count=5,
	)
	assert sm.id == "sm1"
	assert sm.content == "Always run tests before merging"
	assert sm.source_episode_ids == "em1,em2,em3"
	assert sm.confidence == 0.85


# -- Config tests --

def test_episodic_memory_config_defaults() -> None:
	cfg = EpisodicMemoryConfig()
	assert cfg.enabled is False
	assert cfg.default_ttl_days == 30
	assert cfg.decay_alpha == 0.1
	assert cfg.access_boost_days == 5
	assert cfg.distill_model == "sonnet"
	assert cfg.distill_budget_usd == 0.30
	assert cfg.min_episodes_for_distill == 3
	assert cfg.max_semantic_per_query == 5


def test_episodic_memory_config_toml_parsing(tmp_path: Path) -> None:
	p = tmp_path / "mission-control.toml"
	p.write_text("""\
[target]
name = "test"
path = "."

[episodic_memory]
enabled = true
default_ttl_days = 60
decay_alpha = 0.2
access_boost_days = 10
distill_model = "opus"
min_episodes_for_distill = 5
max_semantic_per_query = 10
""")
	config = load_config(p)
	assert config.episodic_memory.enabled is True
	assert config.episodic_memory.default_ttl_days == 60
	assert config.episodic_memory.decay_alpha == 0.2
	assert config.episodic_memory.access_boost_days == 10
	assert config.episodic_memory.distill_model == "opus"
	assert config.episodic_memory.min_episodes_for_distill == 5


def test_episodic_memory_absent_in_toml(tmp_path: Path) -> None:
	p = tmp_path / "mission-control.toml"
	p.write_text('[target]\nname = "test"\npath = "."\n')
	config = load_config(p)
	assert config.episodic_memory.enabled is False


# -- DB CRUD tests --

@pytest.fixture()
def db() -> Database:
	return Database(":memory:")


def test_insert_and_get_episodic(db: Database) -> None:
	em = EpisodicMemory(
		id="em1", event_type="merge_success", content="Tests passed",
		scope_tokens="auth.py,models.py", confidence=0.9, ttl_days=20,
	)
	db.insert_episodic_memory(em)
	results = db.get_episodic_memories_by_scope(["auth.py"])
	assert len(results) == 1
	assert results[0].id == "em1"
	assert results[0].event_type == "merge_success"


def test_scope_overlap_ordering(db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(
		id="em1", scope_tokens="auth.py", access_count=1, confidence=0.5,
	))
	db.insert_episodic_memory(EpisodicMemory(
		id="em2", scope_tokens="auth.py,views.py", access_count=5, confidence=0.9,
	))
	results = db.get_episodic_memories_by_scope(["auth.py"])
	assert len(results) == 2
	assert results[0].id == "em2"  # higher access_count


def test_expired_excluded(db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(
		id="em1", scope_tokens="auth.py", ttl_days=0,
	))
	db.insert_episodic_memory(EpisodicMemory(
		id="em2", scope_tokens="auth.py", ttl_days=10,
	))
	results = db.get_episodic_memories_by_scope(["auth.py"])
	assert len(results) == 1
	assert results[0].id == "em2"


def test_update_episodic(db: Database) -> None:
	em = EpisodicMemory(id="em1", content="original", ttl_days=30)
	db.insert_episodic_memory(em)
	em.content = "updated"
	em.ttl_days = 20
	db.update_episodic_memory(em)
	all_mems = db.get_all_episodic_memories()
	assert len(all_mems) == 1
	assert all_mems[0].content == "updated"
	assert all_mems[0].ttl_days == 20


def test_delete_episodic(db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(id="em1"))
	db.delete_episodic_memory("em1")
	assert db.get_all_episodic_memories() == []


def test_insert_and_get_semantic(db: Database) -> None:
	sm = SemanticMemory(
		id="sm1", content="Always test auth", source_episode_ids="em1,em2",
		confidence=0.85, application_count=3,
	)
	db.insert_semantic_memory(sm)
	top = db.get_top_semantic_memories(limit=5)
	assert len(top) == 1
	assert top[0].id == "sm1"
	assert top[0].content == "Always test auth"


def test_semantic_ordering(db: Database) -> None:
	db.insert_semantic_memory(SemanticMemory(
		id="sm1", content="low", confidence=0.5, application_count=10,
	))
	db.insert_semantic_memory(SemanticMemory(
		id="sm2", content="high", confidence=0.9, application_count=1,
	))
	top = db.get_top_semantic_memories()
	assert top[0].id == "sm2"  # higher confidence


def test_update_semantic(db: Database) -> None:
	sm = SemanticMemory(id="sm1", content="original", application_count=0)
	db.insert_semantic_memory(sm)
	sm.application_count = 5
	sm.content = "refined"
	db.update_semantic_memory(sm)
	top = db.get_top_semantic_memories()
	assert top[0].application_count == 5
	assert top[0].content == "refined"


# -- MemoryManager tests --

@pytest.fixture()
def manager(db: Database) -> MemoryManager:
	config = EpisodicMemoryConfig(
		enabled=True, default_ttl_days=30, access_boost_days=5,
		min_episodes_for_distill=3,
	)
	return MemoryManager(db, config)


def test_store_episode(manager: MemoryManager, db: Database) -> None:
	em = manager.store_episode(
		event_type="merge_success",
		content="Tests passed for auth module",
		outcome="pass",
		scope_tokens=["auth.py", "models.py"],
	)
	assert em.event_type == "merge_success"
	assert em.scope_tokens == "auth.py,models.py"
	all_mems = db.get_all_episodic_memories()
	assert len(all_mems) == 1


def test_retrieve_bumps_access(manager: MemoryManager, db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(
		id="em1", scope_tokens="auth.py", access_count=0, ttl_days=30,
	))
	results = manager.retrieve_relevant(["auth.py"])
	assert len(results) == 1
	# Check access bumped in DB
	all_mems = db.get_all_episodic_memories()
	assert all_mems[0].access_count == 1


def test_decay_tick_reduces_ttl(manager: MemoryManager, db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(id="em1", ttl_days=10, access_count=0))
	evicted, extended = manager.decay_tick()
	assert evicted == 0
	assert extended == 0
	all_mems = db.get_all_episodic_memories()
	assert all_mems[0].ttl_days == 9


def test_decay_tick_evicts_expired(manager: MemoryManager, db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(id="em1", ttl_days=1, access_count=0))
	evicted, extended = manager.decay_tick()
	assert evicted == 1
	assert db.get_all_episodic_memories() == []


def test_decay_tick_extends_frequently_accessed(manager: MemoryManager, db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(id="em1", ttl_days=5, access_count=3))
	evicted, extended = manager.decay_tick()
	assert extended == 1
	all_mems = db.get_all_episodic_memories()
	# 5 + 5 (boost) - 1 (decay) = 9
	assert all_mems[0].ttl_days == 9
	assert all_mems[0].access_count == 0  # counter reset


def test_get_promote_candidates(manager: MemoryManager, db: Database) -> None:
	db.insert_episodic_memory(EpisodicMemory(id="em1", confidence=0.8, ttl_days=2))
	db.insert_episodic_memory(EpisodicMemory(id="em2", confidence=0.5, ttl_days=2))
	db.insert_episodic_memory(EpisodicMemory(id="em3", confidence=0.9, ttl_days=10))
	candidates = manager.get_promote_candidates()
	assert len(candidates) == 1
	assert candidates[0].id == "em1"


@pytest.mark.asyncio()
async def test_distill_below_min_returns_none(manager: MemoryManager) -> None:
	episodes = [EpisodicMemory(id="em1"), EpisodicMemory(id="em2")]
	result = await manager.distill_to_semantic(episodes)
	assert result is None


@pytest.mark.asyncio()
async def test_distill_success(manager: MemoryManager, db: Database) -> None:
	episodes = [
		EpisodicMemory(
			id="em1", event_type="merge_success",
			content="Auth tests passed", outcome="pass", confidence=0.8,
		),
		EpisodicMemory(
			id="em2", event_type="test_failure",
			content="Auth migration broke", outcome="fail", confidence=0.9,
		),
		EpisodicMemory(
			id="em3", event_type="merge_success",
			content="Auth refactor merged", outcome="pass", confidence=0.7,
		),
	]

	mock_proc = AsyncMock()
	mock_proc.communicate = AsyncMock(return_value=(b"Always run auth tests before merging migrations", b""))
	mock_proc.returncode = 0

	with patch("asyncio.create_subprocess_exec", return_value=mock_proc):
		result = await manager.distill_to_semantic(episodes)

	assert result is not None
	assert result.content == "Always run auth tests before merging migrations"
	assert result.source_episode_ids == "em1,em2,em3"
	# avg confidence: (0.8 + 0.9 + 0.7) / 3 = 0.8
	assert abs(result.confidence - 0.8) < 0.01
	# Verify persisted
	top = db.get_top_semantic_memories()
	assert len(top) == 1


@pytest.mark.asyncio()
async def test_distill_llm_failure(manager: MemoryManager) -> None:
	episodes = [EpisodicMemory(id=f"em{i}") for i in range(3)]
	with patch("asyncio.create_subprocess_exec", side_effect=OSError("no claude")):
		result = await manager.distill_to_semantic(episodes)
	assert result is None


# -- Planner context integration tests --

def test_semantic_memories_injected_in_planner_context(db: Database) -> None:
	db.insert_mission(Mission(id="m1", objective="test"))
	epoch = Epoch(id="ep1", mission_id="m1", number=1)
	db.insert_epoch(epoch)
	plan = Plan(id="p1", objective="test")
	db.insert_plan(plan)
	unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
	db.insert_work_unit(unit)
	handoff = Handoff(
		id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
		status="completed", summary="Done",
	)
	db.insert_handoff(handoff)

	db.insert_semantic_memory(SemanticMemory(
		id="sm1", content="Always validate inputs before DB writes",
		confidence=0.85, application_count=3,
	))

	result = build_planner_context(db, "m1")
	assert "## Learned Rules (from past missions)" in result
	assert "Always validate inputs before DB writes" in result
	assert "confidence: 0.8" in result


def test_no_semantic_memories_no_section(db: Database) -> None:
	db.insert_mission(Mission(id="m1", objective="test"))
	epoch = Epoch(id="ep1", mission_id="m1", number=1)
	db.insert_epoch(epoch)
	plan = Plan(id="p1", objective="test")
	db.insert_plan(plan)
	unit = WorkUnit(id="wu1", plan_id="p1", title="Task")
	db.insert_work_unit(unit)
	handoff = Handoff(
		id="h1", work_unit_id="wu1", round_id="", epoch_id="ep1",
		status="completed", summary="Done",
	)
	db.insert_handoff(handoff)

	result = build_planner_context(db, "m1")
	assert "## Learned Rules" not in result
