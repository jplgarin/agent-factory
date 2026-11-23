# Memory and context

How an agent remembers across tasks and what it reads at the start of a run (SPEC 4.3 and 4.4,
M3 in the plan). Memory lives in Postgres under the same isolation as every other table
([DATABASE.md](DATABASE.md)). The context builder is a pure function in `core/`.

| Part | Where |
| --- | --- |
| Models, `MemoryWriter`, `MemoryReader`, `EmbeddingProvider` | `core/memory/` |
| Postgres stores | `db/memory.py`, migration `0002` |
| Local embedding model | `integrations/embeddings/` |
| Context builder, token estimate, profile and preferences schemas | `core/context/` |
| Fencing of untrusted text | `core/untrusted.py` |
| Loader for profile, preferences and prompts | `apps/shared/space_config.py` |

M3 delivers these parts and their tests. Calling them from a running agent (loading the model in
the worker, building the context in an activity, appending the audit event) is part of M4.

## Two kinds of memory

**Semantic memory** (`memory_items`) holds facts and notes of at most 2000 characters. Each item
records its kind, its source (`owner`, `agent`, `email`, `web` or `document`, plus a reference), the
agent instance that wrote it, a confidence between 0 and 1, whether it is trusted, an optional
expiry and an embedding.

**Episodic memory** (`memory_episodes`) holds one short summary per run, filed under an optional
topic key such as `thread:18c2f0a9`. The task, the agent and the outcome are not copied: they are
read from the run the episode belongs to, so they cannot disagree with it.

Working memory, the state of a run in progress, lives in the Temporal workflow and arrives in M4.
Profile and preferences are not memory: they are files (see below).

## Provenance and trust

Every item and episode is either `trusted` or `untrusted`. Untrusted text was written by someone
else or derived from something they wrote: an email body, a web page, an attachment.

| Rule | Enforced by |
| --- | --- |
| Text from an `email`, `web` or `document` source must be marked untrusted. | `MemoryDraft` validation, and a check constraint on `memory_items`. |
| Text written by a run that has read untrusted content (a tainted run, PLAN G7) must be marked untrusted. | `MemoryWriter`. The caller passes `tainted`; there is no default. |
| A write that claims more trust than its origin allows is refused, not downgraded. | `MemoryWriter` raises `ProvenanceError`. |
| A writer or reader works in one space. Drafts and results from another space are refused. | `MemoryWriter`, `MemoryReader`, and row-level security underneath. |
| Stored memory cannot be edited or deleted by a worker. | Space roles have `SELECT` and `INSERT` only. |

`MemoryWriter` is the only write path and `MemoryReader` the only read path. A reader that must
stay untainted asks for trusted text only (`include_untrusted=False`); the reader checks the answer
and raises if a store returns anything else.

Known limit: the database cannot check the `tainted` flag yet, because runs do not record it
until M2. A worker that lies about it can store untrusted text as trusted `agent` text. When
`runs` has the flag, a constraint can tie an episode's trust to its run.

## Search

`MemoryReader.search` embeds the question and returns the closest live items of its space, closest
first. The query:

- compares only vectors of the configured embedding model (`embedding_model` is stored per row),
- skips expired items, and untrusted ones when asked to,
- returns each text once: if the same text is stored as trusted and as untrusted, the trusted row
  wins,
- orders by cosine distance and then by ID, so equal inputs give equal results.

The search is exact: there is no approximate index. With a few thousand items per space an exact
scan is fast and always returns the true nearest items, which keeps the context reproducible.
Measured on a 2 vCPU machine with the M3 schema, 384 dimensions, 8 results:

| Items per space | Median | Slowest of 10 |
| --- | --- | --- |
| 1,000 | 3 ms | 40 ms |
| 10,000 | 22 ms | 26 ms |
| 50,000 | 89 ms | 98 ms |

An index per space (HNSW, partial on `space`) becomes worth its approximations somewhere above
50,000 items per space. That is a decision for when a space gets there.

Writes are idempotent. Writing a draft again with the same ID returns the stored item, which is
what a retried Temporal activity needs. Writing text that is already stored, at least as trusted
and not expiring sooner, returns the existing item without embedding it again. A run has one
episode; writing it again returns the first.

## Embeddings

`EmbeddingProvider` has `embed_documents`, `embed_query`, `model_id` and `dim`. Documents and
queries are embedded separately because some models expect a different prefix for each. The
default implementation, `FastembedProvider`, runs a local ONNX model through fastembed, so no
memory text leaves the host (ADR 0004). It loads the model from a directory and never downloads at
run time.

The model that ships is `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384
dimensions). It is pinned to one commit of its Hugging Face repository in
`integrations/embeddings/models.py`, as images and actions are pinned to digests: a model whose
files change gives different vectors. `model_id` includes the commit
(`...MiniLM-L12-v2@faf4aa422582`) and is stored with every vector, so vectors of different model
files are never compared.

The image build downloads the model at that commit and then loads it as the run-time user with
the network switched off. The build fails if that does not work, so a container cannot start
without a usable model. `HF_HUB_OFFLINE=1` is set in the image.

The model reads at most 128 tokens, a few hundred characters. Longer items are stored
whole but are found by how they begin.

### Model comparison

ADR 0004 named three candidates and asked for a measurement before settling. Measured on
2026-11-23 on a GitHub-hosted runner with 2 threads, by the `Embedding benchmark` workflow
(`scripts/embedding_benchmark.py`). The set is `evals/embeddings/memory_recall.yaml`: 40 synthetic
facts, 20 in English and 20 in Spanish, and 40 questions with one right fact each. Half of the
questions are asked in the other language than the fact. "Top 1" counts how often the right fact
ranked first.

| Model | Dim | Files | Peak memory | Query | Top 1 | Top 3 | Top 1, same language | Top 1, other language |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `paraphrase-multilingual-MiniLM-L12-v2` (ships) | 384 | 252 MB | 717 MB | 6 ms | 31/40 | 36/40 | 17/20 | 14/20 |
| `paraphrase-multilingual-mpnet-base-v2` | 768 | 1127 MB | 1938 MB | 21 ms | 36/40 | 40/40 | 18/20 | 18/20 |
| `multilingual-e5-large` | 1024 | 4489 MB | 2058 MB | 73 ms | 32/40 | 37/40 | 20/20 | 12/20 |

Peak memory is the peak of one process that downloads the model, loads it and embeds the set.
Each worker loads its own copy, so the stack pays it twice. Files is the size of the download
cache; for `multilingual-e5-large` that is about twice the 2.24 GB fastembed lists, which was not
looked into.

What the numbers say:

- `mpnet-base-v2` ranked the right fact first more often than MiniLM (36 against 31 of 40) and
  always had it in the top 3. It costs 2.7 times the memory, about 1.2 GB more per worker.
- With 40 questions that difference is suggestive, not conclusive: the 95% intervals are 62% to
  88% for 31/40 and 77% to 96% for 36/40.
- `multilingual-e5-large` is the slowest and largest and was not better across languages.

MiniLM stays the default. ADR 0004 replaces it only if a larger model is clearly better within
the memory budget, and this set does not show that. If recall proves weak in real use,
`mpnet-base-v2` is the upgrade: it needs a 768-dimension column and a re-embed of the stored
items.

## Context

`build_context` takes a `ContextRequest` and returns a `BuiltContext`. It does no I/O: the caller
loads the prompt, profile, preferences, memory and tools first. The same request always gives the
same blocks and the same hash, whatever order the memory and tools were passed in.

Blocks, in this order (SPEC 4.3):

| Block | Content | Over budget |
| --- | --- | --- |
| `system_prompt` | The agent's prompt file, unchanged. | Error |
| `profile` | Name, role, languages, time zone and facts. | Error |
| `preferences` | Style settings and standing rules. | Error |
| `memory` | Recalled items, then summaries of earlier related runs. | Lowest-ranked entries are dropped |
| `task_brief` | The typed brief as JSON. | Error |
| `tools` | Name, description and input schema of each tool. | Error |

Each block has its own token budget (`ContextBudget`). Instructions are never cut to fit: a prompt,
brief or tool list that does not fit is a configuration error and raises `ContextBudgetError`.
Memory is the elastic block. Items (closest first) and episodes (newest first) are added in turn
while the block fits; each list stops at its first entry that does not fit. What was left out is
reported in `dropped_memories` and `dropped_episodes`.

Untrusted entries are placed inside `<untrusted_data source="...">` tags, with one notice that text
inside those tags is data. Text cannot close its own fence: any `<untrusted_data` or
`</untrusted_data` inside it is escaped. If a context includes untrusted text,
`contains_untrusted` is true and the run that reads it must be treated as tainted. The fence
lowers the chance that injected instructions are followed. It is not the control: the control is
the policy engine denying tools with external effect to tainted runs (PLAN G7).

The task brief is rendered as it is given and is not fenced: it carries the instructions for the
run. A brief written by a run that had read untrusted text makes the new run tainted from the
start. That flag travels with the delegation (G7); the builder cannot see it.

### Token estimate

Budgets are counted by a `TokenCounter`. The default, `Utf8TokenEstimator`, counts one token per
three UTF-8 bytes. It needs no network call and no vendor tokenizer, which keeps the builder
deterministic. It is an estimate that runs high for prose. Context budgets guard against runaway
context; the hard token budget of a task (SPEC 2.8) is charged from the usage the model provider
reports.

### Audit record

`BuiltContext.audit_draft()` gives the `context_assembled` audit event (SPEC 4.7). The payload has
IDs and hashes only, never content:

```json
{
  "agent_id": "email.personal",
  "agent_version": "1.2.0",
  "context_sha256": "f6b9...",
  "tokens": 612,
  "token_counter": "utf8-bytes/3",
  "contains_untrusted": true,
  "blocks": [{"kind": "system_prompt", "sha256": "...", "tokens": 16}],
  "sources": {
    "system_prompt": {"path": "prompts/email.md", "sha256": "..."},
    "profile": {"path": "spaces/personal/profile.yaml", "sha256": "..."},
    "preferences": {"path": "spaces/personal/preferences.yaml", "sha256": "..."}
  },
  "memories": [{"id": "...", "sha256": "...", "trust": "trusted"}],
  "episodes": [{"id": "...", "sha256": "...", "trust": "trusted"}],
  "dropped_memories": ["..."],
  "dropped_episodes": [],
  "tools": [{"name": "mail.search", "sha256": "..."}]
}
```

`context_sha256` covers the text of the six blocks. `sources` holds the hash of each file's bytes,
so a run states which version of the prompt, profile and preferences it used.

## Profile, preferences and prompts

These are files in the private configuration directory (`AF_CONFIG_DIR`), read each time a context
is built and never copied into the database, so there is one source of truth (PLAN G15).
`load_space_config` reads `spaces/<space>/profile.yaml` and `preferences.yaml`, and
`load_system_prompt` reads the file named by an agent's `system_prompt_path`. The formats are
described in [`config.example/README.md`](../config.example/README.md).

- A path may not leave the directory: `..`, absolute paths and symbolic links that point outside
  are refused.
- Unknown keys are rejected, so a typo does not silently drop a preference.
- Error messages name the file and the field, never a value from the file.
- The prompt text must match the hash recorded for its file, or the request is rejected.
