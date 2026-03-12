"""External intelligence subsystem for monitoring AI/agent ecosystem developments."""

from autodev.intelligence.claude_code import scan_claude_code
from autodev.intelligence.evaluator import evaluate_findings, generate_proposals
from autodev.intelligence.models import AdaptationProposal, Finding, IntelSource
from autodev.intelligence.scanner import IntelReport, IntelScanner, run_scan
from autodev.intelligence.sources import (
	IncrementalScanner,
	ScanCache,
	scan_arxiv,
	scan_github,
	scan_hackernews,
	scan_incremental,
)

__all__ = [
	"AdaptationProposal",
	"Finding",
	"IncrementalScanner",
	"IntelReport",
	"IntelScanner",
	"IntelSource",
	"ScanCache",
	"evaluate_findings",
	"generate_proposals",
	"run_scan",
	"scan_arxiv",
	"scan_claude_code",
	"scan_github",
	"scan_hackernews",
	"scan_incremental",
]
