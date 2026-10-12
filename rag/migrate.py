"""One-off migrations for the RAG node.

  python -m rag.migrate import ~/projects/embeddinggemma-2/rag.db
      Copy the standalone :8900 index into data/rag/rag.db (node = this node,
      source inferred from path). Old dir is left untouched as a backup.

  python -m rag.migrate reembed [--batch 32]
      Re-embed imported text chunks as DOCUMENTS. The old indexer posted
      "title: none | text: <chunk>" to Atlas, whose /v1/embeddings defaults to
      mode=query — so every stored text vector was a query embedding of a
      doubly-prefixed string (verified: cos 1.0 vs that, 0.989 vs the proper
      document embedding). Resumable (kv reembed_cursor); uses index_backend.
"""
import argparse
import os
import shutil
import sqlite3
import sys
import time

import numpy as np

from . import config
from .store import Store


def _log(m):
    print(m, flush=True)


def do_import(old_db):
    s = config.load()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    store = Store(os.path.join(config.DATA_DIR, "rag.db"), s["node"])
    if store.max_seq():
        sys.exit("data/rag/rag.db already has rows — refusing to import twice")
    src = sqlite3.connect(f"file:{os.path.expanduser(old_db)}?mode=ro", uri=True)
    total = src.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
    _log(f"importing {total} rows from {old_db}")
    seq = 0
    t0 = time.time()
    cur = src.execute("SELECT path, mtime, category, chunk_idx, text, emb, position FROM chunks ORDER BY id")
    con = store.con
    while True:
        batch = cur.fetchmany(20000)
        if not batch:
            break
        rows = []
        for path, mtime, cat, ci, text, emb, pos in batch:
            seq += 1
            rows.append((s["node"], path, ci, mtime, None, cat, config.source_for(path, s), text, emb,
                         pos or 0, None, s["model_fp"], seq))
        con.executemany(
            """INSERT INTO chunks(node,path,chunk_idx,mtime,size,category,source,text,emb,position,meta,
                   model_fp,seq,deleted) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,0)
               ON CONFLICT(node,path,chunk_idx) DO NOTHING""", rows)
        con.commit()
        _log(f"  {seq}/{total}")
    store.kv_set("seq", seq)
    store.kv_set("imported_from", os.path.abspath(os.path.expanduser(old_db)))
    store.kv_set("reembed_cursor", 0)
    old_dir = os.path.dirname(os.path.abspath(os.path.expanduser(old_db)))
    ex = os.path.join(old_dir, "excluded-media.txt")
    if os.path.exists(ex):
        shutil.copy2(ex, os.path.join(config.DATA_DIR, "excluded-media.txt"))
    th = os.path.join(old_dir, "thumbnails")
    if os.path.isdir(th):  # same cache-key formula: copies stay valid
        shutil.copytree(th, os.path.join(config.DATA_DIR, "thumbnails"), dirs_exist_ok=True)
    _log(f"done in {time.time() - t0:.0f}s; next: python -m rag.migrate reembed")


def do_reembed(batch):
    from .embedder import Embedder, RemoteBackend, STBackend
    from .federation import auth_header
    s = config.load()
    store = Store(os.path.join(config.DATA_DIR, "rag.db"), s["node"])
    local = STBackend(s["model"], threads=s.get("threads"))
    name = s.get("index_backend", "local")
    peer = next((p for p in s.get("peers", []) if p["name"] == name), None)
    backend = RemoteBackend(peer["url"], auth_header(peer).get("Authorization", "")) if peer else local
    emb = Embedder(local, backend, log=_log)
    cur_id = int(store.kv_get("reembed_cursor", 0) or 0)
    rcon = store.rcon()
    total = rcon.execute("SELECT COUNT(*) FROM chunks WHERE node=? AND category='text' AND deleted=0 AND id>?",
                         (s["node"], cur_id)).fetchone()[0]
    _log(f"re-embedding {total} text chunks via {backend.name} from id>{cur_id}")
    done, t0 = 0, time.time()
    while True:
        rows = rcon.execute(
            """SELECT id, text FROM chunks WHERE node=? AND category='text' AND deleted=0 AND id>?
               AND meta IS NULL ORDER BY id LIMIT ?""", (s["node"], cur_id, batch)).fetchall()
        if not rows:
            break
        texts = [t or "" for _, t in rows]
        vecs = emb.docs(texts)
        with store.wlock:
            seq = store._take_seq(len(rows))
            store.con.executemany("UPDATE chunks SET emb=?, seq=? WHERE id=?",
                                  [(np.asarray(v, dtype=np.float16).tobytes(), seq + i, rid)
                                   for i, ((rid, _), v) in enumerate(zip(rows, vecs))])
            cur_id = rows[-1][0]
            store.con.execute("INSERT INTO kv(k,v) VALUES('reembed_cursor',?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                              (str(cur_id),))
            store.con.commit()
        done += len(rows)
        if done % (batch * 50) < batch:
            rate = done / max(1e-6, time.time() - t0)
            _log(f"  {done}/{total} ({rate:.0f}/s, ETA {(total - done) / max(rate, 1e-6) / 60:.0f} min)")
    store.kv_set("reembed_done", time.time())
    _log(f"re-embed complete: {done} chunks in {(time.time() - t0) / 60:.1f} min")


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("import"); a.add_argument("old_db")
    b = sub.add_parser("reembed"); b.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()
    if args.cmd == "import":
        do_import(args.old_db)
    else:
        do_reembed(args.batch)


if __name__ == "__main__":
    main()
