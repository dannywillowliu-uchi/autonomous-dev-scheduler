"""Tests for data models -- defaults, computed properties, edge cases."""

from __future__ import annotations

from mission_control.models import (
	SnapshotDelta,
	WorkUnit,
	_new_id,
	_now_iso,
)


class TestHelpers:
	def test_new_id_length(self) -> None:
		assert len(_new_id()) == 12

	def test_new_id_unique(self) -> None:
		ids = {_new_id() for _ in range(100)}
		assert len(ids) == 100

	def test_now_iso_format(self) -> None:
		ts = _now_iso()
		assert "T" in ts
		assert "+" in ts or "Z" in ts


class TestSnapshotDelta:
	def test_improved_tests_fixed(self) -> None:
		delta = SnapshotDelta(tests_fixed=3)
		assert delta.improved is True
		assert delta.regressed is False

	def test_not_improved_when_tests_broken(self) -> None:
		delta = SnapshotDelta(tests_fixed=5, tests_broken=1)
		assert delta.improved is False

	def test_not_improved_no_changes(self) -> None:
		delta = SnapshotDelta()
		assert delta.improved is False

	def test_regressed_tests_broken(self) -> None:
		delta = SnapshotDelta(tests_broken=2)
		assert delta.regressed is True

	def test_not_regressed_clean(self) -> None:
		delta = SnapshotDelta(tests_fixed=3, lint_delta=-1)
		assert delta.regressed is False

	def test_both_improved_and_regressed_impossible(self) -> None:
		"""If tests_broken > 0, improved must be False."""
		delta = SnapshotDelta(tests_fixed=5, tests_broken=1)
		assert delta.improved is False
		assert delta.regressed is True


class TestWorkUnitDefaults:
	def test_defaults(self) -> None:
		wu = WorkUnit()
		assert wu.status == "pending"
		assert wu.priority == 1
		assert wu.attempt == 0
		assert wu.max_attempts == 3
		assert wu.timeout is None
		assert wu.verification_command is None
		assert wu.depends_on == ""
		assert wu.exit_code is None
		assert wu.commit_hash is None

	def test_unique_ids(self) -> None:
		wu1 = WorkUnit()
		wu2 = WorkUnit()
		assert wu1.id != wu2.id


