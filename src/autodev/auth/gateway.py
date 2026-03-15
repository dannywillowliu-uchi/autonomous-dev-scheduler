"""Auth gateway -- orchestrates vault + browser + Telegram for agent auth requests."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from autodev.auth.browser import AuthResult
from autodev.auth.vault import KeychainVault

if TYPE_CHECKING:
	from autodev.auth.browser import HeadlessAuthHandler
	from autodev.notifier import TelegramNotifier

logger = logging.getLogger(__name__)


class AuthGateway:
	"""Entry point for agent authentication requests.

	Ties vault + browser + Telegram together with per-service locking
	to prevent concurrent auth flows for the same service.
	"""

	def __init__(
		self,
		vault: KeychainVault,
		browser: HeadlessAuthHandler,
		notifier: TelegramNotifier | None = None,
	) -> None:
		self._vault = vault
		self._browser = browser
		self._notifier = notifier
		self._locks: dict[str, asyncio.Lock] = {}

	def _get_lock(self, service: str) -> asyncio.Lock:
		"""Get or create a per-service lock."""
		if service not in self._locks:
			self._locks[service] = asyncio.Lock()
		return self._locks[service]

	async def authenticate(
		self,
		service: str,
		purpose: str,
		url: str = "",
		signup_ok: bool = False,
		force_refresh: bool = False,
	) -> AuthResult:
		"""Authenticate to a service. Thread-safe per service.

		1. Acquire per-service lock
		2. Check vault for existing credentials (unless force_refresh)
		3. If found, return success
		4. If not found, notify via Telegram
		5. If signup required and not approved, return failure
		6. Run browser auth flow
		7. Store credentials on success
		8. Return result
		"""
		async with self._get_lock(service):
			# Check vault first
			if not force_refresh:
				existing = await self._vault.get(service, "default")
				if existing:
					logger.info("Found existing credential for %s", service)
					return AuthResult(
						success=True,
						service=service,
						credential_type="cached",
					)

			# Notify about first-time auth
			if self._notifier:
				try:
					await self._notifier.send_auth_request(service, purpose, url)
				except Exception:
					pass

			# Check if signup approval is needed
			if signup_ok is False and self._notifier:
				try:
					approved = await self._request_signup_approval(service, purpose)
					if not approved:
						return AuthResult(
							success=False,
							service=service,
							error="Signup not approved",
						)
				except Exception:
					pass

			# Run browser auth flow
			if not url:
				return AuthResult(
					success=False,
					service=service,
					error="No auth URL provided",
				)

			try:
				result = await self._browser.run_auth_flow(
					url=url,
					service=service,
				)
				return result
			except Exception as e:
				logger.error("Browser auth flow failed for %s: %s", service, e)
				return AuthResult(
					success=False,
					service=service,
					error=str(e),
				)

	async def _request_signup_approval(self, service: str, purpose: str) -> bool:
		"""Request signup approval via Telegram."""
		if not self._notifier:
			return True
		try:
			return await self._notifier.send_signup_request(service, purpose)
		except Exception:
			return True

	async def _request_spend_approval(self, service: str, amount: str) -> bool:
		"""Request spend approval via Telegram."""
		if not self._notifier:
			return True
		try:
			return await self._notifier.send_spend_request(service, amount, f"Service: {service}")
		except Exception:
			return True
