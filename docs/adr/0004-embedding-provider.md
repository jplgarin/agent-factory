# ADR 0004: Embedding provider

Status: accepted (2026-11-23)

## Context

SPEC 4.4 requires embeddings behind an `EmbeddingProvider` interface, with a provider chosen here and swappable. Anthropic does not offer an embeddings endpoint in the `anthropic` SDK, so a second source is needed. Memory text is derived from personal and Synaption mail, and personal mail is partly in Spanish, so the model must be multilingual.

## Decision

1. Default implementation: a **local model through `fastembed`** (ONNX runtime, CPU), running inside each space's worker. Verified 2026-11-23: `fastembed` 0.9.1 ships `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, `sentence-transformers/paraphrase-multilingual-mpnet-base-v2` and `intfloat/multilingual-e5-large`.
2. Starting model: `paraphrase-multilingual-MiniLM-L12-v2` (384 dimensions), the smallest of the three, because the VPS is shared with the whole stack. This is a placeholder choice: in M3 I will measure memory, latency and retrieval quality on a small synthetic English/Spanish set for all three and replace it if a larger one is clearly better within the memory measured in M0.
3. Every stored vector records `embedding_model` and `embedding_dim`. Queries only compare vectors of the configured model. Changing model means a new vector column (pgvector columns have a fixed dimension) and a resumable re-embed workflow; old vectors are dropped after the switch.
4. The interface is `embed(texts: list[str]) -> list[list[float]]` plus `model_id` and `dim`; a deterministic fake is used in unit tests.

## Amendments from M3 (2026-11-23)

Measured and decided while implementing M3. The numbers and the method are in
[docs/MEMORY.md](../MEMORY.md#model-comparison).

1. **The starting model stays.** On the synthetic English and Spanish set,
   `paraphrase-multilingual-mpnet-base-v2` ranked the right fact first in 36 of 40 questions and
   `paraphrase-multilingual-MiniLM-L12-v2` in 31 of 40, at 1938 MB against 717 MB of peak memory
   per process. With 40 questions the 95% intervals overlap, so the larger model is not clearly
   better, and decision 2 keeps the smaller one. `multilingual-e5-large` was slower, larger and
   not better across languages.
2. **The interface has two methods**, `embed_documents(texts)` and `embed_query(text)`, instead
   of the single `embed(texts)` of decision 4. Some models, `multilingual-e5-large` among them,
   expect a different prefix for queries and for stored text; one method could not serve them.
3. **The model is pinned to a commit** of its Hugging Face repository, and `model_id` includes
   it. Decision 3 compares only vectors of the configured model; without the pin, a rebuilt image
   could carry different files under the same name.
4. **The build verifies the consequence below**: after downloading the model, the image build
   loads it as the run-time user with the network switched off, and fails otherwise.

## Alternatives considered

- *Hosted embeddings (for example Voyage AI or OpenAI)*: likely better quality and no local memory cost, but adds another vendor that receives text derived from personal mail, another secret, and a network dependency for memory writes. Can be added behind the same interface later; local was chosen (PLAN section 6, decision 12).
- *Self-hosting a larger model with a GPU*: not available on a single VPS.

## Consequences

- No memory text leaves the host for embedding.
- The model files must be provisioned into the worker image at build time (not fetched lazily at runtime), so a cold start cannot fail on a download.

## How it will be verified

- M3 measurement table (memory, latency per batch, retrieval recall at k on the synthetic set) in the M3 PR. Done: [docs/MEMORY.md](../MEMORY.md#model-comparison).
- Test that a query never compares vectors with a different `embedding_model`. Done: `tests/db/test_memory.py`.
