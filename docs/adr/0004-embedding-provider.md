# ADR 0004: Embedding provider

Status: accepted (2026-11-23)

## Context

SPEC 4.4 requires embeddings behind an `EmbeddingProvider` interface, with a provider chosen here and swappable. Anthropic does not offer an embeddings endpoint in the `anthropic` SDK, so a second source is needed. Memory text is derived from personal and Synaption mail, and personal mail is partly in Spanish, so the model must be multilingual.

## Decision

1. Default implementation: a **local model through `fastembed`** (ONNX runtime, CPU), running inside each space's worker. Verified 2026-11-23: `fastembed` 0.9.1 ships `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` and `intfloat/multilingual-e5-large`.
2. Starting model: `paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions), the smallest of the three, because the VPS is shared with the whole stack. This is a placeholder choice: in M3 I will measure memory, latency and retrieval quality on a small synthetic English/Spanish set for all three and replace it if a larger one is clearly better within the memory measured in M0.
3. Every stored vector records `embedding_model` and `embedding_dim`. Queries only compare vectors of the configured model. Changing model means a new vector column (pgvector columns have a fixed dimension) and a resumable re-embed workflow; old vectors are dropped after the switch.
4. The interface is `embed(texts: list[str]) -> list[list[float]]` plus `model_id` and `dim`; a deterministic fake is used in unit tests.

## Alternatives considered

- *Hosted embeddings (for example Voyage AI or OpenAI)*: likely better quality and no local memory cost, but adds another vendor that receives text derived from personal mail, another secret, and a network dependency for memory writes. Can be added behind the same interface later; local was chosen (PLAN section 6, decision 12).
- *Self-hosting a larger model with a GPU*: not available on a single VPS.

## Consequences

- No memory text leaves the host for embedding.
- The model files must be provisioned into the worker image at build time (not fetched lazily at runtime), so a cold start cannot fail on a download.

## How it will be verified

- M3 measurement table (memory, latency per batch, retrieval recall at k on the synthetic set) in the M3 PR.
- Test that a query never compares vectors with a different `embedding_model`.
