"""Vector store: SQLite rows (fp16 BLOBs) + an in-memory fp32 matrix searched by numpy.

Every write takes the next value of a monotonic `seq`; deletes are tombstones
(deleted=1, emb NULL) so both the in-process matrix and replication followers
catch up incrementally from a cursor instead of rescanning. Rows are keyed by
(node, path, chunk_idx): replicated peers' rows live beside ours under their
own node name.
"""
import base64
import json
import sqlite3
import threading
import time

import numpy as np

from .embedder import DIM

SCHEMA = """
CREATE TABLE IF NOT EXISTS chunks(
    id INTEGER PRIMARY KEY,
    node TEXT NOT NULL,
    path TEXT NOT NULL,
    chunk_idx INTEGER NOT NULL,
    mtime REAL,
    size INTEGER,
    category TEXT,
    source TEXT,
    text TEXT,
    emb BLOB,
    position REAL DEFAULT 0,
    meta TEXT,
    model_fp TEXT,
    seq INTEGER NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0,
    UNIQUE(node, path, chunk_idx));
CREATE INDEX IF NOT EXISTS chunks_seq ON chunks(seq);
CREATE INDEX IF NOT EXISTS chunks_path ON chunks(path);
CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY, v TEXT);
"""

COLS = ("node", "path", "chunk_idx", "mtime", "size", "category", "source", "text",
        "emb", "position", "meta", "model_fp", "deleted")


def connect(path, readonly=False):
    if readonly:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=60, check_same_thread=False)
    else:
        con = sqlite3.connect(path, timeout=60, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=60000")
    return con


class Store:
    def __init__(self, path, node):
        self.path = path
        self.node = node
        self.con = connect(path)
        self.con.executescript(SCHEMA)
        self.con.commit()
        self.wlock = threading.Lock()
        self._tls = threading.local()
        self.cache = MatrixCache(self)

    def rcon(self):
        """Per-thread read connection: reads must never interleave with the
        writer connection's open transaction."""
        c = getattr(self._tls, "con", None)
        if c is None:
            c = self._tls.con = connect(self.path)
        return c

    # ---- seq / kv -------------------------------------------------------
    def kv_get(self, k, default=None):
        r = self.con.execute("SELECT v FROM kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def kv_set(self, k, v):
        with self.wlock:
            self.con.execute("INSERT INTO kv(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                             (k, str(v)))
            self.con.commit()

    def _begin(self):
        """Take SQLite's write lock BEFORE reading the seq counter: another
        process (rag.migrate reembed) may write the same file, and two writers
        reading seq first would hand out overlapping ranges — the matrix
        cache would then skip a whole batch."""
        if not self.con.in_transaction:
            self.con.execute("BEGIN IMMEDIATE")

    def _take_seq(self, n):
        """Caller holds wlock; opens (or joins) an IMMEDIATE write txn."""
        self._begin()
        r = self.con.execute("SELECT v FROM kv WHERE k='seq'").fetchone()
        cur = int(r[0]) if r else int(self.con.execute(
            "SELECT COALESCE(MAX(seq),0) FROM chunks").fetchone()[0])
        self.con.execute("INSERT INTO kv(k,v) VALUES('seq',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                         (str(cur + n),))
        return cur + 1

    def max_seq(self):
        return int(self.rcon().execute("SELECT COALESCE(MAX(seq),0) FROM chunks").fetchone()[0])

    # ---- writes ---------------------------------------------------------
    def put_file(self, path, rows, *, category, source, mtime=None, size=None, meta=None,
                 model_fp="", node=None):
        """Replace every chunk of (node, path) with `rows`:
        [(chunk_idx, text, vec768, position)]. Chunks the new version no longer
        has become tombstones."""
        node = node or self.node
        meta_s = json.dumps(meta) if meta else None
        with self.wlock:
            try:
                self._begin()
                old = {r[0] for r in self.con.execute(
                    "SELECT chunk_idx FROM chunks WHERE node=? AND path=? AND deleted=0", (node, path))}
                new = {int(r[0]) for r in rows}
                gone = sorted(old - new)
                seq = self._take_seq(len(rows) + len(gone))
                for i, (ci, text, vec, pos) in enumerate(rows):
                    emb = np.asarray(vec, dtype=np.float16).tobytes()
                    self.con.execute(
                        """INSERT INTO chunks(node,path,chunk_idx,mtime,size,category,source,text,emb,
                               position,meta,model_fp,seq,deleted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)
                           ON CONFLICT(node,path,chunk_idx) DO UPDATE SET mtime=excluded.mtime,
                               size=excluded.size, category=excluded.category, source=excluded.source,
                               text=excluded.text, emb=excluded.emb, position=excluded.position,
                               meta=excluded.meta, model_fp=excluded.model_fp, seq=excluded.seq,
                               deleted=0""",
                        (node, path, int(ci), mtime, size, category, source, text, emb,
                         float(pos or 0), meta_s, model_fp, seq + i))
                for j, ci in enumerate(gone):
                    self.con.execute(
                        "UPDATE chunks SET deleted=1, emb=NULL, text=NULL, seq=? WHERE node=? AND path=? AND chunk_idx=?",
                        (seq + len(rows) + j, node, path, ci))
                self.con.commit()
            except BaseException:
                self.con.rollback()
                raise
        return len(rows)

    def _tombstone(self, where, args):
        with self.wlock:
            try:
                self._begin()
                ids = [r[0] for r in self.con.execute(
                    f"SELECT id FROM chunks WHERE deleted=0 AND {where}", args)]
                if ids:
                    seq = self._take_seq(len(ids))
                    self.con.executemany("UPDATE chunks SET deleted=1, emb=NULL, text=NULL, seq=? WHERE id=?",
                                         [(seq + i, rid) for i, rid in enumerate(ids)])
                self.con.commit()
                return len(ids)
            except BaseException:
                self.con.rollback()
                raise

    def delete_path(self, path, node=None):
        return self._tombstone("node=? AND path=?", (node or self.node, path))

    def delete_where(self, source=None, tid=None, path_prefix=None, node=None):
        conds, args = ["node=?"], [node or self.node]
        if source:
            conds.append("source=?"); args.append(source)
        if tid:
            conds.append("json_extract(meta,'$.tid')=?"); args.append(tid)
        if path_prefix:
            conds.append("substr(path,1,?)=?"); args += [len(path_prefix), path_prefix]
        if len(conds) == 1:
            raise ValueError("refusing to delete a whole node")
        return self._tombstone(" AND ".join(conds), args)

    def apply_replica(self, rows):
        """Upsert rows exported by a peer (each carries its own node). Local seq
        is reassigned — cursors are per-store, never shared."""
        if not rows:
            return 0
        with self.wlock:
            try:
                seq = self._take_seq(len(rows))
                for i, r in enumerate(rows):
                    if r["node"] == self.node:
                        continue  # never let a peer overwrite our own rows
                    emb = None if r.get("deleted") or r.get("emb") is None else (
                        base64.b64decode(r["emb"]) if isinstance(r["emb"], str)
                        else np.asarray(r["emb"], dtype=np.float16).tobytes())
                    if emb is not None and len(emb) != DIM * 2:
                        raise ValueError(f"bad vector size {len(emb)} for {r['path']}")
                    self.con.execute(
                        """INSERT INTO chunks(node,path,chunk_idx,mtime,size,category,source,text,emb,
                               position,meta,model_fp,seq,deleted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(node,path,chunk_idx) DO UPDATE SET mtime=excluded.mtime,
                               size=excluded.size, category=excluded.category, source=excluded.source,
                               text=excluded.text, emb=excluded.emb, position=excluded.position,
                               meta=excluded.meta, model_fp=excluded.model_fp, seq=excluded.seq,
                               deleted=excluded.deleted""",
                        (r["node"], r["path"], int(r["chunk_idx"]), r.get("mtime"), r.get("size"),
                         r.get("category"), r.get("source"), r.get("text"), emb,
                         float(r.get("position") or 0), json.dumps(r["meta"]) if r.get("meta") else None,
                         r.get("model_fp") or "", seq + i, 1 if r.get("deleted") else 0))
                self.con.commit()
            except BaseException:
                self.con.rollback()
                raise
        return len(rows)

    # ---- reads ----------------------------------------------------------
    def export(self, since, limit=2000):
        """Own-node rows (tombstones included) with seq > since, oldest first.
        Only our own rows: re-exporting replicas would loop through the fleet."""
        cur = self.rcon().execute(
            f"SELECT seq,{','.join(COLS)} FROM chunks WHERE node=? AND seq>? ORDER BY seq LIMIT ?",
            (self.node, int(since), int(limit)))
        out = []
        for r in cur:
            d = dict(zip(("seq",) + COLS, r))
            # base64 of the stored fp16 bytes: ~2 KB/row on the wire vs ~8 KB as JSON floats
            d["emb"] = None if d["emb"] is None else base64.b64encode(d["emb"]).decode()
            d["meta"] = json.loads(d["meta"]) if d["meta"] else None
            out.append(d)
        return out

    def known_mtimes(self, node=None):
        return {p: m for p, m in self.rcon().execute(
            "SELECT path, MAX(mtime) FROM chunks WHERE node=? AND deleted=0 GROUP BY path",
            (node or self.node,))}

    def path_mtime(self, path, node=None):
        r = self.rcon().execute("SELECT MAX(mtime) FROM chunks WHERE node=? AND path=? AND deleted=0",
                                (node or self.node, path)).fetchone()
        return r[0] if r else None

    def is_indexed(self, path, node=None):
        return self.rcon().execute("SELECT 1 FROM chunks WHERE node=? AND path=? AND deleted=0 LIMIT 1",
                                (node or self.node, path)).fetchone() is not None

    def texts(self, ids):
        if not ids:
            return {}
        q = ",".join("?" * len(ids))
        return {i: (t, m) for i, t, m in self.rcon().execute(
            f"SELECT id, text, meta FROM chunks WHERE id IN ({q})", list(ids))}

    def stats(self):
        rows = self.rcon().execute(
            """SELECT node, category, source, COUNT(DISTINCT path), COUNT(*) FROM chunks
               WHERE deleted=0 GROUP BY 1,2,3""").fetchall()
        return [dict(zip(("node", "category", "source", "files", "vectors"), r)) for r in rows]

    def search(self, qvec, k=10, **filters):
        return self.cache.search(qvec, k, **filters)


class MatrixCache:
    """float32 matrix of live vectors, refreshed from rows whose seq moved past
    our cursor. Slots are reused by row id; tombstoned rows just go dead.
    fp32 in RAM (~3 KB/vector) is deliberate: converting an fp16 matrix per
    query cost 0.43 s/query at 254k rows vs 0.16 s for a BLAS matvec."""

    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.seq = 0
        self.n = 0
        self.mat = np.zeros((0, DIM), dtype=np.float32)
        self.alive = np.zeros(0, dtype=bool)
        self.slot_of = {}
        self.ids = []
        self.paths = []
        self.nodes = []
        self.cats = []
        self.sources = []
        self.positions = []
        self.last_refresh = 0.0

    def _grow(self, need):
        cap = len(self.mat)
        if need <= cap:
            return
        new = max(need, int(cap * 1.5) + 1024)
        m = np.zeros((new, DIM), dtype=np.float32)
        m[:cap] = self.mat
        a = np.zeros(new, dtype=bool)
        a[:cap] = self.alive
        self.mat, self.alive = m, a

    def refresh(self):
        with self.lock:
            con = self.store.rcon()
            top = int(con.execute("SELECT COALESCE(MAX(seq),0) FROM chunks").fetchone()[0])
            if top <= self.seq:
                return 0
            changed = 0
            cur = con.execute(
                "SELECT id, node, path, category, source, position, emb, deleted, seq FROM chunks "
                "WHERE seq>? ORDER BY seq", (self.seq,))
            while True:
                batch = cur.fetchmany(20000)
                if not batch:
                    break
                self._grow(self.n + len(batch))
                for rid, node, path, cat, src, pos, emb, deleted, seq in batch:
                    slot = self.slot_of.get(rid)
                    if deleted or emb is None:
                        if slot is not None:
                            self.alive[slot] = False
                        continue
                    if slot is None:
                        slot = self.n
                        self.n += 1
                        self.slot_of[rid] = slot
                        self.ids.append(rid); self.paths.append(path); self.nodes.append(node)
                        self.cats.append(cat); self.sources.append(src); self.positions.append(pos or 0)
                    else:
                        self.paths[slot] = path; self.nodes[slot] = node; self.cats[slot] = cat
                        self.sources[slot] = src; self.positions[slot] = pos or 0
                    self.mat[slot] = np.frombuffer(emb, dtype=np.float16)
                    self.alive[slot] = True
                    changed += 1
            self.seq = top
            self.last_refresh = time.time()
            return changed

    def _mask(self, category=None, source=None, node=None, nodes_exclude=(), path_prefix=None):
        m = self.alive[: self.n].copy()

        def by(vals, want):
            want = {want} if isinstance(want, str) else set(want)
            return np.fromiter((v in want for v in vals), dtype=bool, count=self.n)
        if category:
            m &= by(self.cats, category)
        if source:
            m &= by(self.sources, source)
        if node:
            m &= by(self.nodes, node)
        if nodes_exclude:
            m &= ~by(self.nodes, nodes_exclude)
        if path_prefix:
            m &= np.fromiter((p.startswith(path_prefix) for p in self.paths), dtype=bool, count=self.n)
        return m

    def search(self, qvec, k=10, **filters):
        """Best chunk per (node, path), top-k by cosine (vectors are unit)."""
        self.refresh()
        with self.lock:
            if self.n == 0:
                return []
            q = np.asarray(qvec, dtype=np.float32)
            sims = self.mat[: self.n] @ q
            if any(filters.values()):
                sims[~self._mask(**filters)] = -2
            else:
                sims[~self.alive[: self.n]] = -2
            # over-fetch: one long video/audio can own hundreds of chunks
            cand = min(self.n, max(k * 25, 200))
            idx = np.argpartition(-sims, cand - 1)[:cand]
            idx = idx[np.argsort(-sims[idx])]
            seen, out = set(), []
            for i in idx:
                if sims[i] <= -2:
                    break
                key = (self.nodes[i], self.paths[i])
                if key in seen:
                    continue
                seen.add(key)
                out.append({"id": self.ids[i], "node": self.nodes[i], "path": self.paths[i],
                            "category": self.cats[i], "source": self.sources[i],
                            "position": float(self.positions[i] or 0), "score": round(float(sims[i]), 4)})
                if len(out) >= k:
                    break
        texts = self.store.texts([r["id"] for r in out])
        for r in out:
            t, meta = texts.get(r["id"], (None, None))
            r["snippet"] = (t or "")[:300]
            if meta:
                r["meta"] = json.loads(meta)
        return out
