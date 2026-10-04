from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web" / "instructor_console_v1"
R4A = ROOT / "src" / "training_core" / "instructor_console_r4a.py"

class Dev019ReviewFeatherTests(unittest.TestCase):
    def test_review_renderer_embeds_feather_not_direct_cesium(self):
        s=(WEB/"review_renderer.js").read_text()
        self.assertIn("/review_feather/map.html", s)
        self.assertIn("omni-review-frame", s)
        self.assertNotIn("new Cesium.Viewer", s)
        self.assertNotIn("OpenStreetMapImageryProvider", s)

    def test_feather_review_is_read_only_adapter(self):
        s=(WEB/"review_feather"/"review_main.js").read_text()
        self.assertIn('from "./src/js/core/map.js"', s)
        self.assertIn("updateHUD", s)
        self.assertIn("setTrainingView", s)
        for forbidden in ("connectBus", "sendBus", "WebSocket", "mavlink", "FlightCommandRouter", "/api/session/", "/api/authority/"):
            self.assertNotIn(forbidden, s)

    def test_feather_entry_does_not_load_legacy_main(self):
        s=(WEB/"review_feather"/"map.html").read_text()
        self.assertIn("./review_main.js", s)
        self.assertNotIn("./src/js/main.js", s)
        self.assertIn("Historical Review", s)

    def test_original_feather_visual_modules_are_present(self):
        for rel in (
            "src/js/core/map.js", "src/js/core/views.js", "src/js/core/camera.js",
            "src/js/core/aircraft.js", "src/js/core/surfaces.js",
            "src/css/base.css", "dist/models/omni_plane.glb",
            "locations/active_location.json", "vehicle_profiles/fixed_wing.json",
        ):
            self.assertTrue((WEB/"review_feather"/rel).is_file(), rel)

    def test_static_route_is_loopback_scoped_and_contained(self):
        s=R4A.read_text()
        self.assertIn('feather_prefix = "/review_feather/"', s)
        self.assertIn('self.web_root / "review_feather"', s)
        self.assertIn('target.relative_to(root)', s)
        self.assertIn('self._r4a_localhost()', s)
        self.assertRegex(s, r'"\.glb"\s*:\s*"model/gltf-binary"')

if __name__ == "__main__":
    unittest.main()
