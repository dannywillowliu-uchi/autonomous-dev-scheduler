"""Tests for shared JSON extraction utilities."""

from __future__ import annotations

import json

from mission_control.json_utils import extract_json_from_text


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

	def test_markdown_fenced_no_lang(self) -> None:
		raw = '```\n{"score": 0.5}\n```'
		result = extract_json_from_text(raw)
		assert result == {"score": 0.5}

	def test_json_embedded_in_prose(self) -> None:
		raw = 'Here is my answer:\n{"score": 0.7, "met": false}\nThat is all.'
		result = extract_json_from_text(raw)
		assert result is not None
		assert result["score"] == 0.7

	def test_array_in_markdown_fences(self) -> None:
		inner = json.dumps([{"title": "Task A"}, {"title": "Task B"}])
		raw = f"Plan:\n```json\n{inner}\n```"
		result = extract_json_from_text(raw, expect_array=True)
		assert isinstance(result, list)
		assert len(result) == 2

	def test_array_embedded_in_prose(self) -> None:
		raw = 'Here are the tasks:\n[{"title": "A"}, {"title": "B"}]\nDone.'
		result = extract_json_from_text(raw, expect_array=True)
		assert isinstance(result, list)
		assert len(result) == 2

	def test_invalid_json_returns_none(self) -> None:
		result = extract_json_from_text("This is not JSON at all {{{")
		assert result is None

	def test_empty_string_returns_none(self) -> None:
		assert extract_json_from_text("") is None
		assert extract_json_from_text("   ") is None

	def test_none_like_input(self) -> None:
		assert extract_json_from_text("") is None

	def test_nested_json_object(self) -> None:
		data = {"outer": {"inner": [1, 2, 3]}, "flag": True}
		raw = f"Response:\n```json\n{json.dumps(data)}\n```"
		result = extract_json_from_text(raw)
		assert result == data

	def test_json_with_surrounding_whitespace(self) -> None:
		raw = '\n\n  {"key": "value"}  \n\n'
		result = extract_json_from_text(raw)
		assert result == {"key": "value"}

	def test_multiple_fences_uses_first(self) -> None:
		raw = '```json\n{"first": true}\n```\nMore text\n```json\n{"second": true}\n```'
		result = extract_json_from_text(raw)
		assert result is not None
		assert result.get("first") is True
