# LLM Evaluator + Auth Gateway Design

## Overview

Replace the keyword-based intelligence evaluator with an LLM-driven evaluator steered by a `program.md` document (Karpathy autoresearch pattern). Add an auth gateway module so agents can authenticate to new services via headless browser automation with Telegram fallback.

## Problem Statement

The current keyword evaluator (`evaluator.py`) scores findings against hardcoded keyword lists like "mcp", "swarm", "agent orchestration". This misses practical tooling integrations (Google Workspace CLI, new SDKs, developer tools) that would make the agent stack more capable. Additionally, agents have no way to authenticate to new services they discover, meaning any integration requiring auth is dead on arrival.

## Goals

1. LLM evaluator makes binary integrate/skip decisions with full context about autodev's architecture and Danny's priorities
2. Auth gateway enables agents to complete OAuth flows, CLI logins, and API key setup with minimal human intervention
3. System should be aggressive on high-value integrations, conservative on marginal ones
4. Danny stays in the loop for signups and spend via Telegram

## Non-Goals

- Complex user profiling from email/social media (scrapped in favor of program.md steering)
- Score-based evaluation (binary decisions only)
- Replacing the existing ratchet/oracle safety rails

---

## Part 1: program.md Steering Document

### File: `docs/program.md`

A markdown document read by the LLM evaluator on every scan cycle. It steers the LLM's judgment about what's worth integrating. This file is human-edited only. Auto-generated context (architecture, git log) is assembled in-memory at evaluation time and never written back to this file.

### Structure

```markdown
# autodev Program

## Identity
Autonomous development framework. Spawns parallel Claude Code agents in swarm mode.
Substrate: Claude Code CLI with --permission-mode auto.

## Danny's Goals
Building agent systems to tackle problems like agentic GPU kernel optimization
and building tools for frontier labs. Expanding what agents can autonomously do
is the priority.

## What To Integrate
- New CLIs, SDKs, APIs that agents can use as tools
- MCP servers that add capabilities (browser, database, cloud services)
- Scheduling, orchestration, coordination patterns
- Auth and credential management improvements
- Anything that expands the surface area of what agents can do autonomously
- Developer tooling that improves agent output quality
- Monitoring, observability, debugging tools for agent systems

## What To Skip
- Game engines, mobile-only frameworks, frontend-only libraries
- Academic papers with no practical code or implementation
- Things autodev already has (check architecture section)
- Marginal improvements to existing capabilities
- Tools that only work with non-Claude LLMs

## Resource Context
Danny is on the Claude Max plan. Swarm runs cost compute but not API dollars.
Be aggressive on high-value integrations. The cost of missing something useful
is higher than the cost of trying something that doesn't work out (ratchet
handles rollback).
```

---

## Part 2: LLM Evaluator Module

### File: `src/autodev/intelligence/llm_evaluator.py`

Replaces `evaluator.py`'s `evaluate_findings()` and `generate_proposals()` with a single LLM call.

### Interface

```python
async def evaluate_findings(
	findings: list[Finding],
	project_path: Path,
	program_path: Path | None = None,
) -> list[AdaptationProposal]:
	"""Evaluate findings using LLM judgment guided by program.md.

	Args:
		findings: Raw findings from all scanners.
		project_path: Path to the autodev project root.
		program_path: Path to program.md (defaults to project_path/docs/program.md).

	Returns:
		List of AdaptationProposal for findings the LLM decided to integrate.
	"""
```

### Implementation

1. Read `program.md` from disk (human-edited sections only)
2. Build enriched context in-memory:
   a. Read CLAUDE.md Architecture section, append as "Current Architecture"
   b. Run `git log --oneline -20`, append as "Recent Activity"
3. Send all findings in a single LLM call (no batching; Claude Max has generous context). Only batch as a fallback if context is genuinely exceeded.
4. Use `_find_claude_binary()` from `spec_generator.py` (shared utility, factored to `src/autodev/intelligence/utils.py`). Use `--print` mode deliberately: the evaluator needs no tool access, just text in/text out.
5. Parse JSON response with robust extraction: strip markdown fences, extract first `[...]` block before `json.loads()`.
6. Convert "integrate" decisions to `AdaptationProposal` objects.
7. On LLM failure: fall back to `evaluator.evaluate_findings()` + `evaluator.generate_proposals()`.

### Shared Utility

Factor `_find_claude_binary()` out of `spec_generator.py` into `src/autodev/intelligence/utils.py` so both `spec_generator.py` and `llm_evaluator.py` can import it.

### Prompt Structure

```
You are evaluating intelligence findings for an autonomous development system.
Read the program document below, then evaluate each finding.

<program>
{enriched_program_md}
</program>

<findings>
{json_array_of_findings}
</findings>

For each finding, decide: integrate or skip.
Return a JSON array wrapped in ```json fences:
[
  {
    "finding_id": "...",
    "decision": "integrate" | "skip",
    "reasoning": "1-2 sentences",
    "proposed_action": "What to implement",
    "target_modules": ["file1.py", "file2.py"]
  }
]
```

### Fallback

If the LLM call fails (subprocess error, JSON parse error, timeout):
1. Log warning with traceback
2. Call `evaluator.evaluate_findings(findings)` and `evaluator.generate_proposals(findings)`
3. Return keyword-based proposals as before

---

## Part 3: Auth Gateway

Three files in `src/autodev/auth/`.

### File: `src/autodev/auth/vault.py`

macOS Keychain wrapper for credential storage.

```python
class KeychainVault:
	"""Store and retrieve credentials from macOS Keychain."""

	SERVICE_PREFIX = "autodev"

	async def store(self, service: str, account: str, secret: str) -> None:
		"""Store a credential in Keychain.

		Uses stdin to pass the secret (not CLI args) to avoid exposure in ps output.
		Calls: echo {secret} | security add-generic-password -U -s autodev/{service} -a {account} -w
		The -w flag without a value reads the password from stdin.
		"""

	async def get(self, service: str, account: str) -> str | None:
		"""Retrieve a credential from Keychain.

		Calls: security find-generic-password -s autodev/{service} -a {account} -w
		Returns None if not found.
		"""

	async def delete(self, service: str, account: str) -> bool:
		"""Delete a credential from Keychain.

		Calls: security delete-generic-password -s autodev/{service} -a {account}
		"""

	async def list_services(self) -> list[dict[str, str]]:
		"""List all autodev-managed credentials.

		Uses: security find-generic-password -l "autodev" (not dump-keychain,
		which prompts for password under launchd).
		Returns list of {service, account} dicts.
		"""
```

All operations use `asyncio.create_subprocess_exec`. The `store()` method passes secrets via stdin (piped to the subprocess) to prevent them appearing in process lists.

### File: `src/autodev/auth/browser.py`

Headless browser auth handler using Playwright. Playwright is added as an optional dependency (`pip install playwright && playwright install chromium`). The browser is lazily initialized only when an auth flow is actually needed (not on every scan cycle).

```python
class HeadlessAuthHandler:
	"""Handle authentication flows via headless Playwright browser."""

	def __init__(self, vault: KeychainVault, notifier: TelegramNotifier | None = None):
		self._vault = vault
		self._notifier = notifier
		self._browser = None  # Lazily initialized

	async def _ensure_browser(self):
		"""Launch headless Chromium on first use."""
		if self._browser is None:
			from playwright.async_api import async_playwright
			self._pw = await async_playwright().start()
			self._browser = await self._pw.chromium.launch(headless=True)

	async def close(self):
		"""Clean up browser resources."""
		if self._browser:
			await self._browser.close()
			await self._pw.stop()
			self._browser = None

	async def run_auth_flow(
		self,
		url: str,
		service: str,
		flow_type: str = "auto",
		timeout_s: int = 300,
	) -> AuthResult:
		"""Navigate to auth URL and attempt to complete the flow.

		Args:
			url: The auth/login URL to navigate to.
			service: Service name for credential storage.
			flow_type: Hint for auth type. One of:
				"oauth" - OAuth redirect flow (Google, GitHub, etc.)
				"cli_login" - CLI tool that opens browser for auth
				"api_key" - Page where you generate/copy an API key
				"auto" - Detect from page content
			timeout_s: Max seconds for the entire auth flow (default 5 min).

		Returns:
			AuthResult with success status, credential info, and any error.
		"""

	async def _detect_flow_type(self, page) -> str:
		"""Analyze page content to determine auth flow type."""

	async def _handle_oauth(self, page, service) -> AuthResult:
		"""Handle OAuth redirect flow.

		1. Fill in credentials from vault if available
		2. Click authorize/allow buttons
		3. Capture redirect URL with token
		4. Store token in vault
		"""

	async def _handle_stuck(self, page, service, reason) -> AuthResult:
		"""When stuck: screenshot page, send to Telegram, wait for help.

		1. Take screenshot of current page
		2. Send to Telegram: "Auth help needed for {service}: {reason}" + screenshot
		3. Poll for Telegram response (instructions or "done")
		4. If "done": check if auth completed, capture credentials
		5. If instructions: attempt to follow them
		6. Timeout after 1 hour
		"""

	async def _handle_captcha(self, page, service) -> AuthResult:
		"""CAPTCHA detected. Telegram Danny with screenshot."""

	async def _handle_2fa(self, page, service) -> AuthResult:
		"""2FA prompt detected. Telegram Danny with screenshot."""
```

```python
@dataclass
class AuthResult:
	success: bool
	service: str
	credential_type: str = ""  # "oauth_token", "api_key", "session_cookie", "cli_token"
	error: str = ""
	required_human: bool = False
```

Playwright runs in headless Chromium. The handler recognizes common UI patterns:
- Google OAuth: "Sign in with Google" buttons, consent screens
- GitHub OAuth: authorize app screens
- Generic login: username/password fields
- API key pages: copyable token strings
- CLI auth: "paste this code" or "click to authorize" patterns

### File: `src/autodev/auth/gateway.py`

Orchestrator that ties vault + browser + Telegram together. Includes per-service locking to prevent concurrent auth flows for the same service.

```python
class AuthGateway:
	"""Entry point for agent authentication requests."""

	def __init__(
		self,
		vault: KeychainVault,
		browser: HeadlessAuthHandler,
		notifier: TelegramNotifier | None = None,
	):
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

		1. Acquire per-service lock (prevents duplicate flows)
		2. Check vault for existing credentials (unless force_refresh)
		3. If found, return them
		4. If not found, notify via Telegram ("First-time auth to {service}")
		5. If signup required and signup_ok=False, request Telegram approval
		6. Run browser auth flow
		7. Store credentials in vault on success
		8. Release lock
		9. Return result
		"""

	async def _request_signup_approval(self, service: str, purpose: str) -> bool:
		"""Telegram Danny: 'Autodev wants to create an account on {service}. Approve?'"""

	async def _request_spend_approval(self, service: str, amount: str) -> bool:
		"""Telegram Danny: 'Autodev wants to spend {amount} on {service}. Approve?'"""
```

---

## Part 4: Integration Points

### Call chain restructuring

The evaluation responsibility moves from `scanner.py` to `auto_update.py`. This keeps `scanner.py` focused on scanning and lets the caller choose the evaluator.

```python
# scanner.py - returns raw findings only, no evaluation
async def run_scan(threshold: float = 0.3) -> IntelReport:
	# ... scan all sources ...
	return IntelReport(findings=all_findings, proposals=[], ...)

# auto_update.py - chooses evaluator
async def run(self, ...):
	report = await run_scan()
	if self._config.intelligence.evaluator_mode == "llm":
		from autodev.intelligence.llm_evaluator import evaluate_findings as llm_evaluate
		proposals = await llm_evaluate(report.findings, self._project_path)
	else:
		from autodev.intelligence.evaluator import evaluate_findings, generate_proposals
		evaluated = evaluate_findings(report.findings)
		proposals = generate_proposals(evaluated, threshold)
	# ... process proposals ...
```

### config.py changes

Add `IntelligenceConfig` dataclass:

```python
@dataclass
class IntelligenceConfig:
	evaluator_mode: str = "llm"  # "llm" or "keyword"
```

Add `intelligence: IntelligenceConfig` field to `MissionConfig`, loaded from `[intelligence]` TOML section.

### auto_update.py changes

- Remove hardcoded `max_daily_modifications` parameter and `_check_rate_limit()` calls
- LLM evaluator decides volume based on program.md context
- Keep a safety cap of 15 as a configurable escape hatch (`max_daily_modifications` in config, default 15, set to 0 to disable)

### Worker auth via inbox protocol (not import)

Workers cannot import and call the auth gateway directly (they are separate Claude Code subprocesses). Instead, auth requests flow through the existing inbox message-passing architecture:

```
Worker hits auth wall
  -> writes to team-lead.json:
     {"from": "worker-name", "type": "auth_request",
      "service": "google-workspace", "url": "https://...", "purpose": "CLI access"}
  -> Worker waits, polling its own inbox for response

Planner sees auth_request in next cycle
  -> Planner calls controller.handle_auth_request(service, url, purpose)

Controller delegates to AuthGateway
  -> AuthGateway checks vault, runs browser flow if needed, stores result

Controller writes to worker's inbox:
  {"type": "auth_response", "service": "google-workspace",
   "credential_type": "oauth_token", "success": true,
   "instructions": "Token stored in Keychain as autodev/google-workspace. Use: ..."}

Worker reads inbox, gets credential, continues
```

### swarm/controller.py changes

Add `handle_auth_request()` method that instantiates the auth gateway and processes the request. Add `"auth_request"` to the message types the planner recognizes.

### swarm/worker_prompt.py changes

Add auth request instructions to worker prompts:

```
## Authentication

If you encounter an auth wall (OAuth screen, API key required, CLI login needed),
send an auth request to the planner via the team inbox:

1. Write to team-lead.json:
   {"from": "<your-name>", "type": "auth_request",
    "service": "service-name", "url": "https://auth-url", "purpose": "why you need access"}
2. Poll your inbox for a response with "type": "auth_response"
3. The gateway will handle OAuth flows, credential storage, and ask Danny for help if stuck
4. Credentials are stored in macOS Keychain for future use
```

### notifier.py changes

New notification methods:

```python
async def send_auth_request(self, service: str, purpose: str) -> None:
	"""Notify: first-time auth to a service."""

async def send_auth_help(self, service: str, reason: str, screenshot_path: str) -> None:
	"""Send screenshot + help request for stuck auth flow."""

async def send_signup_request(self, service: str, purpose: str) -> bool:
	"""Request approval for new account signup. Returns True if approved."""

async def send_spend_request(self, service: str, amount: str) -> bool:
	"""Request approval for spend. Returns True if approved."""
```

---

## Part 5: Testing

### llm_evaluator tests
- Mock `claude --print` subprocess, verify JSON parsing
- Test robust JSON extraction (markdown fences, preamble text, bare JSON)
- Verify fallback to keyword evaluator on LLM failure
- Verify in-memory context enrichment (architecture + git log injection)
- Test with large finding set (verify no batching under normal conditions)

### vault tests
- Mock `security` CLI calls
- Test store passes secret via stdin (not CLI args)
- Test get/delete/list operations
- Test credential not found returns None
- Test service prefix namespacing

### browser tests
- Mock Playwright page interactions
- Test OAuth flow detection
- Test stuck/CAPTCHA/2FA Telegram fallback triggers
- Test auth result capture
- Test lazy browser initialization
- Test timeout enforcement

### gateway tests
- Test vault hit (existing credential, no browser needed)
- Test vault miss -> browser flow -> vault store
- Test per-service locking (concurrent requests for same service)
- Test signup approval Telegram flow
- Test spend approval Telegram flow
- Test force_refresh bypasses vault

### controller auth tests
- Test handle_auth_request reads inbox, delegates to gateway
- Test auth_response written to worker inbox
- Test planner recognizes auth_request message type

### Integration
- All existing tests must continue to pass
- `evaluator.py` kept as fallback, its tests unchanged

---

## Implementation Priorities

1. **Shared utility**: Factor `_find_claude_binary()` into `src/autodev/intelligence/utils.py`
2. **program.md**: Write the steering document
3. **llm_evaluator.py**: LLM-based evaluate_findings with program.md
4. **config.py + scanner.py + auto_update.py**: Wire LLM evaluator into pipeline (IntelligenceConfig, call chain restructure, safety cap)
5. **vault.py**: Keychain wrapper with stdin secret passing
6. **browser.py**: Headless Playwright auth handler with lazy init
7. **gateway.py**: Orchestrator with per-service locking
8. **controller.py + worker_prompt.py**: Inbox-based auth request/response protocol
9. **notifier.py**: Auth notification methods
10. **Tests**: All of the above
