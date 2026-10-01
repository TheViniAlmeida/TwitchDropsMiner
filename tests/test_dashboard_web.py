"""Static dashboard policy and packaging checks."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class DashboardWebTests(unittest.TestCase):
    def test_static_content_policy(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for script in re.findall(r"<script\b[^>]*>", html, re.IGNORECASE):
            self.assertRegex(script, r"\bsrc\s*=")
        for path in WEB.iterdir():
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                content = path.read_text(encoding="utf-8")
                self.assertNotRegex(content, r"\s+on[a-z]+\s*=")
                self.assertNotRegex(content, r"\s+style\s*=")
                self.assertNotIn("innerHTML", content)
                self.assertNotRegex(content, r"\beval\s*\(")
                self.assertNotIn("localStorage", content)
                urls = re.findall(r"https?://[^\s\"'`<>]+", content)
                self.assertTrue(all(url.startswith("https://static-cdn.jtvnw.net/") for url in urls), urls)

    def test_build_spec_includes_web_assets(self) -> None:
        spec = (ROOT / "build.spec").read_text(encoding="utf-8")
        for name in ("index.html", "app.js", "style.css"):
            with self.subTest(name=name):
                self.assertIn(f'(Path("web/{name}"), "./web", True)', spec)
