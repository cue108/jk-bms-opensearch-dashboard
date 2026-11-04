import json, tempfile, threading, unittest, urllib.request
from pathlib import Path
from unittest import mock
import addon_gateway

def _gw_tmp(*pairs, **kw):
    """Conf written to a temp file: [(aid, base_url), …]; urls without trailing /."""
    f = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    for aid, url in pairs:
        f.write(f"{aid} = {url}/\n")
    f.close()
    return addon_gateway.AddonGateway(conf_path=f.name, ttl=0, gate_ttl=0, **kw)

class FakeHandler:
    def __init__(self): self.sent = []
    def _send(self, code, body, ctype): self.sent.append((code, body, ctype))
    def _redirect(self, code, loc): self.sent.append(("REDIR", code, loc))

def _html_srv(body=b"<html>PG</html>"):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    class H(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers(); self.wfile.write(body)
    s = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return s

class TestForward(unittest.TestCase):
    def test_prefix_strip_and_query_preserved(self):
        s = _html_srv(); h = FakeHandler()
        try:
            g = _gw_tmp(("demo", f"http://127.0.0.1:{s.server_address[1]}"))
            g.tpl = lambda t: t.replace("%DEVICE%", "D")
            self.assertTrue(g.forward("/addon/demo/api/x", "a=1", h))
            code, body, ctype = h.sent[0]
            self.assertEqual(code, 200)
            self.assertIn(b"PG", body)
        finally:
            s.shutdown()

    def test_page_served_from_cache_when_addon_goes_down(self):
        """Spec §3.3: the framed page must not blank on a brief outage —
        cache is last-good HTML keyed by health version; payload never caches."""
        s = _html_srv(b"<html>GOOD</html>"); h1 = FakeHandler(); h2 = FakeHandler()
        g = _gw_tmp(("demo", f"http://127.0.0.1:{s.server_address[1]}"))
        g.tpl = lambda t: t
        try:
            self.assertTrue(g.forward("/addon/demo/", "", h1))
            self.assertEqual(h1.sent[0][0], 200)
        finally:
            s.shutdown()                                  # addon is now down
        self.assertTrue(g.forward("/addon/demo/", "", h2))
        self.assertEqual(h2.sent[0][0], 200)               # served from cache
        self.assertIn(b"GOOD", h2.sent[0][1])

    def test_forward_failure_is_502_json(self):
        g = _gw_tmp(("dead", "http://127.0.0.1:1"))        # closed port, no cache
        h = FakeHandler()
        self.assertTrue(g.forward("/addon/dead/", "", h))
        self.assertEqual(h.sent[0][0], 502)
        self.assertIn(b"error", h.sent[0][1])

    def test_legacy_redirects_config_derived(self):
        g = _gw_tmp(("demo", "http://127.0.0.1:1"))
        self.assertEqual(g.legacy("/demo", ""),
                         ("REDIR", 302, "/addon/demo/"))
        self.assertEqual(g.legacy("/api/demo", "q=1"),
                         ("REDIR", 302, "/addon/demo/api/demo?q=1"))
        self.assertIsNone(g.legacy("/live", ""))          # core route never here

    def test_unconfigured_path_is_not_forwarded(self):
        g = _gw_tmp(("demo", "http://127.0.0.1:1"),
                    ("second", "https://host.local/x"))
        self.assertFalse(g.forward("/addon/ghost/", "", FakeHandler()))

    def test_diagnostic_has_no_secrets(self):
        g = _gw_tmp(("dead", "http://127.0.0.1:1"))
        d = g.diagnostic()
        self.assertEqual(d[0]["id"], "dead")
        self.assertNotIn("pass", json.dumps(d).lower())

class TestRoutingOrder(unittest.TestCase):
    def test_api_health_collector_proxy_branch_precedes_legacy(self):
        """Pin (fix round 1, review): server.py's /api/health collector-proxy
        branch must stay BEFORE the /addon + legacy/forward wiring. A configured
        id 'health' would otherwise claim the path — the legacy-claim assert
        below is the teeth; only branch order keeps the proxy answer."""
        import server
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        from urllib.error import HTTPError
        from urllib.request import build_opener, HTTPRedirectHandler

        # distinct from core's literal '{"status":"ok"}' fallback AND from any
        # forwarder/legacy/404 outcome — this exact triple is the discriminator
        MARKER = b'{"status":"ok","src":"collector"}'

        class _Collector(BaseHTTPRequestHandler):
            def log_message(self, *_): pass
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(MARKER)))
                self.end_headers(); self.wfile.write(MARKER)

        collector = ThreadingHTTPServer(("127.0.0.1", 0), _Collector)
        threading.Thread(target=collector.serve_forever, daemon=True).start()
        core = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        threading.Thread(target=core.serve_forever, daemon=True).start()

        class _NoRedir(HTTPRedirectHandler):       # observe 302s instead of following
            def redirect_request(self, *_a, **_k): return None
        op = build_opener(_NoRedir)
        base = f"http://127.0.0.1:{core.server_address[1]}"
        # id 'health' collides on purpose; dead urls — no forward ever resolves here
        gw = _gw_tmp(("health", "http://127.0.0.1:1"),
                     ("demo", "http://127.0.0.1:1"))

        def get(path):
            try:
                with op.open(urllib.request.Request(base + path), timeout=5) as r:
                    return r.status, r.headers.get("Content-Type"), r.read()
            except HTTPError as e:
                try:
                    return e.code, e.headers.get("Location"), e.read()
                finally:
                    e.close()

        try:
            with mock.patch.object(server, "GATEWAY", gw), \
                 mock.patch.object(server, "BACKEND",
                                   f"http://127.0.0.1:{collector.server_address[1]}"):
                # teeth: legacy WOULD capture /api/health if branch order broke
                self.assertEqual(gw.legacy("/api/health", ""),
                                 ("REDIR", 302, "/addon/health/api/health"))
                # proxy branch wins: collector bytes, not 302/404/forward/literal
                self.assertEqual(get("/api/health"),
                                 (200, "application/json", MARKER))
                # negative control: same /api/<id> shape still reaches legacy
                self.assertEqual(get("/api/demo"),
                                 (302, "/addon/demo/api/demo", b""))
        finally:
            core.shutdown(); core.server_close()
            collector.shutdown(); collector.server_close()
