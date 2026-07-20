"""Rendering checks for Truth Layer verdicts in the OSS agents dashboard."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from agents_dashboard_html import agents_dashboard_html  # noqa: E402


class AgentsDashboardTruthTests(unittest.TestCase):
    def _render(self, truth):
        return agents_dashboard_html(
            "member@example.com",
            [
                {
                    "name": "research",
                    "role": "Research agent",
                    "summary": "Produces a cited research brief.",
                    "schedule": "on-demand",
                    "data_sources": ["vault"],
                    "can_do": ["summarize"],
                    "cannot_do": ["publish"],
                }
            ],
            {
                "running": [],
                "recent_runs": [
                    {
                        "agent": "research",
                        "status": "ok",
                        "ts": "2026-07-20 10:00:00",
                        "bytes": 512,
                        "brief_path": "/tmp/research.md",
                        "truth": truth,
                    }
                ],
                "latest_per_agent": {
                    "research": {
                        "ts": "2026-07-20 10:00:00",
                        "bytes": 512,
                        "brief_path": "/tmp/research.md",
                        "truth": truth,
                    }
                },
            },
        )

    def test_healthy_verdict_is_visible(self):
        html = self._render(
            {
                "storage_id": "receipt-1",
                "verdict": "healthy",
                "valid": True,
                "reason_codes": ["all_invariants_satisfied"],
            }
        )
        self.assertIn("Truth Layer", html)
        self.assertIn(">Healthy</span>", html)
        self.assertIn("all_invariants_satisfied", html)

    def test_unverified_legacy_run_is_explicit(self):
        html = self._render(None)
        self.assertIn(">not verified</span>", html)

    def test_reason_codes_are_html_escaped(self):
        html = self._render(
            {
                "verdict": "failed",
                "valid": False,
                "reason_codes": ['bad"><script>alert(1)</script>'],
            }
        )
        self.assertNotIn("<script>alert(1)</script>", html)
        self.assertIn("&lt;script&gt;alert(1)&lt;/script&gt;", html)


if __name__ == "__main__":
    unittest.main()
