from __future__ import annotations
import json, struct, unittest
from pathlib import Path

BASE=Path(__file__).resolve().parents[1]
WEB=BASE/'web'/'instructor_console_v1'

class ReviewUiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html=(WEB/'index.html').read_text(encoding='utf-8')
        cls.ui=(WEB/'review_ui.js').read_text(encoding='utf-8')
        cls.renderer=(WEB/'review_renderer.js').read_text(encoding='utf-8')
        cls.feather_main=(WEB/'review_feather'/'review_main.js').read_text(encoding='utf-8') if (WEB/'review_feather'/'review_main.js').is_file() else ''
        cls.feather_surfaces=(WEB/'review_feather'/'src'/'js'/'core'/'surfaces.js').read_text(encoding='utf-8') if (WEB/'review_feather'/'src'/'js'/'core'/'surfaces.js').is_file() else ''
        cls.feather_map=(WEB/'review_feather'/'map.html').read_text(encoding='utf-8') if (WEB/'review_feather'/'map.html').is_file() else ''

    def test_open_review_entry_and_workspace(self):
        self.assertIn('id="openReviewBtn"', self.html)
        self.assertIn('>OPEN REVIEW</button>', self.html)
        self.assertIn('id="review"', self.html)
        self.assertIn('HISTORICAL EVIDENCE · READ ONLY', self.html)
        self.assertIn("['dashboard','wizard','live','complete','review']", self.html)

    def test_review_client_only_uses_read_only_review_api(self):
        self.assertIn("const PROTOCOL='omni.instructor.review.v1'", self.ui)
        self.assertIn("const API_BASE='/api/review/v1'", self.ui)
        forbidden=('WebSocket','telemetry_bus','bus.js','recv_match','FlightCommandRouter','/api/session/','/api/authority/','multi_worker_entry','sim_vehicle.py','jsbsim','ardupilot')
        combined=self.ui+'\n'+self.renderer
        for token in forbidden:
            self.assertNotIn(token, combined, token)

    def test_review_controls_and_cursor_reconciliation_are_present(self):
        for op in ("call('list'", "call('open'", "call('snapshot'", "mutate('play'", "mutate('pause'", "mutate('speed'", "mutate('seek'", "mutate('step'", "mutate('event-jump'", "call('close'"):
            self.assertIn(op, self.ui, op)
        self.assertIn("err.code==='STALE_REVIEW_REVISION'", self.ui)
        self.assertIn('review_revision:revision()', self.ui)

    def test_cesium_is_review_safe_and_uses_feather_visual_adapter(self):
        self.assertIn('review_renderer.js', self.html)
        self.assertIn('/review_feather/map.html', self.renderer)
        self.assertIn('omni-review-frame', self.renderer)
        self.assertIn('Cesium.js', self.feather_map)
        self.assertIn('./review_main.js', self.feather_map)
        self.assertNotIn('./src/js/main.js', self.feather_map)
        self.assertNotIn('WebSocket', self.feather_main)
        self.assertNotIn('connectBus', self.feather_main)
        self.assertNotIn('sendBus', self.feather_main)
        self.assertNotIn('eyJhbGciOi', self.html+self.renderer+self.ui)

    def test_recorded_actuator_mapping(self):
        combined=self.feather_main+'\n'+self.feather_surfaces
        for token in ('srv1','srv2','srv3','srv4','aileron_L','aileron_R','ruddervator_L','ruddervator_R','prop','prop_disc'):
            self.assertIn(token, combined, token)
        self.assertIn("a.availability==='recorded'", self.ui)
        self.assertIn('No values are inferred from control input.', self.ui)

    def test_live_context_remains_visible_in_review(self):
        self.assertIn('id="runtimeStrip"', self.html)
        self.assertIn('id="reviewLiveContext"', self.html)
        self.assertIn('Historical Review controls cannot modify it.', self.ui)
        # summaryMode hides the strip only for complete; Review must not use summaryMode.
        self.assertIn("id==='complete'", self.html)


    def test_r4a_serves_review_static_allowlist_and_contained_feather_subtree(self):
        r4a=(BASE/'src'/'training_core'/'instructor_console_r4a.py').read_text(encoding='utf-8')
        self.assertIn('DEV019_REVIEW_STATIC_ALLOWLIST', r4a)
        for route in ('/review_ui.js','/review_renderer.js','/review_assets/omni_plane.glb'):
            self.assertIn(route, r4a)
        self.assertIn('allowed.get(path)', r4a)
        self.assertIn('feather_prefix = "/review_feather/"', r4a)
        self.assertIn('self.web_root / "review_feather"', r4a)
        self.assertIn('target.relative_to(root)', r4a)
        self.assertIn('self._r4a_localhost()', r4a)
        self.assertNotIn('SimpleHTTPRequestHandler', r4a)
        self.assertNotIn('serve_forever_static', r4a)

    def test_model_contains_verified_surface_nodes(self):
        model=WEB/'review_assets'/'omni_plane.glb'
        self.assertTrue(model.is_file(), model)
        with model.open('rb') as fh:
            magic,version,length=struct.unpack('<4sII',fh.read(12))
            self.assertEqual(magic,b'glTF')
            self.assertEqual(version,2)
            chunk_len,chunk_type=struct.unpack('<II',fh.read(8))
            self.assertEqual(chunk_type,0x4E4F534A)
            doc=json.loads(fh.read(chunk_len).decode('utf-8').rstrip('\x00 '))
        names={n.get('name') for n in doc.get('nodes',[]) if isinstance(n,dict)}
        expected={'aileron_L','aileron_R','ruddervator_L','ruddervator_R','prop','prop_disc'}
        self.assertTrue(expected.issubset(names), sorted(expected-names))

if __name__=='__main__': unittest.main()
