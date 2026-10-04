import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "web" / "instructor_console_v1" / "index.html"
UI = ROOT / "web" / "instructor_console_v1" / "review_ui.js"

class ReviewFullscreenTests(unittest.TestCase):
    def test_fullscreen_button_and_css_exist(self):
        s = INDEX.read_text(encoding="utf-8")
        self.assertIn('id="reviewFullscreenBtn"', s)
        self.assertIn('reviewToggleFullscreen()', s)
        self.assertIn('.reviewViewerCard:fullscreen', s)
        self.assertIn('.reviewViewerCard:-webkit-full-screen', s)

    def test_fullscreen_uses_browser_api_only(self):
        s = UI.read_text(encoding="utf-8")
        self.assertIn("requestFullscreen", s)
        self.assertIn("exitFullscreen", s)
        self.assertIn("fullscreenchange", s)
        self.assertNotIn("/api/session/", s)
        self.assertNotIn("/api/authority/", s)
        self.assertNotIn("WebSocket(", s)

    def test_fullscreen_exported(self):
        s = UI.read_text(encoding="utf-8")
        self.assertIn("reviewToggleFullscreen", s)
        self.assertIn("reviewSyncFullscreenUi", s)

if __name__ == "__main__":
    unittest.main()
