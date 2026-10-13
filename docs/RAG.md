# RAG node: multimodal semantic search + fleet federation

`rag/` is LangBang's built-in retrieval service. It runs as **its own process**
(`langbang-rag.service`, port **8900**), not inside the chat server, for three reasons:
- torch encodes are CPU-heavy and would stall the run hubs;
- a LangBang restart must not reload a 1.5 GB model;
- the same package deploys unchanged on any box as a federation peer.

Everything lives in one [EmbeddingGemma 2](https://huggingface.co/google/embeddinggemma-2) 768-d
vector space: text, code, PDFs, images, audio and video. A text query finds
pictures, and an image finds similar images and video frames.

## What gets indexed

| source | what | how it gets in |
|---|---|---|
| `home` | the home dir: code/docs/PDF/docx, images, audio (8 s windows), video (keyframes) | nightly rescan (`langbang-rag-scan.timer`, 01:05, runs only inside `rescan_window`), mtime-incremental |
| `output` | `~/Pictures/langbang` generated images; the sidecar prompt is indexed as a caption | pushed by `comfy.generate` |
| `upload` | chat attachments and pasted images (`data/uploads`) | pushed on upload |
| `thread` | each finished chat exchange (`USER: … LANGBANG: …`) | pushed by `runs._produce`; the thread delete removes it |
| `skill` / `memory` | `~/.langbang/skills`, `~/.langbang/memory` | pushed on write; tombstoned on delete |

Files the agent writes with `write_file` / `edit_file` are pushed when the run finishes, and
FILES saves are pushed on save.
Pushes are fire-and-forget (`server/rag.py`): a dead node never delays a run.

## Agent tools

- `semantic_search(query, category, source, scope, top_k)`: category is
  `text|image|video|audio`; source is `home|output|upload|thread|skill|memory`; scope is `fleet|local`.
- `semantic_search_image(path, …)`: reverse image search.

Both return score, kind, absolute path (cited media paths render inline) and snippets.
They can be toggled in CONFIG → tools; client settings are in CONFIG → RAG.

## FILES panel (≈ RAG)

The ✎ FILES path box doubles as a semantic search:
- **Turning it on:** press **≈ RAG** (remembered per browser), or start the query with `?`. `/…` and `~/…` still autocomplete.
- **Searching:** typing searches this box live (~0.15 s); **Enter** adds the fleet (Atlas and peers, up to their timeouts).
- **Results:** hits replace the tree, with thumbnails, score, node and snippet. On Enter, the local hits stay up while the fleet answers. Kind chips multi-select (e.g. img + audio; none = all; sent as `category: "image,audio"`), and 📁 limits hits to the current folder (`path_prefix`); `↰ back` returns to the folder.
- **The box keeps the query** while hits are up: opening a hit doesn't overwrite it, so re-searching is an edit plus Enter.
- **Opening a hit:**
  - local text opens in the editor, scrolled to and selecting the matching chunk;
  - local media opens in the viewer and seeks video/audio to the hit;
  - fleet media streams view-only through `/api/rag/file`;
  - thread hits open the chat.
- **Audio** opens in a visualizer stage (`web/viz.js`, a port of music-sync's orb/bars/radial/particles/plasma/kaleidoscope) with the track's tags, ComfyUI caption/lyrics (from the `prompt` tag), or an "Artist - Title" guess from the file name (`/api/media/info`, `/api/media/cover`; fleet hits are probed through the node). **✨ IMAGES FROM AUDIO** (`/api/media/imagine`, `server/audioart.py`) measures tempo, key, energy and brightness per section with librosa, has the chat model write one prompt per section, and renders them with Qwen-Image (the cover art as image_1 when there is one). The images are recorded in `data/audio-art.json` and play as the stage's backdrop for their section.
- **≈ RELATED** (editor bar) searches by the open image itself, or by the selection or the start of the text.

LangBang serves these through `POST /api/rag/search` and `GET /api/rag/file?p=` (node `/thumbnail`, `/media`, `/proxy` only), so an https tab never touches the node's plain-http port.

## Node API (`rag/server.py`)

Read routes are open to the LAN, like the standalone `:8900` this replaced:
- `/search` (JSON `query` | `vector` | `image_path`, plus `category`/`source`/`path_prefix`/`scope`)
- `/search-image` (multipart)
- `/media`, `/thumbnail` (own indexed files only)
- `/proxy?node=&p=` (a peer's `/media` / `/thumbnail` only)
- `/v1/embeddings` (`mode: query|document`), `/api/embed/{image,audio}`
- `/api/stats`, `/api/peers`, `/healthz`
- `/`, the search UI

Write routes need loopback or `Authorization: Bearer $(cat data/keys/rag_token)`
(the token is generated on first start):
- `POST /index` (`paths` and/or `docs`)
- `DELETE /index?path=|tid=|source=|path_prefix=`
- `POST /api/rescan`
- `GET /replicate/export?since=<seq>`

## Ranking and latency

- **Hybrid name boost:**
  - Rule: a file whose *name* contains the query words gains up to +0.15, scaled by the fraction of words matched. A match only in its folder's name gains +0.04.
  - Why: dense-only ranking put VS Code history junk above `Resume.md` for the one-word query "resume".
  - Cost: the names are held as one joined string scanned with `str.find`, about 10–20 ms over 254k rows.
  - Fleet peers get the query words as `terms`, so they apply the same boost.
- **Searches never queue behind indexing:**
  - The local model lock is a priority lock: query encodes jump the queue, and bulk CPU encodes run in slices of 4.
  - Searches stay at 0.1–0.25 s during a rescan. Before this they measured 17–20 s.
- **Atlas is a shared GPU.** While we index through it, its own searches slow from ~0.1 s to 3–39 s. So:
  - Our index calls pause while a fleet search waits on Atlas (`embedder.quiet`).
  - Only one audio call is in flight at a time, in 2-minute segments.
  - A peer error is retried (3/10/30 s) before falling back to the CPU, which is about 10x slower.
  - The bulk rescan runs only inside `rescan_window` (default `01:00-08:00`, set in node.json). The timer passes `windowed=1`, a run pauses at the end of the window, and the next night resumes it.
  - Order is text → images → video → audio, with audio last because it costs hours of GPU.
- **Atlas fan-out:**
  - A search across all kinds is one parallel call per media kind (~0.1 s).
  - Atlas's 1.3M-chunk document search (~10 s) is queried only for `category=text`, or for every search if the peer sets `"docs_in_all": true`.
- **Audio length:** ffprobe over-reports some AAC lengths (a 4-minute song reads as 2.2 h). Audio embedding therefore stops at the real decoded end. Before this fix, empty tail segments got HTTP 422 from Atlas and then about 1,000 empty CPU windows per song.

## Federation

Peers are configured in `data/rag/node.json`. That file holds the node's own settings; LangBang's
`settings.json` only holds the client side.

```json
{
  "node": "fedora",
  "index_backend": "atlas",
  "peers": [
    {"name": "atlas", "url": "http://192.168.6.181:8092", "kind": "atlas",
     "auth": "basic:tyrel:atlas_web_password", "model_fp": "eg2-197a32965d4b", "timeout": 8},
    {"name": "da36", "url": "http://spark-da36:8900", "kind": "langbang",
     "auth": "bearer:rag_token_da36", "replicate": true}
  ]
}
```

- **Fan-out** (default): `scope=fleet` queries every peer in parallel within its `timeout`
  (default `peer_timeout` 3 s). Results merge by score, deduplicated per `(node, path)`.
  - `langbang` peers receive our query **vector**, so they score in exactly our space. They are
    always asked with `scope=local`, so a query never fans out twice.
  - `atlas` peers get the query text or image bytes.
  - A dead peer costs at most its timeout and never fails the query.
- **Replication** (`"replicate": true`, langbang peers only): every `replicate_interval`
  (600 s), the node pulls the peer's own rows changed since its cursor, tombstones included,
  as base64 fp16. A peer with a fresh replica is answered locally, not over the network,
  and stays searchable while it is down. Previews still proxy to the owning node.
- **One vector space:** every row records `model_fp`, and peers with a different fingerprint are
  refused. `eg2-197a32965d4b` is the sha256 prefix of `google/embeddinggemma-2`'s
  `model.safetensors`. It is byte-identical on fedora and Atlas, and CPU-fp32 vs GPU-bf16 vectors
  measured cos 1.0000.
- `auth` names key **files** in `data/keys/`: `basic:<user>:<file>` or `bearer:<file>`.

### Adding a node to the fleet

```sh
git clone https://github.com/TyrelCB/langbang && cd langbang
uv sync --only-group rag          # just the node: torch CPU + sentence-transformers
echo '{"node":"<name>","roots":["/home/<you>"]}' > data/rag/node.json   # (mkdir -p data/rag)
sudo cp tools/systemd/langbang-rag*.{service,timer} /etc/systemd/system/   # edit User/paths
sudo systemctl daemon-reload && sudo systemctl enable --now langbang-rag langbang-rag-scan.timer
```

Then copy the new node's `data/keys/rag_token` to `data/keys/rag_token_<name>` on each box that
should query it, and add the peer entry there.

## Embedding backends (`rag/embedder.py`)

- `STBackend`: sentence-transformers, in-process. It serves queries and anything local.
- `RemoteBackend`: Atlas-compatible `/v1/embeddings` + `/api/embed/*`. Set
  `"index_backend": "<peer>"` to offload bulk indexing to a GPU box. It falls back to local if the
  peer fails.
- `LlamaCppBackend`: a placeholder for `unsloth/embeddinggemma-2-GGUF` under llama-server
  (`--embeddings --pooling mean`). It is text-only until llama.cpp's mmproj embedding path is
  proven, so media keeps the sentence-transformers backend.

Text must carry the prefix for its role: `task: search result | query: ` for queries and
`title: none | text: ` for documents. Atlas's `/v1/embeddings` defaults to `mode=query`, and the
pre-LangBang indexer relied on that default, so its 129k text vectors were query embeddings of
doubly-prefixed strings. `python -m rag.migrate reembed` rebuilt them as documents.

## Ops

```sh
sudo systemctl restart langbang-rag          # ~15 s: index load 2 s + model warm 10 s
curl -s localhost:8900/api/stats | jq        # counts per node/category/source, rescan state
curl -s -X POST localhost:8900/api/rescan    # walk now, ignoring rescan_window (loopback = authorized)
curl -s -X POST 'localhost:8900/api/rescan?windowed=1'   # as the timer does: pauses outside the window
.venv/bin/python -m unittest -v rag.tests.test_rag
```

- Logs: `/tmp/langbang-rag.log`.
- Data: `data/rag/` (`rag.db`, thumbnails, `excluded-media.txt`, `node.json`).
- Memory: the matrix is held as fp32 (about 3 KB per vector; 254k vectors is about 0.8 GB). That
  is the fastest search (0.14 s/query); fp16-in-RAM cost 3x the latency.
