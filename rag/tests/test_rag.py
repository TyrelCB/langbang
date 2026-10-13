"""Offline tests for the RAG node (no model, no network).

    .venv/bin/python -m unittest -v rag.tests.test_rag
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from unittest import mock

import numpy as np

from rag import embedder, federation
from rag.store import Store


def unit(seed):
    v = np.random.default_rng(seed).standard_normal(embedder.DIM).astype(np.float32)
    return v / np.linalg.norm(v)


class FakeBackend:
    name = "fake"

    def __init__(self):
        self.calls = []

    def embed_text(self, texts, mode):
        self.calls.append((list(texts), mode))
        return np.stack([unit(hash(t) % 2**32) for t in texts])

    def embed_image(self, image):
        return unit(1)

    def embed_audio(self, path):
        return [(unit(2), 0.0)]


class TmpStore(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.td.name, "r.db"), "nodeA")

    def tearDown(self):
        self.td.cleanup()


class StoreTests(TmpStore):
    def test_put_search_and_incremental_cache(self):
        a, b = unit(10), unit(11)
        self.store.put_file("/x/a.txt", [(0, "alpha", a, 0)], category="text", source="home")
        hit = self.store.search(a, 5)[0]
        self.assertEqual(hit["path"], "/x/a.txt")
        self.assertAlmostEqual(hit["score"], 1.0, places=2)
        self.assertEqual(hit["snippet"], "alpha")
        n0 = self.store.cache.n
        self.store.put_file("/x/b.txt", [(0, "beta", b, 0)], category="text", source="output")
        self.assertEqual(self.store.search(b, 1)[0]["path"], "/x/b.txt")  # picked up without reload
        self.assertEqual(self.store.cache.n, n0 + 1)
        self.assertEqual([r["path"] for r in self.store.search(a, 5, source="output")], ["/x/b.txt"])

    def test_reindex_shrink_tombstones_old_chunks(self):
        self.store.put_file("/x/doc", [(0, "c0", unit(1), 0), (1, "c1", unit(2), 0), (2, "c2", unit(3), 0)],
                            category="text", source="home")
        self.store.put_file("/x/doc", [(0, "c0'", unit(4), 0)], category="text", source="home")
        live = self.store.con.execute("SELECT COUNT(*) FROM chunks WHERE deleted=0").fetchone()[0]
        self.assertEqual(live, 1)
        hits = self.store.search(unit(3), 5)  # old chunk 2's vector must be gone
        self.assertTrue(all(h["score"] < 0.5 for h in hits))

    def test_delete_and_dedupe_best_chunk_per_path(self):
        q = unit(7)
        self.store.put_file("/v.mp4", [(i, "v", q * (1 - i * 0.1) + unit(100 + i) * i * 0.1, i)
                                       for i in range(5)], category="video", source="home")
        hits = self.store.search(q, 5)
        self.assertEqual(len(hits), 1)  # five chunks, one path
        self.assertEqual(hits[0]["position"], 0)
        self.store.delete_path("/v.mp4")
        self.assertEqual(self.store.search(q, 5), [])

    def test_delete_where_tid_and_refuses_whole_node(self):
        self.store.put_file("lb-thread:t1/0", [(0, "hi", unit(1), 0)], category="text", source="thread",
                            meta={"tid": "t1"})
        self.store.put_file("lb-thread:t2/0", [(0, "yo", unit(2), 0)], category="text", source="thread",
                            meta={"tid": "t2"})
        self.assertEqual(self.store.delete_where(tid="t1"), 1)
        self.assertEqual({h["path"] for h in self.store.search(unit(1), 5)}, {"lb-thread:t2/0"})
        with self.assertRaises(ValueError):
            self.store.delete_where()

    def test_replication_roundtrip_with_tombstones(self):
        other = Store(os.path.join(self.td.name, "b.db"), "nodeB")
        other.put_file("/b/1", [(0, "one", unit(1), 0)], category="text", source="home")
        other.put_file("/b/2", [(0, "two", unit(2), 0)], category="image", source="output")
        rows = [json.loads(json.dumps(r)) for r in other.export(0)]  # through JSON like the wire
        self.store.apply_replica(rows)
        cur = max(r["seq"] for r in rows)
        hit = self.store.search(unit(2), 1)[0]
        self.assertEqual((hit["node"], hit["path"]), ("nodeB", "/b/2"))
        other.delete_path("/b/2")
        self.store.apply_replica(other.export(cur))
        self.assertNotIn("/b/2", {h["path"] for h in self.store.search(unit(2), 5)})
        # replicas are never re-exported (no loops through the fleet)
        self.assertEqual(self.store.export(0), [])
        # a peer can't overwrite our own rows
        self.store.put_file("/mine", [(0, "m", unit(5), 0)], category="text", source="home")
        self.store.apply_replica([{**rows[0], "node": "nodeA", "path": "/mine", "chunk_idx": 0,
                                   "text": "hijack"}])
        self.assertEqual(self.store.search(unit(5), 1)[0]["snippet"], "m")


class NameBoostTests(TmpStore):
    def test_name_match_lifts_file_over_closer_vector(self):
        """Regression: "resume" ranked VS Code history junk above Resume.md."""
        from rag.store import name_terms
        q = unit(20)
        self.store.put_file("/x/junk.html", [(0, "junk", q, 0)], category="text", source="home")
        near = q * 0.9 + unit(21) * 0.436
        self.store.put_file("/d/resume/Tyrel_Barstow_Resume.md", [(0, "cv", near / np.linalg.norm(near), 0)],
                            category="text", source="home")
        self.assertEqual(self.store.search(q, 2)[0]["path"], "/x/junk.html")  # dense alone
        terms = name_terms("my resume")
        self.assertEqual(terms, ["resume"])
        self.assertEqual(self.store.search(q, 2, terms=terms)[0]["path"], "/d/resume/Tyrel_Barstow_Resume.md")
        # the incremental name index picks up files added after the first boosted search
        self.store.put_file("/y/other_resume_v2.txt", [(0, "v2", q, 0)], category="text", source="home")
        self.assertEqual(self.store.search(q, 1, terms=terms)[0]["path"], "/y/other_resume_v2.txt")


class LockAndAudioTests(unittest.TestCase):
    def test_priority_lock_lets_queries_jump_indexing(self):
        import threading
        lk, order = embedder.PriorityLock(), []
        lk.acquire(False)  # an index encode in flight

        def worker(name, urgent):
            lk.acquire(urgent)
            order.append(name)
            lk.release()
        ts = [threading.Thread(target=worker, args=(f"idx{i}", False)) for i in range(3)]
        for t in ts:
            t.start()
        time.sleep(0.1)
        q = threading.Thread(target=worker, args=("query", True))
        q.start()
        time.sleep(0.1)
        lk.release()
        for t in ts + [q]:
            t.join(2)
        self.assertEqual(order[0], "query")

    def test_remote_audio_stops_at_real_end(self):
        """ffprobe over-reports some AAC lengths (8000 s for a 4-min song);
        segments past the real end must not be sent (Atlas 422 -> CPU fallback)."""
        rb = embedder.RemoteBackend("http://atlas")
        sent = []
        rb._post = lambda path, body, ctype: sent.append(len(body)) or {"data": [{"embedding": unit(1).tolist(),
                                                                                    "position": 0}]}
        with tempfile.TemporaryDirectory() as td:
            wav = os.path.join(td, "a.wav")
            os.system(f"ffmpeg -v error -f lavfi -i sine=d=20 -ac 1 -ar 16000 {wav}")
            with mock.patch.object(embedder, "ffprobe_duration", return_value=8000.0):
                out = rb.embed_audio(wav)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(out), 1)


class FallbackTests(unittest.TestCase):
    def test_retries_peer_before_cpu_fallback(self):
        local, calls = FakeBackend(), []

        class Flaky:
            name = "remote"

            def embed_text(self, texts, mode):
                calls.append(1)
                if len(calls) < 3:
                    raise ConnectionRefusedError("restarting")
                return np.stack([unit(9)] * len(texts))
        e = embedder.Embedder(local, Flaky(), log=lambda m: None)
        e.RETRY_DELAYS = (0.01, 0.01, 0.01)
        e.docs(["a"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(local.calls, [])  # never touched the CPU


class RescanWindowTests(unittest.TestCase):
    def test_window_wraps_midnight(self):
        from rag.indexer import Indexer
        ix = Indexer.__new__(Indexer)
        at = lambda h, m: time.mktime((2026, 10, 12, h, m, 0, 0, 0, -1))
        ix.s = {"rescan_window": "01:00-08:00"}
        self.assertTrue(ix.in_window(at(3, 30)))
        self.assertFalse(ix.in_window(at(8, 0)))
        ix.s = {"rescan_window": "22:00-06:00"}
        self.assertTrue(ix.in_window(at(23, 0)) and ix.in_window(at(5, 59)))
        self.assertFalse(ix.in_window(at(12, 0)))
        ix.s = {"rescan_window": ""}
        self.assertTrue(ix.in_window(at(12, 0)))


class PrefixTests(unittest.TestCase):
    def test_prefixes(self):
        self.assertEqual(embedder.prefixed(["x"], "query"), ["task: search result | query: x"])
        self.assertEqual(embedder.prefixed(["x"], "document"), ["title: none | text: x"])
        with self.assertRaises(ValueError):
            embedder.prefixed(["x"], None)

    def test_remote_sends_mode_and_never_preprefixes(self):
        """Regression: the old indexer double-prefixed docs AND let Atlas default to query mode."""
        rb = embedder.RemoteBackend("http://atlas")
        sent = []

        def fake_post(path, body, ctype):
            d = json.loads(body)
            sent.append(d)
            return {"data": [{"index": i, "embedding": unit(i).tolist()} for i in range(len(d["input"]))]}
        rb._post = fake_post
        rb.embed_text(["chunk one", "chunk two"], "document")
        self.assertEqual(sent[0]["mode"], "document")
        self.assertEqual(sent[0]["input"], ["chunk one", "chunk two"])

    def test_remote_batches_by_char_budget_and_fills_blanks(self):
        rb = embedder.RemoteBackend("http://atlas")
        sent = []

        def fake_post(path, body, ctype):
            d = json.loads(body)
            assert len(d["input"]) <= 32 and sum(map(len, d["input"])) <= 131072
            assert all(t.strip() for t in d["input"])
            sent.append(len(d["input"]))
            return {"data": [{"index": i, "embedding": unit(i).tolist()} for i in range(len(d["input"]))]}
        rb._post = fake_post
        out = rb.embed_text(["x" * 130_000, "", "  ", "ok"] + ["y"] * 40, "document")
        self.assertEqual(len(out), 44)
        self.assertEqual(sent, [32, 12])  # count cap
        sent.clear()
        rb.embed_text(["z" * 130_000] * 5, "document")  # each capped to 32k chars
        self.assertEqual(sent, [3, 2])  # char budget

    def test_indexer_docs_use_document_mode(self):
        from rag.indexer import Indexer
        with tempfile.TemporaryDirectory() as td:
            st = Store(os.path.join(td, "r.db"), "n")
            fb = FakeBackend()
            ix = Indexer(st, embedder.Embedder(fb), {"model_fp": "fp", "sources": {}})
            ix.index_docs([{"path": "lb-thread:t/0", "text": "hello world " * 20, "source": "thread",
                            "meta": {"tid": "t"}}])
            self.assertTrue(fb.calls and all(mode == "document" for _, mode in fb.calls))
            self.assertEqual(st.search(unit(0), 1)[0]["meta"], {"tid": "t"})


class FederationTests(TmpStore):
    def test_merge_dedupes_keeps_best(self):
        out = federation.merge([{"node": "a", "path": "/p", "score": 0.5}, {"node": "a", "path": "/p", "score": 0.7},
                                {"node": "b", "path": "/p", "score": 0.6}], 10)
        self.assertEqual([(r["node"], r["score"]) for r in out], [("a", 0.7), ("b", 0.6)])

    def _fed(self, peers, **kw):
        s = {"model_fp": "fpA", "peers": peers, "peer_timeout": 0.5, "replicate_interval": 600, **kw}
        return federation.Federation(self.store, s, log=lambda m: None)

    def test_dead_peer_times_out_and_fp_mismatch_refused(self):
        fed = self._fed([{"name": "dead", "url": "http://10.255.255.1:9", "kind": "langbang"},
                         {"name": "wrongfp", "url": "http://x", "kind": "atlas", "model_fp": "other"}])
        t0 = time.time()
        res = asyncio.run(fed.search(qvec=unit(1), query="bears", k=5))
        self.assertEqual(res, [])
        self.assertLess(time.time() - t0, 2.5)
        self.assertFalse(fed.status["dead"]["ok"])
        self.assertIn("mismatch", fed.status["wrongfp"]["error"])

    def test_langbang_peer_hits_are_proxied_and_tagged(self):
        fed = self._fed([{"name": "b", "url": "http://b:8900", "kind": "langbang"}])

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return {"model_fp": "fpA", "results": [
                    {"node": "b", "path": "/pic.png", "category": "image", "score": 0.9,
                     "url": "/media?path=%2Fpic.png", "thumb": "/thumbnail?path=%2Fpic.png&position=0"}]}

        async def fake_post(self, url, json=None, headers=None):
            self.sent = json
            assert json["scope"] == "local" and json["vector"]
            return R()
        with mock.patch("httpx.AsyncClient.post", fake_post):
            res = asyncio.run(fed.search(qvec=unit(1), query="q", k=5))
        self.assertEqual(res[0]["origin"], "b")
        self.assertTrue(res[0]["url"].startswith("/proxy?node=b&p=%2Fmedia"))

    def test_multi_category_filters_and_atlas_kinds(self):
        from rag.server import categories
        self.assertEqual(categories("image, audio"), ["image", "audio"])
        self.assertIsNone(categories(""))
        v = unit(3)
        for i, cat in enumerate(("text", "image", "audio", "video")):
            self.store.put_file(f"/x/{cat}", [(0, cat, v, 0)], category=cat, source="home")
        got = {r["path"] for r in self.store.search(v, 10, category=["image", "audio"])}
        self.assertEqual(got, {"/x/image", "/x/audio"})
        fed = self._fed([{"name": "atlas", "url": "http://a", "kind": "atlas"}])
        asked = []

        async def fake_atlas(c, p, h, query, image_bytes, ctype, k, kind):
            asked.append(kind)
            return []
        with mock.patch.object(fed, "_atlas", fake_atlas):
            asyncio.run(fed.search(qvec=unit(1), query="boots", k=5, category=["image", "audio"]))
        self.assertEqual(sorted(asked), ["audio", "image"])

    def test_fresh_replica_skips_network(self):
        fed = self._fed([{"name": "b", "url": "http://10.255.255.1:9", "kind": "langbang", "replicate": True}])
        self.store.kv_set("last_sync:b", time.time())
        t0 = time.time()
        asyncio.run(fed.search(qvec=unit(1), query="q", k=5))
        self.assertLess(time.time() - t0, 0.3)
        self.assertNotIn("b", fed.status)


if __name__ == "__main__":
    unittest.main()
