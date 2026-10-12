"""Fleet federation: fan-out search to peers + optional vector replication.

Peer config (node.json "peers"):
  {"name": "atlas", "url": "http://192.168.6.181:8092", "kind": "atlas",
   "auth": "basic:tyrel:atlas_web_password",   # key FILE name in data/keys/
   "model_fp": "eg2-197a32965d4b",             # Atlas has no /healthz fp; asserted
   "timeout": 8}                               # optional per-peer budget (s)
  {"name": "da36", "url": "http://spark-da36:8900", "kind": "langbang",
   "auth": "bearer:rag_token_da36", "replicate": true}

langbang peers are asked with scope=local (never fan out again: no loops)
and receive our query VECTOR, so they score in exactly our space. Atlas only
takes text/image bytes. A peer that times out or fails costs at most
peer_timeout seconds and never fails the query. Replicated peers whose last
sync is fresh are answered from the local replica instead of the network.
"""
import asyncio
import base64
import json
import time
import urllib.parse

import httpx
import numpy as np

from . import config, embedder
from .store import name_terms


def auth_header(peer):
    spec = peer.get("auth") or ""
    kind, _, rest = spec.partition(":")
    if kind == "basic":
        user, _, keyfile = rest.partition(":")
        pw = config.read_key(keyfile)
        return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()} if pw else {}
    if kind == "bearer":
        tok = config.read_key(rest)
        return {"Authorization": "Bearer " + tok} if tok else {}
    return {}


def proxied(node, rel):
    return "/proxy?" + urllib.parse.urlencode({"node": node, "p": rel}) if rel else None


def _vec_b64(v):
    return base64.b64encode(np.asarray(v, dtype=np.float32).tobytes()).decode()


def vec_from_b64(s):
    return np.frombuffer(base64.b64decode(s), dtype=np.float32)


class Federation:
    def __init__(self, store, settings, log=print):
        self.store = store
        self.s = settings
        self.log = log
        self.status = {}  # name -> {ok, ms, error, at, model_fp}

    def peers(self):
        return [p for p in self.s.get("peers", []) if not p.get("disabled")]

    def peer(self, name):
        return next((p for p in self.s.get("peers", []) if p["name"] == name), None)

    def fresh_replica(self, p):
        if not p.get("replicate"):
            return False
        last = float(self.store.kv_get(f"last_sync:{p['name']}", 0) or 0)
        return time.time() - last < 2 * float(self.s.get("replicate_interval", 600))

    # ---- fan-out --------------------------------------------------------
    async def search(self, *, qvec, query=None, image_bytes=None, image_ctype="image/jpeg",
                     k=10, category=None, source=None):
        """-> (results, per-peer status). Replicated+fresh peers are skipped:
        their rows already sit in the local store."""
        default = float(self.s.get("peer_timeout", 3.0))
        targets = [p for p in self.peers() if not self.fresh_replica(p)]

        async def bounded(c, p):
            # per-peer budget: Atlas's document search alone takes ~3 s
            return await asyncio.wait_for(
                self._one(c, p, qvec, query, image_bytes, image_ctype, k, category, source),
                float(p.get("timeout", default)))
        async with httpx.AsyncClient(timeout=max([default] + [float(p.get("timeout", 0)) for p in targets])) as c:
            res = await asyncio.gather(*(bounded(c, p) for p in targets), return_exceptions=True)
        out = []
        for p, r in zip(targets, res):
            if isinstance(r, BaseException):
                self.status[p["name"]] = {"ok": False, "error": f"{type(r).__name__}: {r}"[:200],
                                          "at": time.time()}
                continue
            out += r
        return out

    async def _one(self, c, p, qvec, query, image_bytes, ctype, k, category, source):
        t0 = time.time()
        h = auth_header(p)
        if p.get("kind") == "atlas":
            with embedder.quiet.hold():  # our indexer stops feeding Atlas meanwhile
                hits = await self._atlas_kinds(c, p, h, query, image_bytes, ctype, k, category)
        else:
            # `terms`: the query words, for the peer's file-name boost
            body = {"vector": _vec_b64(qvec), "terms": name_terms(query), "top_k": k, "scope": "local",
                    "category": category, "source": source, "model_fp": self.s["model_fp"]}
            r = await c.post(p["url"].rstrip("/") + "/search", json=body, headers=h)
            r.raise_for_status()
            d = r.json()
            if d.get("model_fp") and d["model_fp"] != self.s["model_fp"]:
                raise RuntimeError(f"model_fp mismatch {d['model_fp']} != {self.s['model_fp']}")
            hits = []
            for x in d.get("results", []):
                node = x.get("node") or p["name"]
                x["node"] = node
                x["origin"] = node
                for key in ("url", "thumb"):
                    if x.get(key):
                        x[key] = proxied(p["name"], x[key])
                hits.append(x)
        self.status[p["name"]] = {"ok": True, "ms": int((time.time() - t0) * 1000), "at": time.time(),
                                  "n": len(hits)}
        return hits

    async def _atlas_kinds(self, c, p, h, query, image_bytes, ctype, k, category):
        """"all" = one call per media kind, in parallel (~0.1 s each)."""
        if category:
            return await self._atlas(c, p, h, query, image_bytes, ctype, k, category)
        budget = float(p.get("timeout", self.s.get("peer_timeout", 3.0))) - 0.3
        # Atlas's document search (1.3M chunks) takes ~10 s and nearly always
        # missed the budget, holding every "all" search for the full timeout;
        # its documents are searched when the caller asks for category=text
        # (or the peer sets "docs_in_all": true)
        kinds = ["image", "video", "audio"] + (["text"] if image_bytes is None and p.get("docs_in_all") else [])
        res = await asyncio.gather(*(asyncio.wait_for(
            self._atlas(c, p, h, query, image_bytes, ctype, k, kd), budget) for kd in kinds),
            return_exceptions=True)
        ok = [r for r in res if not isinstance(r, BaseException)]
        if not ok:
            raise res[0]
        late = [kd for kd, r in zip(kinds, res) if isinstance(r, BaseException)]
        if late:
            self.log(f"atlas: {','.join(late)} search missed the {budget:.1f}s budget")
        return [x for r in ok for x in r]

    async def _atlas(self, c, p, h, query, image_bytes, ctype, k, category):
        if p.get("model_fp") and p["model_fp"] != self.s["model_fp"]:
            raise RuntimeError("model_fp mismatch")
        kind = {"text": "document", None: "all", "": "all"}.get(category, category)
        base = p["url"].rstrip("/")
        top = min(int(k), 48)
        if image_bytes is not None:
            if kind == "document":
                return []
            r = await c.post(base + "/api/search-image", params={"kind": kind, "top": top},
                             content=image_bytes, headers={**h, "Content-Type": ctype})
        elif query and len(query) >= 2:
            r = await c.get(base + "/api/search", params={"q": query[:400], "kind": kind, "top": top},
                            headers=h)
        else:
            return []
        r.raise_for_status()
        out = []
        for x in r.json().get("results", []):
            path = x.get("path") or (x.get("summary") or "")[:80]
            item = {"node": p["name"], "origin": p["name"], "path": path,
                    "category": "text" if x.get("kind") == "document" else x.get("kind"),
                    "source": "home", "score": x.get("score"),
                    "snippet": (x.get("text") or x.get("summary") or path.rsplit("/", 1)[-1])[:300],
                    "position": float(x.get("position") or 0)}
            for key in ("thumb", "url"):
                if x.get(key):
                    item[key] = proxied(p["name"], x[key])
            if item["score"] is not None:
                out.append(item)
        return out

    # ---- replication ----------------------------------------------------
    def replicate_once(self, p):
        """Pull every own-node row a langbang peer changed since our cursor."""
        if p.get("kind") != "langbang":
            return 0
        h = auth_header(p)
        base = p["url"].rstrip("/")
        with httpx.Client(timeout=60) as c:
            hz = c.get(base + "/healthz", headers=h).json()
            if hz.get("model_fp") != self.s["model_fp"]:
                raise RuntimeError(f"{p['name']}: model_fp {hz.get('model_fp')} != {self.s['model_fp']}")
            key = f"cursor:{p['name']}"
            cur = int(self.store.kv_get(key, 0) or 0)
            total = 0
            while True:
                r = c.get(base + "/replicate/export", params={"since": cur, "limit": 2000}, headers=h)
                r.raise_for_status()
                rows = [json.loads(ln) for ln in r.text.splitlines() if ln.strip()]
                if not rows:
                    break
                self.store.apply_replica(rows)
                cur = max(int(x["seq"]) for x in rows)
                self.store.kv_set(key, cur)
                total += len(rows)
                if len(rows) < 2000:
                    break
        self.store.kv_set(f"last_sync:{p['name']}", time.time())
        return total

    def replicate_all(self):
        for p in self.peers():
            if p.get("replicate"):
                try:
                    n = self.replicate_once(p)
                    if n:
                        self.log(f"replicated {n} rows from {p['name']}")
                except Exception as e:  # noqa: BLE001
                    self.log(f"replicate {p['name']}: {type(e).__name__}: {e}")


def merge(results, k):
    """Dedupe by (node, path) keeping the best score, then top-k."""
    best = {}
    for r in results:
        key = (r.get("node"), r.get("path"))
        if key not in best or (r.get("score") or -9) > (best[key].get("score") or -9):
            best[key] = r
    return sorted(best.values(), key=lambda r: -(r.get("score") or -9))[:k]
