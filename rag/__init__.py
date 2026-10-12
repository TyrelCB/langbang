"""LangBang RAG node: multimodal EmbeddingGemma 2 index + fleet federation.

Runs as its own process (langbang-rag.service, :8900) — torch encodes must not
share the LangBang event loop, and the same package deploys unchanged on any
fleet box as a federation peer.
"""
