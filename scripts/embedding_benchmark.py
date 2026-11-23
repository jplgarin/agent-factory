"""Compare the candidate embedding models of ADR 0004 on a synthetic English/Spanish set.

For each model it reports recall on evals/embeddings/memory_recall.yaml, peak memory, load
time and speed. Each model runs in its own process so that its peak memory is its own.

Needs the network: fastembed downloads the models from Hugging Face. It is run by the
"Embedding benchmark" workflow, not by the test suite.

    uv run python -m scripts.embedding_benchmark
"""

import argparse
import json
import os
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import yaml

from integrations.embeddings.models import CANDIDATES, EmbeddingModel

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / "evals" / "embeddings" / "memory_recall.yaml"
THREADS = 2  # what a worker on the shared VPS can count on
BATCH = 32


def load_dataset(path: Path = DATASET) -> tuple[list[dict[str, str]], list[dict[str, str]]]:
    data = yaml.safe_load(path.read_text())
    return data["documents"], data["queries"]


def ranks(similarity: list[list[float]], relevant: list[int]) -> list[int]:
    """For each query, the 1-based position of its relevant document, best match first."""
    result: list[int] = []
    for row, target in zip(similarity, relevant, strict=True):
        order = sorted(range(len(row)), key=lambda index: (-row[index], index))
        result.append(order.index(target) + 1)
    return result


def summarise(found: list[int]) -> dict[str, Any]:
    return {
        "queries": len(found),
        "top1": sum(rank <= 1 for rank in found),
        "top3": sum(rank <= 3 for rank in found),
        "mrr": round(statistics.fmean(1 / rank for rank in found), 3) if found else 0.0,
    }


def score(
    documents: list[dict[str, str]], queries: list[dict[str, str]], similarity: list[list[float]]
) -> dict[str, dict[str, Any]]:
    index = {document["id"]: position for position, document in enumerate(documents)}
    language = {document["id"]: document["lang"] for document in documents}
    found = ranks(similarity, [index[query["relevant"]] for query in queries])
    same = [r for r, q in zip(found, queries, strict=True) if q["lang"] == language[q["relevant"]]]
    cross = [r for r, q in zip(found, queries, strict=True) if q["lang"] != language[q["relevant"]]]
    return {
        "all": summarise(found),
        "same_language": summarise(same),
        "cross_language": summarise(cross),
    }


def _revision_and_size(cache: Path) -> tuple[str, float]:
    snapshots = sorted(cache.glob("models--*/snapshots/*"))
    revision = snapshots[-1].name if snapshots else "unknown"
    size = sum(f.stat().st_size for f in cache.rglob("*") if f.is_file() and not f.is_symlink())
    return revision, round(size / 1e6)


def measure(model: EmbeddingModel, cache: Path) -> dict[str, Any]:  # pragma: no cover
    """Load one model, embed the set and time it. Runs in a child process; needs the network."""
    import numpy
    from fastembed import TextEmbedding

    documents, queries = load_dataset()
    query_prefix, document_prefix = model.query_prefix, model.document_prefix
    started = time.perf_counter()
    engine = TextEmbedding(model_name=model.name, cache_dir=str(cache), threads=THREADS)
    list(engine.embed(["warm up"]))
    load_s = time.perf_counter() - started

    texts = [document_prefix + document["text"] for document in documents]
    started = time.perf_counter()
    document_vectors = numpy.array(list(engine.embed(texts, batch_size=BATCH)), dtype=float)
    batch_s = time.perf_counter() - started

    latencies: list[float] = []
    query_vectors = []
    for query in queries:
        started = time.perf_counter()
        query_vectors.append(next(iter(engine.embed([query_prefix + query["text"]]))))
        latencies.append((time.perf_counter() - started) * 1000)
    query_matrix = numpy.array(query_vectors, dtype=float)

    def unit(matrix: Any) -> Any:
        return matrix / numpy.linalg.norm(matrix, axis=1, keepdims=True)

    similarity = (unit(query_matrix) @ unit(document_vectors).T).tolist()
    revision, size_mb = _revision_and_size(cache)
    return {
        "model": model.name,
        "dim": int(document_vectors.shape[1]),
        "revision": revision,
        "files_mb": size_mb,
        "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        "load_s": round(load_s, 1),
        "documents_per_s": round(len(texts) / batch_s, 1),
        "query_ms": round(statistics.median(latencies), 1),
        "scores": score(documents, queries, similarity),
    }


def _fraction(part: dict[str, Any], key: str) -> str:
    return f"{part[key]}/{part['queries']}"


def table(results: list[dict[str, Any]]) -> str:
    header = (
        "| Model | Dim | Files | Peak memory | Load | Documents/s | Query | "
        "Top 1 | Top 3 | MRR | Top 1 same language | Top 1 cross language |\n"
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |\n"
    )
    rows = [
        f"| `{r['model']}` | {r['dim']} | {r['files_mb']} MB | {r['peak_rss_mb']} MB | "
        f"{r['load_s']} s | {r['documents_per_s']} | {r['query_ms']} ms | "
        f"{_fraction(r['scores']['all'], 'top1')} | {_fraction(r['scores']['all'], 'top3')} | "
        f"{r['scores']['all']['mrr']} | {_fraction(r['scores']['same_language'], 'top1')} | "
        f"{_fraction(r['scores']['cross_language'], 'top1')} |"
        for r in results
    ]
    return header + "\n".join(rows) + "\n"


def main() -> None:  # pragma: no cover
    parser = argparse.ArgumentParser(description="Compare the candidate embedding models.")
    parser.add_argument("--model", help="measure this one model and print JSON (child process)")
    parser.add_argument("--cache", type=Path, default=ROOT / ".cache" / "embedding-benchmark")
    arguments = parser.parse_args()
    if arguments.model:
        model = next(candidate for candidate in CANDIDATES if candidate.name == arguments.model)
        print(json.dumps(measure(model, arguments.cache / model.directory)))
        return

    results: list[dict[str, Any]] = []
    for model in CANDIDATES:
        command = [sys.executable, "-m", "scripts.embedding_benchmark", "--model", model.name]
        child = subprocess.run(  # noqa: S603
            [*command, "--cache", str(arguments.cache)],
            capture_output=True,
            text=True,
            check=False,
            cwd=ROOT,
        )
        if child.returncode != 0:
            sys.stderr.write(child.stderr)
            raise SystemExit(f"benchmark of {model.name} failed")
        result = json.loads(child.stdout.strip().splitlines()[-1])
        results.append(result)
        # One annotation per model, so the numbers can be read back through the GitHub API.
        print(f"::notice title=embedding-benchmark::{json.dumps(result, sort_keys=True)}")
    report = (
        f"Threads: {THREADS}. Set: {DATASET.relative_to(ROOT)} "
        f"({results[0]['scores']['all']['queries']} queries).\n\n" + table(results)
    )
    print(report)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as handle:
            handle.write("## Embedding benchmark\n\n" + report)


if __name__ == "__main__":  # pragma: no cover
    main()
