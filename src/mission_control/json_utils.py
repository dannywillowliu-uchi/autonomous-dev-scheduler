"""Shared JSON extraction utilities for parsing LLM output."""

from __future__ import annotations

import json
import re
from typing import Any


def extract_json_from_text(text: str, expect_array: bool = False) -> Any | None:
	"""Extract a JSON object or array from text that may contain markdown fences or prose.

	Tries in order:
	1. Strip markdown fences (```json ... ``` or ``` ... ```)
	2. Find a bare JSON object/array via brace/bracket matching
	3. Return None if nothing parseable found

	Args:
		text: Raw text that may contain JSON.
		expect_array: If True, look for a JSON array ([...]) instead of object ({...}).

	Returns:
		Parsed JSON (dict or list) or None if extraction failed.
	"""
	if not text or not text.strip():
		return None

	# Step 1: Try to extract from markdown fences
	fence_match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", text, re.DOTALL)
	if fence_match:
		fenced_content = fence_match.group(1).strip()
		try:
			return json.loads(fenced_content)
		except (json.JSONDecodeError, ValueError):
			pass

	# Step 2: Strip any remaining markdown fences from the whole text
	cleaned = re.sub(r"```(?:json)?\s*", "", text)
	cleaned = re.sub(r"```", "", cleaned)
	cleaned = cleaned.strip()

	# Step 3: Try parsing the whole cleaned text
	try:
		return json.loads(cleaned)
	except (json.JSONDecodeError, ValueError):
		pass

	# Step 4: Find a bare JSON object or array
	if expect_array:
		match = re.search(r"(\[[\s\S]*\])", cleaned)
	else:
		match = re.search(r"\{[\s\S]*\}", cleaned, re.DOTALL)

	if match:
		try:
			return json.loads(match.group(0) if not expect_array else match.group(1))
		except (json.JSONDecodeError, ValueError):
			pass

	return None
