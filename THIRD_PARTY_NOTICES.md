# Third-party notices

LangBang's own code is Apache-2.0 (see [LICENSE](LICENSE)). The repository also
**contains** the third-party code below, each under its own license. Python
packages listed in `pyproject.toml` / `uv.lock` (LangChain, LangGraph,
deepagents, FastAPI, PyTorch, pocket-tts, …) are installed by the user from
PyPI, not redistributed here; their licenses are permissive (MIT / BSD /
Apache-2.0 / PSF, plus MPL-2.0 for certifi, orjson and tqdm, none modified).

## Bundled JavaScript — `web/vendor/`

| File | Project | Version | License | Copyright |
|---|---|---|---|---|
| `highlight.min.js` | [highlight.js](https://github.com/highlightjs/highlight.js) | 11.11.1 | BSD-3-Clause | © 2006-2024 Josh Goebel and other contributors |
| `marked.min.js` | [marked](https://github.com/markedjs/marked) | 15.0.12 | MIT | © 2011-2025 Christopher Jeffrey |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) | 3.2.7 | Apache-2.0 OR MPL-2.0 | © Cure53 and other contributors |

The original license headers are kept at the top of each file, unmodified.

## SGLang — `tools/sglang-patches/`

`qwen3_coder_detector.orig.py` (unmodified) and `qwen3_coder_detector.patched.py`
/ `qwen3_coder_tagless_param.patch` (modified; changes marked `LANGBANG PATCH`)
come from [SGLang](https://github.com/sgl-project/sglang)'s
`python/sglang/srt/function_call/qwen3_coder_detector.py` (image
`lmsysorg/sglang@sha256:9d2a843c…`), © The SGLang Authors, licensed under the
[Apache License 2.0](https://github.com/sgl-project/sglang/blob/main/LICENSE).

## Generated assets — `web/sounds/`

Generated with `stabilityai/stable-audio-3-small-sfx` under the Stability AI
Community License (outputs owned by the generator's user). This Stability AI
Model is licensed under the Stability AI Community License, Copyright ©
Stability AI Ltd. All Rights Reserved. **Powered by Stability AI.**
