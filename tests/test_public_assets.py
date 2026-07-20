"""Hermetic smoke tests for the public landing and authentication assets."""

from __future__ import annotations

import re
import sys
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PUBLIC = ROOT / "public"
sys.path.insert(0, str(ROOT / "server"))

import html_chrome  # noqa: E402
import members_auth_html  # noqa: E402
from public_globus_html import public_globus_landing_html  # noqa: E402


class PublicAssetTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        html_chrome.configure(site="", members_dir="")
        members_auth_html.configure(site="")
        cls.css = (PUBLIC / "styles.css").read_text(encoding="utf-8")

    def test_assets_are_small_self_contained_source_files(self) -> None:
        stylesheet = PUBLIC / "styles.css"
        favicon = PUBLIC / "favicon.svg"
        self.assertTrue(stylesheet.is_file())
        self.assertTrue(favicon.is_file())
        self.assertLess(stylesheet.stat().st_size, 20_000)
        self.assertLess(favicon.stat().st_size, 4_000)
        self.assertNotIn("@import", self.css)

    def test_public_pages_link_assets_and_every_emitted_class_is_styled(self) -> None:
        pages = [
            public_globus_landing_html(public_chat_enabled=True),
            members_auth_html.login_html("Try again."),
            members_auth_html.code_html("member@example.com", "Sent.", ok=True),
            members_auth_html.onboarding_html("member@example.com"),
        ]
        classes: set[str] = set()
        for page in pages:
            self.assertIn('href="/favicon.svg"', page)
            self.assertIn('href="/styles.css"', page)
            for class_list in re.findall(r'class="([^"]+)"', page):
                classes.update(class_list.split())

        missing = [
            name
            for name in sorted(classes)
            if re.search(rf"\.{re.escape(name)}(?=[\s,:.>+~#{{])", self.css) is None
        ]
        self.assertEqual(missing, [], f"missing CSS selectors for: {missing}")

    def test_favicon_has_accessible_text_and_no_external_content(self) -> None:
        favicon = (PUBLIC / "favicon.svg").read_text(encoding="utf-8")
        root = ET.fromstring(favicon)
        namespace = {"svg": "http://www.w3.org/2000/svg"}
        title = root.find("svg:title", namespace)
        description = root.find("svg:desc", namespace)
        self.assertEqual(root.attrib.get("role"), "img")
        self.assertTrue(root.attrib.get("aria-labelledby"))
        self.assertTrue(title is not None and (title.text or "").strip())
        self.assertTrue(description is not None and (description.text or "").strip())
        self.assertNotIn("<script", favicon.lower())
        self.assertNotIn("http://", favicon.replace("http://www.w3.org/2000/svg", ""))
        self.assertNotIn("https://", favicon)

    def test_docker_image_copies_public_assets(self) -> None:
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(
            dockerfile,
            r"(?m)^COPY --chown=globus:globus public/\s+/app/public/$",
        )


if __name__ == "__main__":
    unittest.main()
