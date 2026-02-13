"""Parse Claude's stream-json output for token usage and MC_RESULT."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class TokenUsage:
	"""Accumulated token counts from a Claude session."""

	input_tokens: int = 0
	output_tokens: int = 0
	cache_creation_tokens: int = 0
	cache_read_tokens: int = 0


@dataclass
class StreamJsonResult:
	"""Parsed result from Claude's stream-json output format."""

	usage: TokenUsage = field(default_factory=TokenUsage)
	text_content: str = ""
	mc_result: dict[str, object] | None = None


def parse_stream_json(output: str) -> StreamJsonResult:
	"""Parse NDJSON stream-json output from Claude CLI.

	Each line is a JSON object. For type=="assistant", accumulates
	message.usage tokens and concatenates content[].text.
	Then extracts MC_RESULT from the concatenated text.

	Args:
		output: Raw stdout from `claude -p --output-format stream-json`.

	Returns:
		StreamJsonResult with accumulated tokens, text, and parsed MC_RESULT.
	"""
	result = StreamJsonResult()
	if not output or not output.strip():
		return result

	texts: list[str] = []
	usage = TokenUsage()

	for line in output.splitlines():
		line = line.strip()
		if not line:
			continue
		try:
			event = json.loads(line)
		except (json.JSONDecodeError, ValueError):
			continue

		if not isinstance(event, dict):
			continue

		event_type = event.get("type", "")

		if event_type == "result":
			# Final result message has top-level usage
			msg_usage = event.get("usage", {})
			if isinstance(msg_usage, dict):
				usage.input_tokens += int(msg_usage.get("input_tokens", 0))
				usage.output_tokens += int(msg_usage.get("output_tokens", 0))
				usage.cache_creation_tokens += int(
					msg_usage.get("cache_creation_input_tokens", 0)
				)
				usage.cache_read_tokens += int(
					msg_usage.get("cache_read_input_tokens", 0)
				)
			# Extract text from result content
			for block in event.get("content", []):
				if isinstance(block, dict) and block.get("type") == "text":
					texts.append(str(block.get("text", "")))

		elif event_type == "assistant":
			# Assistant message with usage and content
			msg = event.get("message", event)
			msg_usage = msg.get("usage", {})
			if isinstance(msg_usage, dict):
				usage.input_tokens += int(msg_usage.get("input_tokens", 0))
				usage.output_tokens += int(msg_usage.get("output_tokens", 0))
				usage.cache_creation_tokens += int(
					msg_usage.get("cache_creation_input_tokens", 0)
				)
				usage.cache_read_tokens += int(
					msg_usage.get("cache_read_input_tokens", 0)
				)
			for block in msg.get("content", []):
				if isinstance(block, dict) and block.get("type") == "text":
					texts.append(str(block.get("text", "")))

		elif event_type == "content_block_delta":
			delta = event.get("delta", {})
			if isinstance(delta, dict) and delta.get("type") == "text_delta":
				texts.append(str(delta.get("text", "")))

	result.usage = usage
	result.text_content = "".join(texts)

	# Extract MC_RESULT from concatenated text
	from mission_control.session import parse_mc_result

	mc = parse_mc_result(result.text_content)
	result.mc_result = mc

	return result


def compute_token_cost(usage: TokenUsage, pricing: object) -> float:
	"""Compute USD cost from token usage and pricing config.

	Args:
		usage: Token counts from a session.
		pricing: PricingConfig with per-million rates.

	Returns:
		Total cost in USD.
	"""
	cost = 0.0
	cost += usage.input_tokens * getattr(pricing, "input_per_million", 3.0) / 1_000_000
	cost += usage.output_tokens * getattr(pricing, "output_per_million", 15.0) / 1_000_000
	cost += usage.cache_creation_tokens * getattr(pricing, "cache_write_per_million", 3.75) / 1_000_000
	cost += usage.cache_read_tokens * getattr(pricing, "cache_read_per_million", 0.30) / 1_000_000
	return cost
