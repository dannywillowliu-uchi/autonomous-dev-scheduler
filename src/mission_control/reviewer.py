"""Algorithmic post-session review -- did the session help or hurt?"""

from __future__ import annotations

from dataclasses import dataclass, field

from mission_control.models import Session, Snapshot, SnapshotDelta
from mission_control.state import compare_snapshots


@dataclass
class ReviewVerdict:
	"""Verdict from reviewing a session's impact."""

	verdict: str  # "helped", "neutral", "hurt"
	should_revert: bool = False
	should_merge: bool = False
	improvements: list[str] = field(default_factory=list)
	regressions: list[str] = field(default_factory=list)
	summary: str = ""


def review_session(
	before: Snapshot,
	after: Snapshot,
	session: Session,
	auto_merge: bool = False,
) -> ReviewVerdict:
	"""Review a session by comparing before/after snapshots.

	Rules:
	- Hurt: test_passed decreased OR test_failed increased OR new security findings -> revert
	- Neutral: no metric changes -> keep branch, don't merge
	- Helped: any metric improved without regressions -> optionally merge

	Test regressions always outweigh lint/type improvements. Safety first.
	"""
	delta = compare_snapshots(before, after)
	improvements: list[str] = []
	regressions: list[str] = []

	# Collect improvements
	if delta.tests_fixed > 0:
		improvements.append(f"{delta.tests_fixed} test(s) fixed")
	if delta.lint_delta < 0:
		improvements.append(f"{abs(delta.lint_delta)} lint error(s) fixed")
	if delta.type_delta < 0:
		improvements.append(f"{abs(delta.type_delta)} type error(s) fixed")
	if delta.security_delta < 0:
		improvements.append(f"{abs(delta.security_delta)} security finding(s) resolved")
	if delta.tests_added > 0 and delta.tests_broken == 0:
		improvements.append(f"{delta.tests_added} test(s) added")

	# Collect regressions
	if delta.tests_broken > 0:
		regressions.append(f"{delta.tests_broken} test(s) broken")
	if delta.security_delta > 0:
		regressions.append(f"{delta.security_delta} new security finding(s)")

	# Determine verdict
	if delta.regressed:
		return ReviewVerdict(
			verdict="hurt",
			should_revert=True,
			should_merge=False,
			improvements=improvements,
			regressions=regressions,
			summary=_build_summary("hurt", session, delta, improvements, regressions),
		)

	if delta.improved:
		return ReviewVerdict(
			verdict="helped",
			should_revert=False,
			should_merge=auto_merge,
			improvements=improvements,
			regressions=regressions,
			summary=_build_summary("helped", session, delta, improvements, regressions),
		)

	return ReviewVerdict(
		verdict="neutral",
		should_revert=False,
		should_merge=False,
		improvements=improvements,
		regressions=regressions,
		summary=_build_summary("neutral", session, delta, improvements, regressions),
	)


def _build_summary(
	verdict: str,
	session: Session,
	delta: SnapshotDelta,
	improvements: list[str],
	regressions: list[str],
) -> str:
	parts = [f"Session {session.id}: {verdict}"]
	if improvements:
		parts.append(f"Improvements: {', '.join(improvements)}")
	if regressions:
		parts.append(f"Regressions: {', '.join(regressions)}")
	return ". ".join(parts)
