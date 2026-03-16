"""Web source scanners for blogs, GitHub trending, arXiv, and X/Twitter profiles."""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from autodev.intelligence.claude_code import AUTOMATION_KEYWORDS
from autodev.intelligence.models import Finding

logger = logging.getLogger(__name__)


# X/Twitter accounts to monitor via Playwright scraping
X_ACCOUNTS: list[dict[str, str]] = [
	{
		"handle": "bcherny",
		"trust_level": "high",
		"description": "Boris Cherny, creator of Claude Code at Anthropic",
	},
]


@dataclass
class SourceConfig:
	url: str
	source_type: str  # "blog", "trending", "papers"
	trust_level: str  # "high", "medium"


SOURCES: dict[str, SourceConfig] = {
	"anthropic_blog": SourceConfig(
		url="https://www.anthropic.com/news",
		source_type="blog",
		trust_level="high",
	),
	"openai_blog": SourceConfig(
		url="https://openai.com/blog",
		source_type="blog",
		trust_level="high",
	),
	"deepmind_blog": SourceConfig(
		url="https://deepmind.google/discover/blog/",
		source_type="blog",
		trust_level="high",
	),
	"github_trending": SourceConfig(
		url="https://github.com/trending?since=weekly&spoken_language_code=en",
		source_type="trending",
		trust_level="medium",
	),
	"arxiv_agents": SourceConfig(
		url="https://arxiv.org/list/cs.AI/recent",
		source_type="papers",
		trust_level="medium",
	),
}

AGENT_KEYWORDS = [
	"agent", "autonomous", "multi-agent", "agentic", "llm", "tool use",
	"code generation", "reasoning", "planning", "self-improving",
]


def _score_relevance(text: str) -> float:
	"""Score text relevance using AUTOMATION_KEYWORDS from claude_code."""
	lower = text.lower()
	hits = sum(1 for kw in AUTOMATION_KEYWORDS if kw in lower)
	return min(hits / 3.0, 1.0)


class WebSourceScanner:
	"""Scans web sources (blogs, GitHub trending, arXiv) for AI/agent news."""

	async def scan(self, client: httpx.AsyncClient) -> list[Finding]:
		"""Scan all web sources concurrently."""
		tasks = []
		for name, cfg in SOURCES.items():
			if cfg.source_type == "blog":
				tasks.append(self._scan_blog(client, cfg.url, name, cfg.trust_level))
			elif cfg.source_type == "trending":
				tasks.append(self._scan_github_trending(client, cfg.trust_level))
			elif cfg.source_type == "papers":
				tasks.append(self._scan_arxiv(client, cfg.trust_level))

		# X/Twitter profile scanning (sequential, uses Playwright)
		tasks.append(self._scan_x_accounts())

		results = await asyncio.gather(*tasks, return_exceptions=True)
		findings: list[Finding] = []
		for result in results:
			if isinstance(result, BaseException):
				logger.warning("Web source scan failed: %s", result)
				continue
			findings.extend(result)
		return findings

	async def _scan_x_accounts(self) -> list[Finding]:
		"""Scrape X/Twitter profiles using Playwright for recent tweets."""
		if not X_ACCOUNTS:
			return []

		findings: list[Finding] = []
		try:
			from playwright.async_api import async_playwright
		except ImportError:
			logger.warning("Playwright not installed, skipping X account scanning")
			return []

		pw = None
		browser = None
		try:
			pw = await async_playwright().start()
			browser = await pw.chromium.launch(headless=True)

			for account in X_ACCOUNTS:
				handle = account["handle"]
				trust = account.get("trust_level", "high")
				try:
					page = await browser.new_page()
					await page.goto(
						f"https://x.com/{handle}",
						timeout=30000,
						wait_until="networkidle",
					)
					# Wait for tweets to render
					await page.wait_for_timeout(3000)

					# Extract tweets from article elements
					articles = await page.query_selector_all("article")
					for article in articles[:10]:
						try:
							text_el = await article.query_selector(
								"[data-testid='tweetText']"
							)
							if not text_el:
								continue
							text = await text_el.inner_text()
							if not text or len(text) < 10:
								continue

							# Extract tweet URL from time link
							time_link = await article.query_selector("time")
							tweet_url = f"https://x.com/{handle}"
							tweet_date = ""
							if time_link:
								parent = await time_link.evaluate_handle(
									"el => el.closest('a')"
								)
								if parent:
									href = await parent.get_attribute("href")
									if href:
										tweet_url = f"https://x.com{href}"
								tweet_date = await time_link.get_attribute(
									"datetime"
								) or ""

							score = _score_relevance(text)
							findings.append(Finding(
								source=f"x_{handle}",
								title=text[:120],
								url=tweet_url,
								summary=text[:300],
								published_at=tweet_date or datetime.now(
									timezone.utc
								).isoformat(),
								relevance_score=max(score, 0.5),
								trust_level=trust,
							))
						except Exception:
							continue
					await page.close()
				except Exception as exc:
					logger.warning("X scan failed for @%s: %s", handle, exc)
		except Exception as exc:
			logger.warning("Playwright X scanning failed: %s", exc)
		finally:
			if browser:
				await browser.close()
			if pw:
				await pw.stop()

		logger.info("X scan: %d tweets from %d accounts", len(findings), len(X_ACCOUNTS))
		return findings

	async def _scan_blog(
		self, client: httpx.AsyncClient, url: str, source_name: str, trust: str
	) -> list[Finding]:
		"""Fetch a blog page and extract article titles+links."""
		findings: list[Finding] = []
		try:
			resp = await client.get(url, follow_redirects=True)
			resp.raise_for_status()
			html = resp.text

			# Extract <a> tags with href and text content
			links = re.findall(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>([^<]+)</a>', html)
			for href, title in links:
				title = title.strip()
				if len(title) < 10 or len(title) > 300:
					continue

				score = _score_relevance(title)
				if score < 0.1:
					continue

				# Resolve relative URLs
				if href.startswith("/"):
					from urllib.parse import urlparse
					parsed = urlparse(url)
					href = f"{parsed.scheme}://{parsed.netloc}{href}"

				findings.append(Finding(
					source=source_name,
					title=title,
					url=href,
					summary=f"Blog post from {source_name}",
					published_at=datetime.now(timezone.utc).isoformat(),
					relevance_score=score,
					trust_level=trust,
				))
		except httpx.HTTPError as exc:
			logger.warning("Blog scan failed for %s: %s", source_name, exc)

		return findings

	async def _scan_github_trending(
		self, client: httpx.AsyncClient, trust: str
	) -> list[Finding]:
		"""Fetch GitHub trending page and extract repo names+descriptions."""
		findings: list[Finding] = []
		try:
			resp = await client.get(
				SOURCES["github_trending"].url,
				follow_redirects=True,
			)
			resp.raise_for_status()
			html = resp.text

			# GitHub trending uses <h2 class="h3 ..."><a href="/owner/repo">
			repo_links = re.findall(r'<h2[^>]*>\s*<a[^>]+href=["\'](/[^"\']+)["\']', html)
			# Descriptions follow in <p class="...">
			descriptions = re.findall(r'<p class="col-9[^"]*">\s*(.+?)\s*</p>', html, re.DOTALL)

			for i, repo_path in enumerate(repo_links):
				repo_name = repo_path.strip("/")
				desc = descriptions[i].strip() if i < len(descriptions) else ""
				combined = f"{repo_name} {desc}".lower()

				if not any(kw in combined for kw in AGENT_KEYWORDS):
					continue

				score = _score_relevance(f"{repo_name} {desc}")

				findings.append(Finding(
					source="github_trending",
					title=repo_name,
					url=f"https://github.com{repo_path}",
					summary=desc[:200] if desc else f"Trending repo: {repo_name}",
					published_at=datetime.now(timezone.utc).isoformat(),
					relevance_score=max(score, 0.3),
					trust_level=trust,
				))
		except httpx.HTTPError as exc:
			logger.warning("GitHub trending scan failed: %s", exc)

		return findings

	async def _scan_arxiv(
		self, client: httpx.AsyncClient, trust: str
	) -> list[Finding]:
		"""Fetch arXiv cs.AI recent listings and extract paper titles+links."""
		findings: list[Finding] = []
		try:
			resp = await client.get(
				SOURCES["arxiv_agents"].url,
				follow_redirects=True,
			)
			resp.raise_for_status()
			html = resp.text

			# Extract /abs/ links and their neighboring title text
			entries = re.findall(
				r'<a[^>]+href=["\'](/abs/[^"\']+)["\'][^>]*>([^<]*)</a>',
				html,
			)
			# Also try the list-title pattern: <span class="list-title ..."><a href="/abs/...">arXiv:...</a></span>
			# followed by title in <div class="list-title ...">Title: ...</div>
			title_blocks = re.findall(
				r'arXiv:(\d+\.\d+).*?<div[^>]*class="list-title[^"]*"[^>]*>\s*'
				r'(?:<span[^>]*>[^<]*</span>\s*)?(.+?)\s*</div>',
				html,
				re.DOTALL,
			)

			seen_ids: set[str] = set()
			for arxiv_id, title in title_blocks:
				if arxiv_id in seen_ids:
					continue
				seen_ids.add(arxiv_id)
				title = re.sub(r"<[^>]+>", "", title).strip()
				if not title:
					continue

				score = _score_relevance(title)
				findings.append(Finding(
					source="arxiv_agents",
					title=title,
					url=f"https://arxiv.org/abs/{arxiv_id}",
					summary=f"arXiv cs.AI paper: {title[:150]}",
					published_at=datetime.now(timezone.utc).isoformat(),
					relevance_score=score,
					trust_level=trust,
				))

			# Fallback: if title_blocks didn't match, use simpler link extraction
			if not findings:
				for href, link_text in entries:
					link_text = link_text.strip()
					arxiv_id = href.replace("/abs/", "")
					if arxiv_id in seen_ids or not link_text:
						continue
					seen_ids.add(arxiv_id)

					findings.append(Finding(
						source="arxiv_agents",
						title=link_text or f"arXiv paper {arxiv_id}",
						url=f"https://arxiv.org{href}",
						summary="arXiv cs.AI paper",
						published_at=datetime.now(timezone.utc).isoformat(),
						relevance_score=0.3,
						trust_level=trust,
					))
		except httpx.HTTPError as exc:
			logger.warning("arXiv scan failed: %s", exc)

		return findings


async def scan_web_sources(client: httpx.AsyncClient) -> list[Finding]:
	"""Module-level convenience function for source registration."""
	scanner = WebSourceScanner()
	return await scanner.scan(client)
