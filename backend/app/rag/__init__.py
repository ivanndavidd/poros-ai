"""Documentation doc-RAG (Feature 1.2): ingest -> chunk -> embed -> vector store
(SQLite) -> retrieve with tenant+role ACL + citations.

Vector store is pluggable (see retriever); the default is self-contained SQLite +
an offline embedder so it runs on Windows with no extra services.
"""
from .embeddings import embed_texts, embed_query, embedder_name
from .ingest import (
    extract_text, chunk_text, ingest_document, ingest_existing_document,
    reingest_document, reindex_tenant,
)
from .retriever import retrieve

__all__ = ["embed_texts", "embed_query", "embedder_name",
           "extract_text", "chunk_text", "ingest_document", "ingest_existing_document",
           "reingest_document", "reindex_tenant", "retrieve"]
