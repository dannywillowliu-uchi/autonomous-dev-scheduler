"""Tests for tiered auth handler."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from autodev.auth.browser import (
	AuthHandler,
	AuthResult,
	HeadlessAuthHandler,
)


@pytest.fixture
def mock_vault():
	vault = AsyncMock()
	vault.store = AsyncMock()
	vault.get = AsyncMock(return_value=None)
	return vault


@pytest.fixture
def mock_notifier():
	notifier = AsyncMock()
	notifier.send_auth_help = AsyncMock()
	notifier.send = AsyncMock()
	return notifier


@pytest.fixture
def handler(mock_vault, mock_notifier):
	return AuthHandler(vault=mock_vault, notifier=mock_notifier)


# --- AuthResult tests ---


class TestAuthResult:
	def test_success_result(self):
		r = AuthResult(success=True, service="github", credential_type="oauth_token")
		assert r.success is True
		assert r.service == "github"
		assert r.credential_type == "oauth_token"
		assert r.error == ""
		assert r.required_human is False

	def test_failure_result(self):
		r = AuthResult(success=False, service="gitlab", error="timeout")
		assert r.success is False
		assert r.error == "timeout"

	def test_human_required(self):
		r = AuthResult(success=False, service="x", required_human=True, error="CAPTCHA")
		assert r.required_human is True

	def test_instructions_field(self):
		r = AuthResult(success=True, service="gcp", instructions="Token from $GH_TOKEN")
		assert r.instructions == "Token from $GH_TOKEN"


class TestBackwardCompat:
	def test_headless_auth_handler_alias(self):
		assert HeadlessAuthHandler is AuthHandler


# --- Tier 1: Environment variables ---


class TestTryEnvVar:
	@pytest.mark.asyncio
	async def test_finds_github_token(self, handler, mock_vault):
		with patch.dict(os.environ, {"GH_TOKEN": "ghp_test123"}):
			result = await handler._try_env_var("github")
		assert result.success is True
		assert result.credential_type == "env_var"
		mock_vault.store.assert_called_once_with("github", "env_token", "ghp_test123")

	@pytest.mark.asyncio
	async def test_finds_second_env_var(self, handler, mock_vault):
		with patch.dict(os.environ, {"GITHUB_TOKEN": "ghp_fallback"}, clear=False):
			# Remove GH_TOKEN if it exists
			env = {k: v for k, v in os.environ.items() if k != "GH_TOKEN"}
			env["GITHUB_TOKEN"] = "ghp_fallback"
			with patch.dict(os.environ, env, clear=True):
				result = await handler._try_env_var("github")
		assert result.success is True

	@pytest.mark.asyncio
	async def test_no_env_var_returns_failure(self, handler):
		with patch.dict(os.environ, {}, clear=True):
			result = await handler._try_env_var("github")
		assert result.success is False

	@pytest.mark.asyncio
	async def test_unknown_service_returns_failure(self, handler):
		result = await handler._try_env_var("unknown-service")
		assert result.success is False


# --- Tier 2: Service accounts ---


class TestTryServiceAccount:
	@pytest.mark.asyncio
	async def test_non_google_service_skipped(self, handler):
		result = await handler._try_service_account("github")
		assert result.success is False

	@pytest.mark.asyncio
	async def test_google_creds_env_var(self, handler, tmp_path):
		creds_file = tmp_path / "sa.json"
		creds_file.write_text("{}")
		with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": str(creds_file)}):
			result = await handler._try_service_account("google-cloud")
		assert result.success is True
		assert result.credential_type == "service_account"

	@pytest.mark.asyncio
	async def test_google_creds_missing_file(self, handler):
		with patch.dict(os.environ, {"GOOGLE_APPLICATION_CREDENTIALS": "/nonexistent/sa.json"}):
			result = await handler._try_service_account("google-cloud")
		assert result.success is False

	@pytest.mark.asyncio
	async def test_google_default_creds_path(self, handler):
		with (
			patch.dict(os.environ, {}, clear=True),
			patch("autodev.auth.browser.Path.home") as mock_home,
		):
			mock_home.return_value = MagicMock()
			# Simulate no files existing
			adc = mock_home.return_value / ".config" / "gcloud" / "application_default_credentials.json"
			adc.exists.return_value = False
			cdb = mock_home.return_value / ".config" / "gcloud" / "credentials.db"
			cdb.exists.return_value = False
			result = await handler._try_service_account("google-cloud")
		assert result.success is False


# --- Tier 3: Device code flow ---


class TestTryDeviceCode:
	@pytest.mark.asyncio
	async def test_github_already_authed(self, handler):
		mock_proc = AsyncMock()
		mock_proc.returncode = 0
		mock_proc.communicate = AsyncMock(return_value=(b"", b""))
		with (
			patch("shutil.which", return_value="/usr/bin/gh"),
			patch("asyncio.create_subprocess_exec", return_value=mock_proc),
		):
			result = await handler._try_device_code("github", "test")
		assert result.success is True
		assert result.credential_type == "cli_auth"

	@pytest.mark.asyncio
	async def test_github_not_authed_telegrams(self, handler, mock_notifier):
		mock_proc = AsyncMock()
		mock_proc.returncode = 1
		mock_proc.communicate = AsyncMock(return_value=(b"", b"not logged in"))
		with (
			patch("shutil.which", return_value="/usr/bin/gh"),
			patch("asyncio.create_subprocess_exec", return_value=mock_proc),
		):
			result = await handler._try_device_code("github", "need repos")
		assert result.success is False
		assert result.required_human is True
		mock_notifier.send.assert_called_once()

	@pytest.mark.asyncio
	async def test_non_github_skipped(self, handler):
		result = await handler._try_device_code("vercel", "deploy")
		assert result.success is False

	@pytest.mark.asyncio
	async def test_github_no_gh_cli(self, handler):
		with patch("shutil.which", return_value=None):
			result = await handler._try_device_code("github", "test")
		assert result.success is False


# --- Tier 4: Console flow ---


class TestTryConsoleFlow:
	@pytest.mark.asyncio
	async def test_gcloud_already_authed(self, handler):
		mock_proc = AsyncMock()
		mock_proc.returncode = 0
		mock_proc.communicate = AsyncMock(return_value=(
			b'[{"account": "danny@example.com", "status": "ACTIVE"}]', b"",
		))
		with (
			patch("shutil.which", return_value="/usr/bin/gcloud"),
			patch("asyncio.create_subprocess_exec", return_value=mock_proc),
		):
			result = await handler._try_console_flow("google-cloud", "workspace", "")
		assert result.success is True
		assert "danny@example.com" in result.instructions

	@pytest.mark.asyncio
	async def test_unknown_service_skipped(self, handler):
		result = await handler._try_console_flow("random-saas", "test", "")
		assert result.success is False

	@pytest.mark.asyncio
	async def test_cli_not_installed(self, handler):
		with patch("shutil.which", return_value=None):
			result = await handler._try_console_flow("github", "test", "")
		assert result.success is False


# --- Tier 5: Playwright (kept from original tests) ---


def _make_mock_page(content: str = "<html></html>", url: str = "https://example.com"):
	"""Create a mock Playwright page with configurable content and URL."""
	page = AsyncMock()
	page.content = AsyncMock(return_value=content)
	page.url = url
	page.goto = AsyncMock()
	page.close = AsyncMock()
	page.screenshot = AsyncMock(return_value=b"fake-png-bytes")
	page.wait_for_load_state = AsyncMock()

	locator = AsyncMock()
	locator.first = locator
	locator.is_visible = AsyncMock(return_value=False)
	locator.count = AsyncMock(return_value=0)
	locator.nth = MagicMock(return_value=locator)
	locator.text_content = AsyncMock(return_value="")
	locator.get_attribute = AsyncMock(return_value=None)
	page.locator = MagicMock(return_value=locator)

	return page


class TestDetectFlowType:
	@pytest.mark.asyncio
	async def test_detect_oauth(self, handler):
		page = _make_mock_page(content="<html><body>Sign in with Google</body></html>")
		result = await handler._detect_flow_type(page)
		assert result == "oauth"

	@pytest.mark.asyncio
	async def test_detect_api_key(self, handler):
		page = _make_mock_page(content="<html><body>Your API Key: abc123</body></html>")
		result = await handler._detect_flow_type(page)
		assert result == "api_key"

	@pytest.mark.asyncio
	async def test_detect_cli_login(self, handler):
		page = _make_mock_page(content="<html><body>Paste this code into your CLI</body></html>")
		result = await handler._detect_flow_type(page)
		assert result == "cli_login"

	@pytest.mark.asyncio
	async def test_detect_captcha(self, handler):
		page = _make_mock_page(content="<html><body>Please complete the CAPTCHA</body></html>")
		result = await handler._detect_flow_type(page)
		assert result == "captcha"

	@pytest.mark.asyncio
	async def test_detect_2fa(self, handler):
		page = _make_mock_page(content="<html><body>Enter your two-factor authentication code</body></html>")
		result = await handler._detect_flow_type(page)
		assert result == "2fa"

	@pytest.mark.asyncio
	async def test_detect_unknown(self, handler):
		page = _make_mock_page(content="<html><body>Nothing relevant here</body></html>")
		result = await handler._detect_flow_type(page)
		assert result == "unknown"

	@pytest.mark.asyncio
	async def test_captcha_takes_priority(self, handler):
		page = _make_mock_page(content="<html>Sign in with Google<div>CAPTCHA</div></html>")
		result = await handler._detect_flow_type(page)
		assert result == "captcha"


class TestHandleStuck:
	@pytest.mark.asyncio
	async def test_stuck_sends_screenshot(self, handler, mock_notifier):
		page = _make_mock_page()
		result = await handler._handle_stuck(page, "test-service", "something went wrong")
		assert result.success is False
		assert result.required_human is True
		mock_notifier.send_auth_help.assert_called_once()

	@pytest.mark.asyncio
	async def test_stuck_without_notifier(self, mock_vault):
		handler = AuthHandler(vault=mock_vault, notifier=None)
		page = _make_mock_page()
		result = await handler._handle_stuck(page, "test-service", "no notifier")
		assert result.success is False
		assert result.required_human is True


class TestHandleOAuth:
	@pytest.mark.asyncio
	async def test_oauth_captures_token(self, handler, mock_vault):
		page = _make_mock_page(url="https://callback.example.com?code=abc123&state=xyz")
		locator = AsyncMock()
		locator.first = locator
		locator.is_visible = AsyncMock(return_value=False)
		page.locator = MagicMock(return_value=locator)

		result = await handler._handle_oauth(page, "github")
		assert result.success is True
		assert result.credential_type == "oauth_token"
		mock_vault.store.assert_called_once_with("github", "oauth_token", "abc123")

	@pytest.mark.asyncio
	async def test_oauth_stuck_when_no_token(self, handler, mock_notifier):
		page = _make_mock_page(url="https://example.com/login")
		locator = AsyncMock()
		locator.first = locator
		locator.is_visible = AsyncMock(return_value=False)
		page.locator = MagicMock(return_value=locator)

		result = await handler._handle_oauth(page, "github")
		assert result.success is False
		assert result.required_human is True


class TestHandleApiKey:
	@pytest.mark.asyncio
	async def test_api_key_found(self, handler, mock_vault):
		page = _make_mock_page()
		fake_key = "sk-abcdefghij1234567890abcdef"

		locator = AsyncMock()
		locator.count = AsyncMock(return_value=1)
		el = AsyncMock()
		el.text_content = AsyncMock(return_value=fake_key)
		el.get_attribute = AsyncMock(return_value=None)
		locator.nth = MagicMock(return_value=el)

		call_count = 0

		def mock_locator(selector):
			nonlocal call_count
			call_count += 1
			if call_count == 1:
				return locator
			empty = AsyncMock()
			empty.count = AsyncMock(return_value=0)
			return empty

		page.locator = MagicMock(side_effect=mock_locator)

		result = await handler._handle_api_key(page, "openai")
		assert result.success is True
		assert result.credential_type == "api_key"
		mock_vault.store.assert_called_once_with("openai", "api_key", fake_key)


class TestHandleCliLogin:
	@pytest.mark.asyncio
	async def test_cli_code_found(self, handler, mock_vault):
		page = _make_mock_page()

		locator = AsyncMock()
		locator.count = AsyncMock(return_value=1)
		el = AsyncMock()
		el.text_content = AsyncMock(return_value="ABCD-1234")
		locator.nth = MagicMock(return_value=el)

		call_count = 0

		def mock_locator(selector):
			nonlocal call_count
			call_count += 1
			if call_count == 1:
				return locator
			empty = AsyncMock()
			empty.count = AsyncMock(return_value=0)
			return empty

		page.locator = MagicMock(side_effect=mock_locator)

		result = await handler._handle_cli_login(page, "vercel")
		assert result.success is True
		assert result.credential_type == "cli_token"


# --- Tier 6: Telegram manual ---


class TestTelegramManual:
	@pytest.mark.asyncio
	async def test_sends_manual_instructions(self, handler, mock_notifier):
		result = await handler._telegram_manual("random-tool", "need access", "https://tool.com/login")
		assert result.success is False
		assert result.required_human is True
		assert "Manual auth" in result.error
		mock_notifier.send.assert_called_once()
		call_msg = mock_notifier.send.call_args[0][0]
		assert "random-tool" in call_msg
		assert "autodev/random-tool" in call_msg

	@pytest.mark.asyncio
	async def test_manual_without_notifier(self, mock_vault):
		handler = AuthHandler(vault=mock_vault, notifier=None)
		result = await handler._telegram_manual("svc", "test", "")
		assert result.success is False
		assert result.required_human is True


# --- Full authenticate flow ---


class TestAuthenticate:
	@pytest.mark.asyncio
	async def test_env_var_short_circuits(self, handler, mock_vault):
		with patch.dict(os.environ, {"GH_TOKEN": "ghp_test"}):
			result = await handler.authenticate("github", "need repos")
		assert result.success is True
		assert result.credential_type == "env_var"

	@pytest.mark.asyncio
	async def test_falls_through_all_tiers(self, handler, mock_notifier):
		"""Unknown service with no env vars, no CLI, no URL -> manual fallback."""
		with patch.dict(os.environ, {}, clear=True):
			result = await handler.authenticate("obscure-saas", "test", url="")
		assert result.success is False
		assert result.required_human is True
		assert "Manual auth" in result.error

	@pytest.mark.asyncio
	async def test_playwright_tier_with_url(self, handler, mock_vault, mock_notifier):
		"""With a URL and unknown service, tries Playwright then manual."""
		with patch.dict(os.environ, {}, clear=True):
			with patch.object(handler, "_try_playwright", new_callable=AsyncMock) as mock_pw:
				mock_pw.return_value = AuthResult(success=True, service="custom", credential_type="api_key")
				result = await handler.authenticate("custom", "test", url="https://custom.com/keys")
		assert result.success is True
		assert result.credential_type == "api_key"


# --- Close / cleanup ---


class TestClose:
	@pytest.mark.asyncio
	async def test_close_cleans_up(self, mock_vault):
		handler = AuthHandler(vault=mock_vault)
		handler._browser = AsyncMock()
		handler._pw = AsyncMock()
		await handler.close()
		assert handler._browser is None
		assert handler._pw is None

	@pytest.mark.asyncio
	async def test_close_noop_when_not_initialized(self, mock_vault):
		handler = AuthHandler(vault=mock_vault)
		await handler.close()
		assert handler._browser is None


# --- Lazy browser initialization ---


class TestLazyInit:
	@pytest.mark.asyncio
	async def test_browser_not_initialized_on_construction(self, handler):
		assert handler._browser is None
		assert handler._pw is None

	@pytest.mark.asyncio
	async def test_ensure_browser_idempotent(self, mock_vault):
		handler = AuthHandler(vault=mock_vault)
		page = _make_mock_page()

		browser = AsyncMock()
		browser.new_page = AsyncMock(return_value=page)
		pw_instance = AsyncMock()
		pw_instance.chromium.launch = AsyncMock(return_value=browser)
		pw_cm = AsyncMock()
		pw_cm.start = AsyncMock(return_value=pw_instance)

		mock_pw_module = MagicMock()
		mock_pw_module.async_playwright = MagicMock(return_value=pw_cm)
		with patch.dict("sys.modules", {"playwright": MagicMock(), "playwright.async_api": mock_pw_module}):
			await handler._ensure_browser()
			first_browser = handler._browser
			await handler._ensure_browser()
			assert handler._browser is first_browser
