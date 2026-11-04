# frontend/addon_gateway.py — generic addon gateway (core-side; NO addon names).
import json, os, re, time, urllib.request
from pathlib import Path

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_LINE_RE = re.compile(r"^\s*([a-z][a-z0-9-]{1,31})\s*=\s*(https?://\S+?)\s*(?:#.*)?$")


class AddonGateway:
    def __init__(self, conf_path=None, ttl=None, gate_ttl=None,
                 health_timeout=None, forward_timeout=None):
        self.conf_path = conf_path or os.environ.get("DSH_ADDONS_CONF") \
            or str(Path(__file__).with_name("addons.conf"))
        self.ttl = ttl if ttl is not None else 15.0
        self.gate_ttl = gate_ttl if gate_ttl is not None else \
            float(os.environ.get("DSH_GATE_TTL", "15"))
        self.health_timeout = health_timeout if health_timeout is not None else 2.0
        self.forward_timeout = forward_timeout if forward_timeout is not None else 10.0
        self.tpl = None                      # injected by server.py (_tpl callable)
        self._entries, self._loaded_at = {}, 0.0
        self._health_cache, self._page_cache, self._warned = {}, {}, {}

    def _warn(self, key: str, msg: str) -> None:
        now = time.monotonic()
        if now - self._warned.get(key, 0.0) >= max(self.ttl, 30.0):
            self._warned[key] = now
            print(f"[addon] {msg}", flush=True)       # one prefixed honest line

    def entries(self) -> dict:
        now = time.monotonic()
        if now - self._loaded_at < self.ttl:
            return dict(self._entries)
        out = {}
        try:
            text = Path(self.conf_path).read_text()
        except OSError:
            text = ""
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            m = _LINE_RE.match(line)
            if not m:
                self._warn("bad:" + line, f"stage=parse error=invalid line: {line!r}")
                continue
            aid, url = m.group(1), m.group(2).rstrip("/")
            if not _ID_RE.match(aid):
                self._warn("bad:" + line, f"stage=parse error=bad id {aid!r}")
                continue
            out[aid] = url
        self._entries, self._loaded_at = out, now
        return dict(out)

    def health(self, aid: str):
        now = time.monotonic()
        hit = self._health_cache.get(aid)
        if hit and now - hit[0] < self.gate_ttl:
            return hit[1]
        entries = self.entries()
        doc = None
        if aid in entries:
            try:
                req = urllib.request.Request(entries[aid] + "/health")
                with urllib.request.urlopen(req, timeout=self.health_timeout) as r:
                    cand = json.loads(r.read().decode())
                doc = cand if isinstance(cand, dict) and cand.get("id") == aid else None
                if doc is None:
                    self._warn(f"health:{aid}",
                               f"id={aid} stage=health error=id mismatch or bad json")
            except Exception as exc:                      # noqa: BLE001
                self._warn(f"health:{aid}", f"id={aid} stage=health error={exc}")
                doc = None
        self._health_cache[aid] = (now, doc)
        return doc

    def nav_tabs(self):
        out = []
        for aid, url in self.entries().items():
            h = self.health(aid)
            if not h or not h.get("fresh"):
                continue                                  # gate: present ∧ fresh
            label = str(h.get("title") or aid)
            if h.get("contract_version") != 1:
                label = (f"{label} ⚠ contract {h.get('contract_version')}"
                         f" (core 1) · v{h.get('version')}")
            out.append({"key": aid, "label": label, "src": f"/addon/{aid}/",
                        "hashes": [aid] + list(h.get("hashes") or []),
                        "order": int(h.get("order") or 100)})
        out.sort(key=lambda t: (t["order"], t["key"]))
        return out

    def forward(self, path: str, query: str, handler) -> bool:
        rest = path[len("/addon/"):]
        aid, _, sub = rest.partition("/")
        entries = self.entries()
        if aid not in entries:
            return False
        h = self.health(aid)                             # spec §9.5: label, refuse
        if h and h.get("contract_version") != 1:         # politely on version skew
            c = h.get("contract_version")
            self._warn(f"contract:{aid}",
                       f"id={aid} stage=forward error=contract {c} unsupported")
            if not sub:                                  # page route
                handler._send(200, (f"This addon requires a newer core "
                                    f"(contract {c}; core supports 1)").encode(),
                              "text/html; charset=utf-8")
                return True
            if sub == "api" or sub.startswith("api/"):   # api route
                handler._send(503, json.dumps(
                    {"error": f"contract {c} unsupported"}).encode(),
                    "application/json")
                return True
        if not sub and not path.endswith("/"):
            handler._redirect(301, f"/addon/{aid}/")          # slash normalisation
            return True
        url = entries[aid] + "/" + sub + (f"?{query}" if query else "")
        try:
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=self.forward_timeout) as r:
                body, ctype = r.read(), r.headers.get(
                    "Content-Type", "application/octet-stream")
                status = r.status
        except Exception as exc:                              # noqa: BLE001
            cached = self._page_cache.get(aid)
            is_page = (not sub) and cached is not None
            if is_page:
                # spec §3.3: briefly-down addon must not blank the framed page
                body, key = cached[1], cached[0]
                if self.tpl:
                    body = self.tpl(body.decode()).encode()
                handler._send(200, body, "text/html; charset=utf-8")
                return True
            self._warn(f"fwd:{aid}", f"id={aid} stage=forward error={exc}")
            handler._send(502, json.dumps({"error": str(exc)}).encode(),
                          "application/json")
            return True
        if ctype.startswith("text/html"):
            page_key = (self.health(aid) or {}).get("version")
            self._page_cache[aid] = (page_key, body)          # last-good keyed by version
            if self.tpl:
                body = self.tpl(body.decode()).encode()
        handler._send(status, body, ctype)
        return True

    def legacy(self, path: str, query: str):
        """Config-derived legacy routes (spec §4): first segment is a configured
        id ⇒ redirect into its mount; /api/<id> ⇒ mount + /api/<id>. Core routes
        are checked BEFORE this by Handler order, so no collision is possible."""
        entries = self.entries()
        parts = path.strip("/").split("/")
        qs = f"?{query}" if query else ""
        if len(parts) >= 2 and parts[0] == "api" and parts[1] in entries:
            aid = parts[1]
            tail = "/".join(parts[2:])
            loc = f"/addon/{aid}/api/{aid}" + ("/" + tail if tail else "")
            return ("REDIR", 302, loc + qs)
        if parts and parts[0] in entries:
            tail = "/".join(parts[1:])
            loc = f"/addon/{parts[0]}/" + tail
            return ("REDIR", 302, loc + qs)
        return None

    def diagnostic(self) -> list:
        out = []
        for aid, url in sorted(self.entries().items()).__iter__():
            h = self.health(aid)
            out.append({"id": aid, "url": url, "health": h,
                        "page_cached": aid in self._page_cache})
        return out
