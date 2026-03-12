"""Auto-update pipeline: bridges intel proposals to swarm missions.

Scans for improvement proposals via the intelligence subsystem, filters
already-applied ones, classifies risk, and either auto-launches low-risk
proposals as swarm missions or gates high-risk ones via Telegram approval.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from autodev.config import MissionConfig
from autodev.db import Database
from autodev.intelligence.models import AdaptationProposal
from autodev.intelligence.scanner import run_scan
from autodev.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


@dataclass
class UpdateResult:
	"""Result of a single proposal processing."""

	proposal_id: str
	title: str
	risk_level: str
	action: str  # "launched", "approved", "rejected", "skipped", "dry_run"
	mission_id: str = ""


class AutoUpdatePipeline:
	"""Bridge intel proposals to swarm missions."""

	def __init__(self, config: MissionConfig, db: Database) -> None:
		self._config = config
		self._db = db

	async def run(
		self,
		dry_run: bool = False,
		approve_all: bool = False,
		threshold: float = 0.3,
	) -> list[UpdateResult]:
		"""Full pipeline: scan -> evaluate -> filter -> approve -> launch.

		Args:
			dry_run: Show proposals without launching missions.
			approve_all: Skip Telegram approval for high-risk proposals.
			threshold: Relevance threshold for proposal generation.

		Returns:
			List of UpdateResult for each processed proposal.
		"""
		report = await run_scan(threshold=threshold)
		logger.info(
			"Intel scan complete: %d findings, %d proposals",
			len(report.findings), len(report.proposals),
		)

		# Filter by title (IDs are regenerated each scan, but titles are stable)
		proposals = [
			p for p in report.proposals
			if not self._is_already_applied(p.title)
		]
		if not proposals:
			logger.info("No new proposals to process")
			return []

		logger.info("%d new proposals to process", len(proposals))

		results: list[UpdateResult] = []
		for proposal in proposals:
			result = await self._process_proposal(
				proposal,
				dry_run=dry_run,
				approve_all=approve_all,
			)
			results.append(result)

		return results

	async def _process_proposal(
		self,
		proposal: AdaptationProposal,
		dry_run: bool = False,
		approve_all: bool = False,
	) -> UpdateResult:
		"""Process a single proposal based on its risk level."""
		if dry_run:
			logger.info("DRY RUN: %s (risk=%s)", proposal.title, proposal.risk_level)
			return UpdateResult(
				proposal_id=proposal.id,
				title=proposal.title,
				risk_level=proposal.risk_level,
				action="dry_run",
			)

		if proposal.risk_level == "low":
			return await self._auto_launch(proposal)

		# High-risk: require approval
		if approve_all:
			return await self._auto_launch(proposal)

		return await self._request_approval(proposal)

	async def _auto_launch(self, proposal: AdaptationProposal) -> UpdateResult:
		"""Auto-launch a low-risk proposal as a swarm mission."""
		objective = self._generate_objective(proposal)
		mission_id = self._record_applied(proposal, objective)
		logger.info("Launched mission %s for proposal: %s", mission_id, proposal.title)

		return UpdateResult(
			proposal_id=proposal.id,
			title=proposal.title,
			risk_level=proposal.risk_level,
			action="launched",
			mission_id=mission_id,
		)

	async def _request_approval(self, proposal: AdaptationProposal) -> UpdateResult:
		"""Send a high-risk proposal to Telegram for approval."""
		tg_config = self._config.notifications.telegram
		if not tg_config.bot_token or not tg_config.chat_id:
			logger.warning(
				"Telegram not configured, skipping high-risk proposal: %s",
				proposal.title,
			)
			return UpdateResult(
				proposal_id=proposal.id,
				title=proposal.title,
				risk_level=proposal.risk_level,
				action="skipped",
			)

		notifier = TelegramNotifier(tg_config.bot_token, tg_config.chat_id)
		try:
			description = (
				f"Auto-Update Proposal (HIGH RISK)\n\n"
				f"Title: {proposal.title}\n"
				f"Type: {proposal.proposal_type}\n"
				f"Priority: {proposal.priority}\n"
				f"Effort: {proposal.effort_estimate}\n"
				f"Target modules: {', '.join(proposal.target_modules)}\n\n"
				f"{proposal.description}"
			)
			approved = await notifier.request_approval(description)
		finally:
			await notifier.close()

		if approved:
			objective = self._generate_objective(proposal)
			mission_id = self._record_applied(proposal, objective)
			logger.info("Approved and launched mission %s: %s", mission_id, proposal.title)
			return UpdateResult(
				proposal_id=proposal.id,
				title=proposal.title,
				risk_level=proposal.risk_level,
				action="approved",
				mission_id=mission_id,
			)

		logger.info("Proposal rejected via Telegram: %s", proposal.title)
		return UpdateResult(
			proposal_id=proposal.id,
			title=proposal.title,
			risk_level=proposal.risk_level,
			action="rejected",
		)

	def _generate_objective(self, proposal: AdaptationProposal) -> str:
		"""Convert a proposal into a swarm mission objective."""
		modules = ", ".join(proposal.target_modules) if proposal.target_modules else "TBD"
		return (
			f"[AUTO-UPDATE] {proposal.title}. "
			f"{proposal.description} "
			f"Target modules: {modules}. "
			f"Effort: {proposal.effort_estimate}. "
			f"All tests must pass after changes."
		)

	def _is_already_applied(self, title: str) -> bool:
		"""Check if a proposal with this title was already applied."""
		return self._db.is_proposal_applied(title)

	def _record_applied(self, proposal: AdaptationProposal, objective: str) -> str:
		"""Record a proposal as applied and return the mission_id."""
		from autodev.models import _new_id

		mission_id = _new_id()
		self._db.record_applied_proposal(
			proposal_id=proposal.id,
			finding_title=proposal.title,
			mission_id=mission_id,
			status="launched",
			objective=objective,
		)
		return mission_id
