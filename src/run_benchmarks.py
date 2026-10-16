from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RELATIONAL_QUERIES = PROJECT_ROOT / "sql" / "relational_queries.sql"
JSONB_QUERIES = PROJECT_ROOT / "sql" / "jsonb_queries.sql"
DEFAULT_OUTPUT = PROJECT_ROOT / "results" / "phase5_query_benchmarks.json"
QUERY_NAMES = (
    "patient_history",
    "bmi_by_gender",
    "medication_by_region_date",
)
HISTORY_ID_KEYS = {
    "encounters": "encounter_id",
    "conditions": "condition_id",
    "medications": "medication_id",
    "observations": "observation_id",
    "organizations": "organization_id",
    "providers": "provider_id",
    "provider_organizations": "role_id",
}


def load_named_queries(path: str | Path) -> dict[str, str]:
    queries: dict[str, list[str]] = {}
    current_name: str | None = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.startswith("-- name: "):
            current_name = line.removeprefix("-- name: ").strip()
            if not current_name or current_name in queries:
                raise ValueError(f"Invalid or duplicate query name in {path}: {current_name!r}")
            queries[current_name] = []
        elif current_name is not None:
            queries[current_name].append(line)

    result = {name: "\n".join(lines).strip() for name, lines in queries.items()}
    if any(not query for query in result.values()):
        raise ValueError(f"Empty named query in {path}")
    return result


def _history_signature(history: dict[str, Any], design: str) -> dict[str, Any]:
    patient = history["patient"]
    signature = {
        "patient_id": patient["patient_id"] if design == "relational" else patient["id"]
    }
    for section, relational_key in HISTORY_ID_KEYS.items():
        rows = history.get(section, [])
        id_key = relational_key if design == "relational" else "id"
        signature[section] = sorted(str(row[id_key]) for row in rows)
    return signature


def _result_signature(query_name: str, rows: list[tuple[Any, ...]], design: str) -> Any:
    if query_name == "patient_history":
        if not rows:
            return None
        return _history_signature(rows[0][0], design)
    if query_name == "bmi_by_gender":
        if not rows:
            return {"observation_count": 0, "avg_bmi": None}
        count, average = rows[0]
        return {
            "observation_count": int(count),
            "avg_bmi": format(average, "f") if isinstance(average, Decimal) else average,
        }
    return [str(row[0]) for row in rows]


def _query_parameters(query_name: str, design: str, patient_id: str) -> tuple[Any, ...]:
    if query_name == "patient_history":
        return (patient_id,)
    if query_name == "bmi_by_gender":
        return ("female",)
    common = (
        "http://www.nlm.nih.gov/research/umls/rxnorm",
        "314076",
        "2000-01-01T00:00:00+00:00",
        "2025-01-01T00:00:00+00:00",
    )
    if design == "relational":
        return (common[0], common[1], "MA", common[2], common[3])
    return (common[0], common[1], common[2], common[3], "MA")


def _percentile_95(values: list[float]) -> float:
    ordered = sorted(values)
    rank = max(0, (95 * len(ordered) + 99) // 100 - 1)
    return ordered[rank]


def run_query_benchmarks(
    relational_dsn: str,
    jsonb_dsn: str,
    *,
    repetitions: int = 3,
    random_seed: int = 42,
    output_file: str | Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    if repetitions < 3:
        raise ValueError("At least three repetitions are required")

    relational_queries = load_named_queries(RELATIONAL_QUERIES)
    jsonb_queries = load_named_queries(JSONB_QUERIES)
    if set(relational_queries) != set(QUERY_NAMES) or set(jsonb_queries) != set(QUERY_NAMES):
        raise ValueError("SQL files must define all three expected named queries")

    connections = {
        "relational": psycopg.connect(relational_dsn),
        "jsonb": psycopg.connect(jsonb_dsn),
    }
    try:
        patient_id = connections["relational"].execute(
            "SELECT min(patient_id) FROM patients"
        ).fetchone()[0]
        if not patient_id:
            raise ValueError("The relational patients table is empty")

        query_files = {"relational": relational_queries, "jsonb": jsonb_queries}
        parameters = {
            design: {
                name: _query_parameters(name, design, patient_id)
                for name in QUERY_NAMES
            }
            for design in connections
        }
        results: dict[str, dict[str, list[dict[str, Any]]]] = {
            design: {name: [] for name in QUERY_NAMES} for design in connections
        }
        signatures: dict[str, dict[str, Any]] = {name: {} for name in QUERY_NAMES}
        schedule = [
            (design, query_name)
            for design in connections
            for query_name in QUERY_NAMES
        ]
        rng = random.Random(random_seed)

        for repetition in range(1, repetitions + 1):
            current_schedule = schedule.copy()
            rng.shuffle(current_schedule)
            for design, query_name in current_schedule:
                query = query_files[design][query_name]
                params = parameters[design][query_name]
                started = time.perf_counter_ns()
                rows = connections[design].execute(query, params).fetchall()
                elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
                signature = _result_signature(query_name, rows, design)
                prior = signatures[query_name].get(design)
                if prior is not None and prior != signature:
                    raise AssertionError(
                        f"Non-deterministic result for {design}/{query_name}"
                    )
                signatures[query_name][design] = signature
                results[design][query_name].append(
                    {
                        "repetition": repetition,
                        "elapsed_ms": elapsed_ms,
                        "rows_returned": len(rows),
                    }
                )

        equivalence: dict[str, bool] = {}
        for query_name in QUERY_NAMES:
            equivalence[query_name] = (
                signatures[query_name]["relational"]
                == signatures[query_name]["jsonb"]
            )
            if not equivalence[query_name]:
                raise AssertionError(
                    f"Relational and JSONB results differ for {query_name}"
                )

        plans: dict[str, dict[str, Any]] = {design: {} for design in connections}
        database_sizes: dict[str, int] = {}
        for design, connection in connections.items():
            database_sizes[design] = int(
                connection.execute(
                    "SELECT pg_database_size(current_database())"
                ).fetchone()[0]
            )
            for query_name in QUERY_NAMES:
                explain_query = (
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) "
                    + query_files[design][query_name]
                )
                plans[design][query_name] = connection.execute(
                    explain_query, parameters[design][query_name]
                ).fetchone()[0]

        summaries: dict[str, dict[str, Any]] = {}
        for design in connections:
            summaries[design] = {}
            for query_name in QUERY_NAMES:
                timings = [
                    run["elapsed_ms"]
                    for run in results[design][query_name]
                ]
                summaries[design][query_name] = {
                    "runs": results[design][query_name],
                    "median_ms": statistics.median(timings),
                    "p95_ms_nearest_rank": _percentile_95(timings),
                }

        summary = {
            "created_at": datetime.now(timezone.utc).isoformat(),
            "dataset": "data/synthea_1000",
            "repetitions": repetitions,
            "random_seed_for_order": random_seed,
            "cache_state": (
                "uncontrolled; exploratory queries were run before timing, so these "
                "measurements may benefit from cached data"
            ),
            "parameters": {
                "patient_id": patient_id,
                "gender": "female",
                "bmi_loinc_code": "39156-5",
                "medication_code_system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "medication_code": "314076",
                "organization_state": "MA",
                "encounter_start_inclusive": "2000-01-01T00:00:00+00:00",
                "encounter_start_exclusive": "2025-01-01T00:00:00+00:00",
            },
            "result_equivalence": equivalence,
            "result_signatures": signatures,
            "database_size_bytes": database_sizes,
            "timings": summaries,
            "explain_analyze_buffers": plans,
        }
    finally:
        for connection in connections.values():
            connection.close()

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run equivalent relational and JSONB query benchmarks."
    )
    parser.add_argument("--relational-dsn", required=True)
    parser.add_argument("--jsonb-dsn", required=True)
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.repetitions < 3:
        parser.error("--repetitions must be at least 3")
    try:
        summary = run_query_benchmarks(
            args.relational_dsn,
            args.jsonb_dsn,
            repetitions=args.repetitions,
            random_seed=args.seed,
            output_file=args.output_file,
        )
    except (OSError, ValueError, psycopg.Error, AssertionError) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(summary["timings"], indent=2, sort_keys=True))
    print(f"Saved full results to {args.output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())