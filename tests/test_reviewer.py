"""Tests for post-session review."""

from __future__ import annotations

from mission_control.models import Session, Snapshot
from mission_control.reviewer import review_session


def _session(sid: str = "test1") -> Session:
	return Session(id=sid, target_name="proj", task_description="Fix stuff")


class TestReviewSession:
	def test_helped_tests_fixed(self) -> None:
		before = Snapshot(test_total=10, test_passed=8, test_failed=2)
		after = Snapshot(test_total=10, test_passed=10, test_failed=0)
		verdict = review_session(before, after, _session())
		assert verdict.verdict == "helped"
		assert verdict.should_revert is False
		assert len(verdict.improvements) > 0
		assert "2 test(s) fixed" in verdict.improvements

	def test_helped_lint_fixed(self) -> None:
		before = Snapshot(test_total=10, test_passed=10, test_failed=0, lint_errors=5)
		after = Snapshot(test_total=10, test_passed=10, test_failed=0, lint_errors=2)
		verdict = review_session(before, after, _session())
		assert verdict.verdict == "helped"
		assert "3 lint error(s) fixed" in verdict.improvements

	def test_hurt_tests_broken(self) -> None:
		before = Snapshot(test_total=10, test_passed=10, test_failed=0)
		after = Snapshot(test_total=10, test_passed=8, test_failed=2)
		verdict = review_session(before, after, _session())
		assert verdict.verdict == "hurt"
		assert verdict.should_revert is True
		assert "2 test(s) broken" in verdict.regressions

	def test_hurt_security_regression(self) -> None:
		before = Snapshot(security_findings=0)
		after = Snapshot(security_findings=2)
		verdict = review_session(before, after, _session())
		assert verdict.verdict == "hurt"
		assert verdict.should_revert is True

	def test_neutral_no_changes(self) -> None:
		before = Snapshot(test_total=10, test_passed=10, test_failed=0)
		after = Snapshot(test_total=10, test_passed=10, test_failed=0)
		verdict = review_session(before, after, _session())
		assert verdict.verdict == "neutral"
		assert verdict.should_revert is False
		assert verdict.should_merge is False

	def test_hurt_overrides_lint_improvement(self) -> None:
		before = Snapshot(test_total=10, test_passed=10, test_failed=0, lint_errors=10)
		after = Snapshot(test_total=10, test_passed=9, test_failed=1, lint_errors=0)
		verdict = review_session(before, after, _session())
		assert verdict.verdict == "hurt"
		assert verdict.should_revert is True

	def test_auto_merge_when_helped(self) -> None:
		before = Snapshot(test_total=10, test_passed=8, test_failed=2)
		after = Snapshot(test_total=10, test_passed=10, test_failed=0)
		verdict = review_session(before, after, _session(), auto_merge=True)
		assert verdict.should_merge is True

	def test_no_auto_merge_when_hurt(self) -> None:
		before = Snapshot(test_total=10, test_passed=10, test_failed=0)
		after = Snapshot(test_total=10, test_passed=8, test_failed=2)
		verdict = review_session(before, after, _session(), auto_merge=True)
		assert verdict.should_merge is False

	def test_summary_contains_verdict(self) -> None:
		before = Snapshot(test_total=10, test_passed=8, test_failed=2)
		after = Snapshot(test_total=10, test_passed=10, test_failed=0)
		verdict = review_session(before, after, _session("abc"))
		assert "helped" in verdict.summary
		assert "abc" in verdict.summary
