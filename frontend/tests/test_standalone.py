import importlib, threading, unittest, urllib.error, urllib.request
from pathlib import Path
import addon_gateway, server

FRONTEND = Path(__file__).resolve().parents[1]
# The forbidden tokens are assembled at runtime: this file lives inside the
# tree it audits, so neither the source text nor the compiled bytecode of any
# *.py under frontend/ may carry an installed-addon name verbatim.
FORBIDDEN = ("".join(("grid", "guard")), "".join(("ener", "gie")))  # no installed-addon names in core
GG = "/" + "".join(("grid", "guard"))          # retired addon route (must 404)
OFFER = "".join(("Ener", "gie", " Management"))  # retired offer label (must not render)


class TestStructural(unittest.TestCase):
    def test_no_addon_name_in_any_core_file(self):
        for p in list(FRONTEND.rglob("*.py")) + list(FRONTEND.rglob("*.conf")):
            if "__pycache__" in p.parts:
                continue
            text = p.read_text(errors="ignore").lower()
            for name in FORBIDDEN:
                self.assertNotIn(name, text, f"{name} leaked into {p}")


def _boot(conf_path):
    gw = addon_gateway.AddonGateway(conf_path=conf_path, ttl=0, gate_ttl=0)
    gw.tpl = server._tpl
    old = server.GATEWAY
    server.GATEWAY = gw
    srv = importlib.import_module("http.server").ThreadingHTTPServer(
        ("127.0.0.1", 0), server.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}", old


class TestExecutableStandalone(unittest.TestCase):
    def _portal(self, conf):
        srv, base, old = _boot(conf)
        try:
            with urllib.request.urlopen(base + "/", timeout=5) as r:
                html = r.read().decode()
            for path, want in ((GG, 404), ("/api" + GG, 404),
                               ("/addon/nope/", 404), ("/api/addons", 200)):
                try:
                    with urllib.request.urlopen(base + path, timeout=5) as r:
                        code = r.status
                except urllib.error.HTTPError as e:
                    code = e.code
                    e.close()
                self.assertEqual(code, want, path)
            return html
        finally:
            srv.shutdown(); srv.server_close()
            server.GATEWAY = old

    def test_missing_conf_is_standalone(self):
        html = self._portal("/definitely/missing.conf")
        self.assertEqual(html.count("<button"), 2)
        self.assertNotIn(OFFER, html)

    def test_comment_only_conf_is_standalone(self):
        html = self._portal(str(Path(__file__).with_name("fakeconf_comment.txt")))
        self.assertEqual(html.count("<button"), 2)
