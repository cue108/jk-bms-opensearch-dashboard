import os, tempfile, time, unittest
from pathlib import Path
import addon_gateway

OK = Path(__file__).with_name("fakeconf_ok.txt").read_text()
BAD = Path(__file__).with_name("fakeconf_bad.txt").read_text()

def _conf(text):
    f = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    f.write(text); f.close(); return f.name

class TestConf(unittest.TestCase):
    def test_parse_ok(self):
        gw = addon_gateway.AddonGateway(conf_path=_conf(OK), ttl=0)
        self.assertEqual(gw.entries(), {"demo": "http://10.0.0.9:8101",
                                        "second": "https://host.local/x"})

    def test_invalid_lines_skipped_others_survive(self):
        gw = addon_gateway.AddonGateway(conf_path=_conf(BAD), ttl=0)
        self.assertEqual(list(gw.entries()), ["goodone"])   # per-line isolation

    def test_missing_file_is_standalone(self):
        gw = addon_gateway.AddonGateway(conf_path="/nope/none.conf", ttl=0)
        self.assertEqual(gw.entries(), {})

    def test_ttl_re_read_without_restart(self):
        p = _conf("# nothing\n")
        gw = addonGateway = addon_gateway.AddonGateway(conf_path=p, ttl=15)
        self.assertEqual(gw.entries(), {})
        with open(p, "a") as fh: fh.write("added = http://10.0.0.9:9000\n")
        self.assertEqual(gw.entries(), {})            # cached still
        gw._loaded_at = time.monotonic() - 16         # age past TTL
        self.assertEqual(gw.entries(), {"added": "http://10.0.0.9:9000"})

    def test_url_normalised_no_trailing_slash(self):
        gw = addon_gateway.AddonGateway(conf_path=_conf("xy = http://h:1/\n"), ttl=0)
        self.assertEqual(gw.entries()["xy"], "http://h:1")

    def test_bad_scheme_skipped(self):
        gw = addon_gateway.AddonGateway(conf_path=_conf("x = ftp://nope/\n"), ttl=0)
        self.assertEqual(gw.entries(), {})
