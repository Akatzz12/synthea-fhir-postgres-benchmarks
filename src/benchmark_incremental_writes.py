from __future__ import annotations

"""
Incremental write benchmark: cost of appending one new encounter + observations
to an existing patient record.

Relational design: two simple INSERTs (encounters + observations tables).
JSONB design: SELECT full document → modify in Python → UPDATE full document.

The JSONB approach rewrites the entire patient document on every write,
regardless of how small the new data is. Write amplification measures this:

    amplification = bytes_rewritten / bytes_of_new_data

Run against write-test database copies, not the benchmark databases:

    createdb -T fhir_relational_benchmark fhir_relational_write_test
    createdb -T fhir_jsonb_benchmark      fhir_jsonb_write_test

    python -m src.benchmark_incremental_writes \\
        --relational-dsn 'dbname=fhir_relational_write_test' \\
        --jsonb-dsn      'dbname=fhir_jsonb_write_test' \\
        --output-file    results/phase8_incremental_writes.json
"""

import argparse
import json
import statistics
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = PROJECT_ROOT / "results" / "phase8_incremental_writes.json"

# One encounter + these five vital-sign observations per write.
NEW_VITALS: list[tuple[str, str, str, float, str]] = [
    ("http://loinc.org", "39156-5", "Body Mass Index",            24.5,  "kg/m2"),
    ("http://loinc.org", "8867-4",  "Heart rate",                 72.0,  "/min"),
    ("http://loinc.org", "59408-5", "Oxygen saturation",          99.0,  "%"),
    ("http://loinc.org", "8310-5",  "Body temperature",           37.0,  "Cel"),
    ("http://loinc.org", "8480-6",  "Systolic blood pressure",   118.0,  "mm[Hg]"),
]
ENC_START = "2025-06-01T10:00:00+00:00"
ENC_END   = "2025-06-01T11:00:00+00:00"


# ---------------------------------------------------------------------------
# Relational write
# ---------------------------------------------------------------------------

def _write_relational(conn: psycopg.Connection, patient_id: str, org_id: str | None) -> tuple[str, float]:
    enc_id  = str(uuid.uuid4())
    obs_ids = [str(uuid.uuid4()) for _ in NEW_VITALS]

    t0 = time.perf_counter_ns()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO encounters "
                "(encounter_id, patient_id, organization_id, encounter_type, start_time, end_time) "
                "VALUES (%s, %s, %s, %s, %s::timestamptz, %s::timestamptz)",
                (enc_id, patient_id, org_id, "AMB", ENC_START, ENC_END),
            )
            cur.executemany(
                "INSERT INTO observations "
                "(observation_id, patient_id, encounter_id, code_system, code, "
                " description, value_numeric, unit, observed_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::timestamptz)",
                [
                    (obs_ids[i], patient_id, enc_id,
                     code_system, code, description, value, unit, ENC_START)
                    for i, (code_system, code, description, value, unit) in enumerate(NEW_VITALS)
                ],
            )
    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
    return enc_id, elapsed_ms


# ---------------------------------------------------------------------------
# JSONB write
# ---------------------------------------------------------------------------

def _write_jsonb(conn: psycopg.Connection, patient_id: str) -> tuple[str, float, int, int, int]:
    enc_id  = str(uuid.uuid4())
    obs_ids = [str(uuid.uuid4()) for _ in NEW_VITALS]

    new_encounter = {
        "resourceType": "Encounter",
        "id": enc_id,
        "type": [{"coding": [{"code": "AMB", "display": "ambulatory"}]}],
        "period": {"start": ENC_START, "end": ENC_END},
    }
    new_observations = [
        {
            "resourceType": "Observation",
            "id": obs_ids[i],
            "code": {"coding": [{"system": code_system, "code": code, "display": description}]},
            "valueQuantity": {"value": value, "unit": unit},
            "effectiveDateTime": ENC_START,
        }
        for i, (code_system, code, description, value, unit) in enumerate(NEW_VITALS)
    ]
    new_data_bytes = (
        len(json.dumps(new_encounter).encode())
        + sum(len(json.dumps(o).encode()) for o in new_observations)
    )

    t0 = time.perf_counter_ns()
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "SELECT document, pg_column_size(document) "
                "FROM patient_documents WHERE patient_id = %s FOR UPDATE",
                (patient_id,),
            )
            row = cur.fetchone()
            document: dict[str, Any] = row[0]
            doc_bytes_before: int = row[1]

            document["encounters"]   = document.get("encounters",   []) + [new_encounter]
            document["observations"] = document.get("observations", []) + new_observations

            cur.execute(
                "UPDATE patient_documents SET document = %s "
                "WHERE patient_id = %s RETURNING pg_column_size(document)",
                (Jsonb(document), patient_id),
            )
            doc_bytes_after: int = cur.fetchone()[0]

    elapsed_ms = (time.perf_counter_ns() - t0) / 1_000_000
    return enc_id, elapsed_ms, doc_bytes_before, doc_bytes_after, new_data_bytes


# ---------------------------------------------------------------------------
# Benchmark runner
# ---------------------------------------------------------------------------

def run_incremental_write_benchmark(
    relational_dsn: str,
    jsonb_dsn: str,
    *,
    sample_size: int = 50,
    repetitions: int = 5,
    output_file: str | Path = DEFAULT_OUTPUT,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc)

    relational_results: list[float] = []
    jsonb_results:      list[float] = []
    amplification_samples: list[dict[str, Any]] = []

    # ---- relational --------------------------------------------------------
    with psycopg.connect(relational_dsn) as conn:
        patient_ids: list[str] = [
            row[0]
            for row in conn.execute(
                "SELECT patient_id FROM patients ORDER BY random() LIMIT %s",
                (sample_size,),
            ).fetchall()
        ]
        org_row = conn.execute(
            "SELECT organization_id FROM organizations LIMIT 1"
        ).fetchone()
        org_id = org_row[0] if org_row else None

        for pid in patient_ids:
            for _ in range(repetitions):
                _, ms = _write_relational(conn, pid, org_id)
                relational_results.append(ms)

    # ---- jsonb -------------------------------------------------------------
    with psycopg.connect(jsonb_dsn) as conn:
        patient_ids = [
            row[0]
            for row in conn.execute(
                "SELECT patient_id FROM patient_documents ORDER BY random() LIMIT %s",
                (sample_size,),
            ).fetchall()
        ]

        for i, pid in enumerate(patient_ids):
            for rep in range(repetitions):
                _, ms, before, after, new_bytes = _write_jsonb(conn, pid)
                jsonb_results.append(ms)
                # Collect amplification samples from the first write of each patient.
                if rep == 0 and i < 10:
                    amplification_samples.append({
                        "document_bytes_before": before,
                        "document_bytes_after":  after,
                        "new_data_bytes":         new_bytes,
                        "amplification_ratio":    round(after / new_bytes, 1) if new_bytes else None,
                    })

    def _stats(values: list[float]) -> dict[str, float]:
        s = sorted(values)
        rank95 = max(0, int(len(s) * 0.95) - 1)
        return {
            "mean_ms":   round(statistics.mean(values), 3),
            "stdev_ms":  round(statistics.stdev(values), 3) if len(values) > 1 else 0.0,
            "median_ms": round(statistics.median(values), 3),
            "p95_ms":    round(s[rank95], 3),
            "min_ms":    round(min(values), 3),
            "max_ms":    round(max(values), 3),
            "n":         len(values),
        }

    avg_amp = statistics.mean(
        s["amplification_ratio"] for s in amplification_samples if s["amplification_ratio"]
    ) if amplification_samples else None
    avg_doc_before = statistics.mean(
        s["document_bytes_before"] for s in amplification_samples
    ) if amplification_samples else None
    avg_new_bytes = statistics.mean(
        s["new_data_bytes"] for s in amplification_samples
    ) if amplification_samples else None

    summary: dict[str, Any] = {
        "created_at":        started_at.isoformat(),
        "sample_patients":   sample_size,
        "repetitions_per_patient": repetitions,
        "rows_per_write":    1 + len(NEW_VITALS),
        "new_data_description": "1 Encounter + 5 Observation vitals",
        "timings": {
            "relational": _stats(relational_results),
            "jsonb":       _stats(jsonb_results),
        },
        "write_amplification": {
            "avg_document_bytes_before_write": round(avg_doc_before) if avg_doc_before else None,
            "avg_new_data_bytes":              round(avg_new_bytes)  if avg_new_bytes  else None,
            "avg_amplification_ratio":         round(avg_amp, 1)     if avg_amp        else None,
            "interpretation": (
                f"Each JSONB write rewrites ~{round(avg_doc_before/1024):.0f} KB "
                f"to add ~{round(avg_new_bytes)} bytes of new data "
                f"(~{round(avg_amp, 0):.0f}x amplification)."
            ) if avg_amp and avg_doc_before and avg_new_bytes else None,
            "samples": amplification_samples,
        },
    }

    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    return summary


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Benchmark incremental writes: INSERT (relational) vs "
            "read-modify-write (JSONB). Run against write-test database copies."
        )
    )
    parser.add_argument("--relational-dsn", required=True,
                        help="DSN for the relational write-test database.")
    parser.add_argument("--jsonb-dsn", required=True,
                        help="DSN for the JSONB write-test database.")
    parser.add_argument("--sample-size", type=int, default=50,
                        help="Number of patients to sample (default 50).")
    parser.add_argument("--repetitions", type=int, default=5,
                        help="Writes per patient (default 5).")
    parser.add_argument("--output-file", type=Path, default=DEFAULT_OUTPUT)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args   = parser.parse_args(argv)
    try:
        summary = run_incremental_write_benchmark(
            args.relational_dsn,
            args.jsonb_dsn,
            sample_size=args.sample_size,
            repetitions=args.repetitions,
            output_file=args.output_file,
        )
    except (OSError, ValueError, psycopg.Error) as exc:
        print(f"Benchmark failed: {exc}", file=sys.stderr)
        return 1

    t = summary["timings"]
    amp = summary["write_amplification"]
    print(f"Relational  median={t['relational']['median_ms']:.1f}ms  "
          f"mean={t['relational']['mean_ms']:.1f}ms  "
          f"stdev={t['relational']['stdev_ms']:.1f}ms")
    print(f"JSONB       median={t['jsonb']['median_ms']:.1f}ms  "
          f"mean={t['jsonb']['mean_ms']:.1f}ms  "
          f"stdev={t['jsonb']['stdev_ms']:.1f}ms")
    if amp["avg_amplification_ratio"]:
        print(f"JSONB write amplification: {amp['avg_amplification_ratio']:.0f}x  "
              f"({amp['avg_document_bytes_before_write']:,} bytes rewritten "
              f"for {amp['avg_new_data_bytes']:,} bytes of new data)")
    print(f"Saved to {args.output_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
