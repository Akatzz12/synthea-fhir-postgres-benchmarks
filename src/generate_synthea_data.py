from __future__ import annotations

import argparse
import json
import re
import signal
import shutil
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any


DEFAULT_SYNTHIA_JAR = Path(__file__).resolve().parents[1] / "tools" / "synthea-with-dependencies.jar"
PERSON_PROGRESS_PATTERN = re.compile(r"^\s*\d+\s+--\s+.*\(\d+\s+y/o\s+[MF]\)")


def _person_is_deceased(line: str) -> bool | None:
    if not PERSON_PROGRESS_PATTERN.match(line):
        return None
    return "DECEASED" in line


def _run_synthea(
    command: list[str],
    *,
    population_size: int,
    log_path: Path,
    progress_interval: int = 1000,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    generated = 0
    alive = 0
    deceased = 0
    started_at = time.monotonic()

    with log_path.open("w", encoding="utf-8") as log_file:
        with subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        ) as process:
            try:
                assert process.stdout is not None
                for line in process.stdout:
                    log_file.write(line)
                    log_file.flush()
                    person_is_deceased = _person_is_deceased(line)
                    if person_is_deceased is None:
                        continue

                    generated += 1
                    if person_is_deceased:
                        deceased += 1
                    else:
                        alive += 1

                    if generated % progress_interval == 0:
                        elapsed = int(time.monotonic() - started_at)
                        remaining = max(population_size - alive, 0)
                        progress = (
                            f"Generated/exported bundles: {generated:,}; alive: {alive:,}; "
                            f"deceased: {deceased:,}; alive target remaining: {remaining:,}; "
                            f"elapsed: {elapsed}s"
                        )
                        print(progress, file=sys.stderr, flush=True)
                return_code = process.wait()
            except KeyboardInterrupt:
                if process.poll() is None:
                    process.send_signal(signal.SIGINT)
                    process.wait()
                raise

    if return_code != 0:
        raise subprocess.CalledProcessError(return_code, command)

    elapsed = int(time.monotonic() - started_at)
    remaining = max(population_size - alive, 0)
    print(
        f"Generation process finished: {generated:,} bundles; alive: {alive:,}; "
        f"deceased: {deceased:,}; alive target remaining: {remaining:,}; elapsed: {elapsed}s",
        file=sys.stderr,
        flush=True,
    )


def _iter_fhir_json_files(directory: Path) -> list[Path]:
    if directory.is_dir() and (directory / "fhir").exists():
        directory = directory / "fhir"
    return sorted(path for path in directory.rglob("*.json") if path.is_file())


def _prepare_output_directory(output_dir: str | Path, *, overwrite: bool) -> Path:
    output_path = Path(output_dir)
    if output_path.exists():
        if not output_path.is_dir():
            raise NotADirectoryError(f"Output path is not a directory: {output_path}")
        if any(output_path.iterdir()):
            if not overwrite:
                raise FileExistsError(
                    f"Output directory is not empty: {output_path}; "
                    "pass --overwrite to replace it"
                )
            shutil.rmtree(output_path)
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def summarize_fhir_directory(directory: str | Path) -> dict[str, Any]:
    base_dir = Path(directory)
    if not base_dir.exists():
        raise FileNotFoundError(f"FHIR directory does not exist: {base_dir}")

    json_files = _iter_fhir_json_files(base_dir)
    if not json_files:
        return {"file_count": 0, "resource_counts": {}, "patient_count": 0}

    resource_counts: Counter[str] = Counter()
    patient_ids: set[str] = set()

    for json_path in json_files:
        try:
            document = json.loads(json_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {json_path}: {exc}") from exc

        entries: list[dict[str, Any]] = []
        if isinstance(document, dict):
            if document.get("resourceType") == "Bundle":
                bundle_entries = document.get("entry", [])
                for entry in bundle_entries:
                    if isinstance(entry, dict):
                        resource = entry.get("resource")
                        if isinstance(resource, dict):
                            entries.append(resource)
            elif document.get("resourceType"):
                entries.append(document)

        for resource in entries:
            resource_type = resource.get("resourceType")
            if resource_type:
                resource_counts[resource_type] += 1
                if resource_type == "Patient":
                    patient_id = resource.get("id")
                    if patient_id:
                        patient_ids.add(str(patient_id))

    return {
        "file_count": len(json_files),
        "resource_counts": dict(sorted(resource_counts.items())),
        "total_fhir_resources": sum(resource_counts.values()),
        "patient_count": len(patient_ids),
        "raw_file_bytes": sum(path.stat().st_size for path in json_files),
    }


def generate_synthea_dataset(
    population_size: int,
    *,
    output_dir: str | Path,
    state: str | None = None,
    seed: int | None = None,
    synthea_jar: str | Path = DEFAULT_SYNTHIA_JAR,
    log_file: str | Path | None = None,
    overwrite: bool = False,
) -> dict[str, Any]:
    if population_size < 1:
        raise ValueError("population_size must be greater than zero")

    jar_path = Path(synthea_jar)
    if not jar_path.exists():
        raise FileNotFoundError(f"Synthea JAR not found: {jar_path}")

    output_path = _prepare_output_directory(output_dir, overwrite=overwrite)

    command = [
        "java",
        "-jar",
        str(jar_path),
        "--exporter.fhir.export=true",
        f"--exporter.baseDirectory={output_path}",
        "-p",
        str(population_size),
    ]
    if seed is not None:
        command.extend(["-s", str(seed)])
    if state:
        command.append(str(state))

    generation_log_path = Path(log_file) if log_file else output_path.parent / f"{output_path.name}_generation.log"
    generation_started_at = time.monotonic()
    _run_synthea(
        command,
        population_size=population_size,
        log_path=generation_log_path,
    )
    generation_seconds = time.monotonic() - generation_started_at

    summary = summarize_fhir_directory(output_path)
    summary["population_size"] = population_size
    summary["generation_seconds"] = generation_seconds
    summary["state"] = state
    summary["seed"] = seed
    summary["output_dir"] = str(output_path)
    summary["generation_log"] = str(generation_log_path)
    return summary


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate and validate Synthea FHIR datasets for the database comparison project.")
    parser.add_argument("--population", type=int, default=100, help="Number of synthetic patients to generate.")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for Synthea generation.")
    parser.add_argument("--state", default="Massachusetts", help="State or region to generate records for.")
    parser.add_argument("--output-dir", default="data/generated", help="Directory for the generated FHIR output.")
    parser.add_argument("--jar", default=str(DEFAULT_SYNTHIA_JAR), help="Path to the Synthea JAR file.")
    parser.add_argument("--log-file", help="Path for the full Synthea log; defaults beside the output directory.")
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Replace a non-empty output directory before generation.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    try:
        summary = generate_synthea_dataset(
            args.population,
            output_dir=args.output_dir,
            state=args.state,
            seed=args.seed,
            synthea_jar=args.jar,
            log_file=args.log_file,
            overwrite=args.overwrite,
        )
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f"Generation failed: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Generation interrupted. Partial output was left in place.", file=sys.stderr)
        return 130

    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
