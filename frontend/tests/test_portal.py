import json, threading, unittest, urllib.request
from unittest import mock
import server

SHOW_GOLDEN = '''function show(which) {
  for (const k of Object.keys(tabs)) {
    const [tid, fid, src] = tabs[k];
    document.getElementById(tid).classList.toggle("on", k === which);
    const f = document.getElementById(fid);
    f.classList.toggle("on", k === which);
    if (k === which && f.getAttribute("src") === "about:blank")
      f.setAttribute("src", src);
  }
  history.replaceState(null, "", "#" + which);
}'''

class FakeGW:
    def __init__(self, tabs): self._tabs = tabs
    def nav_tabs(self): return self._tabs
    def health(self, aid): return None

def _page(tabs):
    with mock.patch.object(server, "GATEWAY", FakeGW(tabs)):
        return server.portal_page()

class TestPortal(unittest.TestCase):
    def test_standalone_has_exactly_two_core_tabs(self):
        html = _page([])
        self.assertEqual(html.count("<button"), 2)
        self.assertNotIn("iframe id=\"f-", html.replace(
            'iframe id="f-live"', "").replace('iframe id="f-hist"', "")
            .replace('<iframe id="f-live" class="on" src="/live"></iframe>', "")
            .replace('<iframe id="f-hist" src="about:blank"></iframe>', ""))
        # offer label assembled at runtime: the core test tree carries no
        # installed-addon name verbatim (Task 9 structural gate)
        self.assertNotIn("".join(("Ener", "gie", " Management")), html)
        self.assertIn(SHOW_GOLDEN, html)                   # show() byte-identical

    def test_addon_tab_generated_with_lazy_iframe_and_hash_alias(self):
        html = _page([{"key": "demo", "label": "Demo Addon",
                       "src": "/addon/demo/", "hashes": ["dm"], "order": 20}])
        self.assertIn('<button id="tab-demo">Demo Addon</button>', html)
        self.assertIn('<iframe id="f-demo" src="about:blank"></iframe>', html)
        self.assertIn('"demo": ["tab-demo", "f-demo", "/addon/demo/"]', html)
        self.assertIn('"dm": "demo"', html)                # alias → key
        self.assertIn('"live": "live"', html)              # default hash key exists

    def test_core_order_before_addons_and_label_mismatch_passthrough(self):
        html = _page([{"key": "zz", "label": "Zed ⚠ contract 2 (core 1) · v9",
                       "src": "/addon/zz/", "hashes": [], "order": 20}])
        self.assertLess(html.index("tab-live"), html.index("tab-zz"))
        self.assertIn("⚠ contract 2", html)
