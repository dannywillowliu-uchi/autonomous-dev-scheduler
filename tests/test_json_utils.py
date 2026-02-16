"""Tests for shared JSON extraction utilities."""

from __future__ import annotations

import json

from mission_control.json_utils import _find_balanced, extract_json_from_text


class TestExtractJsonFromText:
	def test_plain_json_object(self) -> None:
		raw = json.dumps({"key": "value", "num": 42})
		result = extract_json_from_text(raw)
		assert result == {"key": "value", "num": 42}

	def test_plain_json_array(self) -> None:
		raw = json.dumps([{"a": 1}, {"b": 2}])
		result = extract_json_from_text(raw, expect_array=True)
		assert result == [{"a": 1}, {"b": 2}]

	def test_markdown_fenced_json(self) -> None:
		raw = '```json\n{"score": 0.9, "met": true}\n```'
		result = extract_json_from_text(raw)
		assert result == {"score": 0.9, "met": True}

	def test_json_embedded_in_prose(self) -> None:
		raw = 'Here is my answer:\n{"score": 0.7, "met": false}\nThat is all.'
		result = extract_json_from_text(raw)
		assert result is not None
		assert result["score"] == 0.7

	def test_invalid_json_returns_none(self) -> None:
		result = extract_json_from_text("This is not JSON at all {{{")
		assert result is None

	def test_empty_string_returns_none(self) -> None:
		assert extract_json_from_text("") is None
		assert extract_json_from_text("   ") is None

	def test_nested_json_object(self) -> None:
		data = {"outer": {"inner": [1, 2, 3]}, "flag": True}
		raw = f"Response:\n```json\n{json.dumps(data)}\n```"
		result = extract_json_from_text(raw)
		assert result == data

	def test_nested_json_in_prose_balanced(self) -> None:
		"""Balanced brace matching correctly extracts nested objects from prose."""
		data = {"a": {"b": {"c": 1}}, "d": [1, 2]}
		raw = f"Here is the result: {json.dumps(data)} end of output"
		result = extract_json_from_text(raw)
		assert result == data

	def test_large_input_no_hang(self) -> None:
		"""Large input with no valid JSON returns None without hanging."""
		# 100KB of text with nested braces but no valid JSON
		large_text = "x" * 50000 + " { not json { nested { deeper } } } " + "y" * 50000
		result = extract_json_from_text(large_text)
		assert result is None

	def test_json_with_escaped_quotes(self) -> None:
		"""JSON with escaped quotes inside strings is handled correctly."""
		data = {"message": 'He said "hello"', "count": 1}
		raw = f"Output: {json.dumps(data)}"
		result = extract_json_from_text(raw)
		assert result == data

	def test_large_repetitive_braces_no_hang(self) -> None:
		"""Repetitive brace patterns that could cause regex backtracking return promptly."""
		# Pattern that would cause catastrophic backtracking with greedy [\s\S]*
		text = "{ " * 500 + "not json" + " }" * 500
		result = extract_json_from_text(text)
		# Should return without hanging (balanced matcher handles this linearly)
		# Result may or may not be None depending on whether inner text parses as JSON
		assert result is None or isinstance(result, dict)



class TestFindBalanced:
	def test_simple_object(self) -> None:
		assert _find_balanced('{"a": 1}', "{", "}") == '{"a": 1}'

	def test_nested(self) -> None:
		text = '{"a": {"b": 1}}'
		assert _find_balanced(text, "{", "}") == text

	def test_string_with_braces(self) -> None:
		"""Braces inside JSON strings should not affect matching."""
		text = '{"msg": "use {x} here"}'
		assert _find_balanced(text, "{", "}") == text

