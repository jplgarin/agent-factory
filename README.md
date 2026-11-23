# agent-factory

A private, self-hosted multi-agent system: orchestration, memory and delegation. Agents work for one
person across two isolated spaces (`personal` and `synaption`). The model proposes actions;
deterministic code validates, authorizes, audits and executes them.

This repository is public and contains code, docs and synthetic examples only. Real configuration
lives in a separate private repository loaded through `AF_CONFIG_DIR`.

**Status:** M0 (repository, tooling, local stack). No agents yet. See [docs/PLAN.md](docs/PLAN.md)
for the milestones and [docs/adr/](docs/adr/) for the accepted decisions. [docs/SPEC.md](docs/SPEC.md)
is the source of truth.

## Architecture

```mermaid
flowchart LR
  you[You, over Tailscale] -->|bearer token, TLS| gw[Gateway<br/>FastAPI]
  gw --> pg[(Postgres 16<br/>+ pgvector)]
  gw --> t[Temporal]
  t --> wp[Worker: personal]
  t --> ws[Worker: synaption]
  wp --> pg
  ws --> pg
  wp --> r[(Redis)]
  ws --> r
```

One worker process per space, each with its own Temporal namespace and task queue (ADR 0001) and,
from M1, its own database role under row-level security (ADR 0002). Every published port binds
`127.0.0.1` (ADR 0007).

## Quick start

Requires Docker with Compose v2.20+, [uv](https://docs.astral.sh/uv/) and GNU make.

```sh
make setup   # dependencies, git hooks, .env with random local passwords
make up      # build and start the stack, wait until every service is healthy
curl -s http://127.0.0.1:8000/readyz
make test-integration
make down
```

Temporal UI: http://127.0.0.1:8080.

## Development

| Target | What it does |
| --- | --- |
| `make lint` | Ruff lint and format check |
| `make typecheck` | Pyright, strict on `core/` |
| `make test` | Unit tests with coverage |
| `make test-integration` | Tests against the running stack |
| `make eval` / `make migrate` | Placeholders until M5b / M1 |
| `make stats` | Memory and CPU per container |

`core/` may import only the standard library and pydantic; a unit test enforces it. Vendor code goes
under `integrations/`, processes under `apps/`.

## License

MIT
