"""Static dashboard policy and packaging checks."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"


class DashboardWebTests(unittest.TestCase):
    def test_static_content_policy(self) -> None:
        html = (WEB / "index.html").read_text(encoding="utf-8")
        for script in re.findall(r"<script\b([^>]*)>(.*?)</script\s*>", html, re.IGNORECASE | re.DOTALL):
            self.assertRegex(script[0], r"\bsrc\s*=")
            self.assertFalse(script[1].strip(), "Inline scripts are forbidden")
        for path in WEB.rglob("*"):
            if not path.is_file():
                continue
            with self.subTest(path=path.name):
                content = path.read_text(encoding="utf-8")
                if path.suffix == ".html":
                    self.assertNotRegex(content, r"\s+on[a-z]+\s*=")
                    self.assertNotRegex(content, r"\s+style\s*=")
                for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "new Function"):
                    self.assertNotIn(forbidden, content)
                self.assertNotRegex(content, r"\beval\s*\(")
                self.assertNotRegex(content, r"setAttribute\s*\(\s*['\"]style['\"]")
                urls = re.findall(r"https?://[^\s\"'`<>]+", content)
                self.assertTrue(all(url.startswith("https://static-cdn.jtvnw.net/") or url.startswith("http://www.w3.org/2000/svg") for url in urls), urls)

    def test_build_spec_includes_web_assets(self) -> None:
        spec = (ROOT / "build.spec").read_text(encoding="utf-8")
        for path in WEB.rglob("*"):
            if path.is_file():
                with self.subTest(name=path.name):
                    self.assertIn(f'(Path("{path.relative_to(ROOT).as_posix()}"), "./web", True)', spec)
