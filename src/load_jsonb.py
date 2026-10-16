from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import psutil
import psycopg
from psycopg.types.json import Jsonb

from src.fhir_mapping import (
    SharedFHIRIndex,
    iter_patient_bundle_paths,
    project_patient_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "synthea_1000"
DEFAULT_SUMMARY_FILE = PROJECT_ROOT / "results" / "phase4_jsonb_load_summary.json"
DOCUMENT_SECTIONS = (
    "encounters",
    "conditions",
    "medications",
    "observations",
    "organizations",
    "providers",
    "provider_organizations",
)


def load_jsonb_dataset(
    dsn: str,
    data_dir: str | Path,
    *,
    reset: bool = False,
    progress_interval: int = 100,
) -> dict[str, Any]:
    source_dir = Path(data_dir)
    shared = SharedFHIRIndex.from_data_directory(source_dir)
    bundle_paths = list(iter_patient_bundle_paths(source_dir))
    if not bundle_paths:
        raise ValueError(f"No patient Bundle files found in {source_dir}")

    resource_counts: Counter[str] = Counter()
    process = psutil.Process()
    peak_rss_bytes = process.memory_info().rss
    started_at = datetime.now(timezone.utc)
    started_clock = time.monotonic()

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            if reset:
                cursor.execute("TRUNCATE TABLE patient_documents")

            for bundle_number, bundle_path in enumerate(bundle_paths, start=1):
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                patient_id, document = project_patient_bundle(bundle, shared)
                cursor.execute(
                    "INSERT INTO patient_documents (patient_id, document) VALUES (%s, %s)",
                    (patient_id, Jsonb(document)),
                )

                resource_counts["Patient"] += 1
                for section in DOCUMENT_SECTIONS:
                    for resource in document[section]:
                        resource_counts[str(resource["resourceType"])] += 1

                peak_rss_bytes = max(peak_rss_bytes, process.memory_info().rss)
                if bundle_number % progress_interval == 0:
                    elapsed = time.monotonic() - started_clock
                    print(
                        f"Loaded {bundle_number:,}/{len(bundle_paths):,} patient documents "
                        f"in {elapsed:.1f}s",
                        file=sys.stderr,
                        flush=True,
                    )

            database_size_bytes = cursor.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0]
            table_size_bytes = cursor.execute(
                "SELECT pg_total_relation_size('patient_documents'::regclass)"
            ).fetchone()[0]
            row_count = cursor.execute(
                "SELECT count(*) FROM patient_documents"
            ).fetchone()[0]

    finished_at = datetime.now(timezone.utc)
    elapsed_seconds = time.monotonic() - started_clock
    return {
        "design": "jsonb",
        "data_dir": str(source_dir),
        "source_bundle_count": len(bundle_paths),
        "patient_document_rows_loaded": len(bundle_paths),
        "patient_document_rows_in_table": row_count,
        "resource_counts_in_documents": dict(sorted(resource_counts.items())),
        "total_resource_copies_in_documents": sum(resource_counts.values()),
        "database_size_bytes": database_size_bytes,
        "patient_documents_table_size_bytes": table_size_bytes,
        "peak_observed_rss_bytes": peak_rss_bytes,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "documents_per_second": len(bundle_paths) / elapsed_seconds if elapsed_seconds else None,
        "reset_before_load": reset,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load Synthea patient histories into the PostgreSQL JSONB design."
    )
    parser.add_argument("--dsn", required=True, help="PostgreSQL connection string for the target database.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY_FILE)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate patient_documents before loading; this discards existing table contents.",
    )
    parser.add_argument("--progress-interval", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.progress_interval < 1:
        parser.error("--progress-interval must be at least 1")

    try:
        summary = load_jsonb_dataset(
            args.dsn,
            args.data_dir,
            reset=args.reset,
            progress_interval=args.progress_interval,
        )
    except (OSError, ValueError, psycopg.Error) as exc:
        print(f"JSONB load failed: {exc}", file=sys.stderr)
        return 1

    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())