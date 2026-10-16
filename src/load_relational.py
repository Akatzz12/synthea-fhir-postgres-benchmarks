from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import psutil
import psycopg
from psycopg.types.json import Jsonb

from src.fhir_mapping import (
    FHIRMappingError,
    SharedFHIRIndex,
    iter_patient_bundle_paths,
    project_patient_bundle,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data" / "synthea_1000"
DEFAULT_SUMMARY_FILE = PROJECT_ROOT / "results" / "phase4_relational_load_summary.json"
BATCH_SIZE = 1000
TABLES_TO_RESET = (
    "encounter_providers",
    "conditions",
    "medications",
    "observations",
    "encounters",
    "provider_organizations",
    "provider_identifiers",
    "providers",
    "organization_identifiers",
    "organizations",
    "patients",
)


def _name_parts(resource: dict[str, Any]) -> tuple[str | None, str | None]:
    names = resource.get("name") or []
    if not names:
        return None, None
    given = " ".join(names[0].get("given", [])) or None
    return given, names[0].get("family")


def _first_coding(concept: dict[str, Any] | None) -> tuple[str | None, str | None, str | None]:
    if not isinstance(concept, dict):
        return None, None, None
    codings = concept.get("coding") or []
    if not codings:
        return None, None, concept.get("text")
    coding = codings[0]
    return coding.get("system"), coding.get("code"), coding.get("display") or concept.get("text")


def _resource_id(reference: dict[str, Any] | None, resource_type: str) -> str | None:
    if not isinstance(reference, dict):
        return None
    value = reference.get("reference")
    if not isinstance(value, str):
        return None
    if value.startswith("urn:uuid:"):
        return value.removeprefix("urn:uuid:")
    parsed = urlsplit(value)
    parts = parsed.path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == resource_type:
        return parts[1]
    return None


def _resolve_external(
    reference: dict[str, Any] | None,
    resource_type: str,
    shared: SharedFHIRIndex,
    context: str,
) -> dict[str, Any] | None:
    resource = shared.resolve(reference, resource_type)
    if resource is None:
        raise FHIRMappingError(f"Unresolved {context}: {reference}")
    return resource


def _patient_race(patient: dict[str, Any]) -> str | None:
    race_url = "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race"
    for extension in patient.get("extension", []):
        if extension.get("url") != race_url:
            continue
        nested = extension.get("extension", [])
        for item in nested:
            if item.get("url") == "text" and item.get("valueString"):
                return item["valueString"]
        for item in nested:
            coding = item.get("valueCoding")
            if item.get("url") == "ombCategory" and isinstance(coding, dict):
                return coding.get("display") or coding.get("code")
    return None


def _patient_rows(
    document: dict[str, Any], shared: SharedFHIRIndex
) -> dict[str, list[tuple[Any, ...]]]:
    patient = document["patient"]
    patient_id = str(patient["id"])
    first_name, last_name = _name_parts(patient)
    addresses = patient.get("address") or []
    address = addresses[0] if addresses else {}
    deceased = patient.get("deceasedDateTime")
    patient_row = (
        patient_id,
        first_name,
        last_name,
        patient.get("birthDate"),
        patient.get("gender"),
        _patient_race(patient),
        address.get("state"),
        deceased,
    )

    local_encounters = {
        str(resource["id"]): resource for resource in document["encounters"]
    }
    rows: dict[str, list[tuple[Any, ...]]] = {
        "patients": [patient_row],
        "encounters": [],
        "encounter_providers": [],
        "conditions": [],
        "medications": [],
        "observations": [],
    }

    for encounter in document["encounters"]:
        encounter_id = str(encounter["id"])
        subject_id = _resource_id(encounter.get("subject"), "Patient")
        if subject_id != patient_id:
            raise FHIRMappingError(
                f"Encounter {encounter_id} subject {subject_id} does not match patient {patient_id}"
            )
        organization_ref = encounter.get("serviceProvider")
        organization = (
            _resolve_external(
                organization_ref,
                "Organization",
                shared,
                f"Encounter {encounter_id} serviceProvider",
            )
            if organization_ref
            else None
        )
        period = encounter.get("period") or {}
        encounter_type = encounter.get("type") or []
        _, type_code, type_display = _first_coding(
            encounter_type[0] if encounter_type else None
        )
        rows["encounters"].append(
            (
                encounter_id,
                patient_id,
                str(organization["id"]) if organization else None,
                type_code or type_display,
                period.get("start"),
                period.get("end"),
            )
        )

        for participant in encounter.get("participant", []):
            provider_ref = participant.get("individual")
            if not provider_ref:
                continue
            provider = _resolve_external(
                provider_ref,
                "Practitioner",
                shared,
                f"Encounter {encounter_id} participant",
            )
            participation_types = participant.get("type") or []
            _, participation_code, _ = _first_coding(
                participation_types[0] if participation_types else None
            )
            rows["encounter_providers"].append(
                (encounter_id, str(provider["id"]), participation_code or "")
            )

    for condition in document["conditions"]:
        condition_id = str(condition["id"])
        subject_id = _resource_id(condition.get("subject"), "Patient")
        if subject_id != patient_id:
            raise FHIRMappingError(
                f"Condition {condition_id} subject {subject_id} does not match patient {patient_id}"
            )
        encounter_id = _resource_id(condition.get("encounter"), "Encounter")
        if encounter_id and encounter_id not in local_encounters:
            raise FHIRMappingError(f"Condition {condition_id} has unresolved Encounter {encounter_id}")
        code_system, code, description = _first_coding(condition.get("code"))
        rows["conditions"].append(
            (
                condition_id,
                patient_id,
                encounter_id,
                code_system,
                code,
                description,
                condition.get("onsetDateTime"),
                condition.get("abatementDateTime"),
            )
        )

    for medication in document["medications"]:
        medication_id = str(medication["id"])
        subject_id = _resource_id(medication.get("subject"), "Patient")
        if subject_id != patient_id:
            raise FHIRMappingError(
                f"MedicationRequest {medication_id} subject {subject_id} does not match patient {patient_id}"
            )
        encounter_id = _resource_id(medication.get("encounter"), "Encounter")
        if encounter_id and encounter_id not in local_encounters:
            raise FHIRMappingError(
                f"MedicationRequest {medication_id} has unresolved Encounter {encounter_id}"
            )
        code_system, code, description = _first_coding(
            medication.get("medicationCodeableConcept")
        )
        rows["medications"].append(
            (
                medication_id,
                patient_id,
                encounter_id,
                code_system,
                code,
                description,
                medication.get("authoredOn"),
                medication.get("status"),
            )
        )

    for observation in document["observations"]:
        observation_id = str(observation["id"])
        subject_id = _resource_id(observation.get("subject"), "Patient")
        if subject_id != patient_id:
            raise FHIRMappingError(
                f"Observation {observation_id} subject {subject_id} does not match patient {patient_id}"
            )
        encounter_id = _resource_id(observation.get("encounter"), "Encounter")
        if encounter_id and encounter_id not in local_encounters:
            raise FHIRMappingError(
                f"Observation {observation_id} has unresolved Encounter {encounter_id}"
            )
        code_system, code, description = _first_coding(observation.get("code"))
        quantity = observation.get("valueQuantity") or {}
        other_values = {
            key: value
            for key, value in observation.items()
            if key.startswith("value") and key != "valueQuantity"
        }
        if observation.get("component"):
            other_values["component"] = observation["component"]
        rows["observations"].append(
            (
                observation_id,
                patient_id,
                encounter_id,
                code_system,
                code,
                description,
                quantity.get("value"),
                other_values.get("valueString"),
                Jsonb(other_values) if other_values else None,
                quantity.get("unit"),
                observation.get("effectiveDateTime")
                or (observation.get("effectivePeriod") or {}).get("start"),
            )
        )

    return rows


def _identifier_rows(resources: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    rows = []
    for resource in resources:
        resource_id = str(resource["id"])
        for identifier in resource.get("identifier", []):
            system = identifier.get("system")
            value = identifier.get("value")
            if system and value:
                rows.append((resource_id, str(system), str(value)))
    return rows


def _provider_organization_rows(shared: SharedFHIRIndex) -> list[tuple[str, str, str, str]]:
    rows = []
    for role in shared.practitioner_roles:
        provider = shared.resolve(role.get("practitioner"), "Practitioner")
        organization = shared.resolve(role.get("organization"), "Organization")
        if provider is None or organization is None:
            raise FHIRMappingError(f"Unresolved PractitionerRole reference in {role.get('id')}")
        codes = role.get("code") or []
        _, role_code, role_display = _first_coding(codes[0] if codes else None)
        rows.append(
            (
                str(role["id"]),
                str(provider["id"]),
                str(organization["id"]),
                role_code or role_display or "",
            )
        )
    return rows


def _batched_insert(
    cursor: psycopg.Cursor[Any],
    statement: str,
    rows: Iterable[tuple[Any, ...]],
) -> int:
    batch: list[tuple[Any, ...]] = []
    inserted = 0
    for row in rows:
        batch.append(row)
        if len(batch) >= BATCH_SIZE:
            cursor.executemany(statement, batch)
            inserted += len(batch)
            batch.clear()
    if batch:
        cursor.executemany(statement, batch)
        inserted += len(batch)
    return inserted


def load_relational_dataset(
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

    organizations = [
        resource
        for (resource_type, _), resource in shared.resources_by_id.items()
        if resource_type == "Organization"
    ]
    providers = [
        resource
        for (resource_type, _), resource in shared.resources_by_id.items()
        if resource_type == "Practitioner"
    ]
    counts: Counter[str] = Counter()
    process = psutil.Process()
    peak_rss_bytes = process.memory_info().rss
    started_at = datetime.now(timezone.utc)
    started_clock = time.monotonic()

    with psycopg.connect(dsn) as connection:
        with connection.cursor() as cursor:
            if reset:
                cursor.execute(
                    "TRUNCATE TABLE " + ", ".join(TABLES_TO_RESET) + " CASCADE"
                )

            counts["organizations"] = _batched_insert(
                cursor,
                "INSERT INTO organizations (organization_id, name, state, city) VALUES (%s, %s, %s, %s)",
                (
                    (
                        str(resource["id"]),
                        resource.get("name"),
                        ((resource.get("address") or [{}])[0]).get("state"),
                        ((resource.get("address") or [{}])[0]).get("city"),
                    )
                    for resource in organizations
                ),
            )
            counts["organization_identifiers"] = _batched_insert(
                cursor,
                "INSERT INTO organization_identifiers (organization_id, system, value) VALUES (%s, %s, %s)",
                _identifier_rows(organizations),
            )
            counts["providers"] = _batched_insert(
                cursor,
                "INSERT INTO providers (provider_id, first_name, last_name) VALUES (%s, %s, %s)",
                (
                    (str(resource["id"]), *_name_parts(resource))
                    for resource in providers
                ),
            )
            counts["provider_identifiers"] = _batched_insert(
                cursor,
                "INSERT INTO provider_identifiers (provider_id, system, value) VALUES (%s, %s, %s)",
                _identifier_rows(providers),
            )
            counts["provider_organizations"] = _batched_insert(
                cursor,
                "INSERT INTO provider_organizations (role_id, provider_id, organization_id, role_code) VALUES (%s, %s, %s, %s)",
                _provider_organization_rows(shared),
            )

            for bundle_number, bundle_path in enumerate(bundle_paths, start=1):
                bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
                _, document = project_patient_bundle(bundle, shared)
                patient_rows = _patient_rows(document, shared)

                for table, columns in (
                    ("patients", "patient_id, first_name, last_name, birth_date, gender, race, state, deceased_at"),
                    ("encounters", "encounter_id, patient_id, organization_id, encounter_type, start_time, end_time"),
                    ("encounter_providers", "encounter_id, provider_id, participation_type"),
                    ("conditions", "condition_id, patient_id, encounter_id, code_system, code, description, onset_at, abatement_at"),
                    ("medications", "medication_id, patient_id, encounter_id, code_system, code, description, authored_at, status"),
                    ("observations", "observation_id, patient_id, encounter_id, code_system, code, description, value_numeric, value_text, value_json, unit, observed_at"),
                ):
                    rows = patient_rows[table]
                    placeholders = ", ".join(["%s"] * len(rows[0])) if rows else ""
                    if rows:
                        inserted = _batched_insert(
                            cursor,
                            f"INSERT INTO {table} ({columns}) VALUES ({placeholders})",
                            rows,
                        )
                        counts[table] += inserted

                for resource_type, table in (
                    ("Patient", "patients"),
                    ("Encounter", "encounters"),
                    ("Condition", "conditions"),
                    ("MedicationRequest", "medications"),
                    ("Observation", "observations"),
                ):
                    counts[f"source_{resource_type}"] += len(
                        document[
                            {
                                "Patient": "patient",
                                "Encounter": "encounters",
                                "Condition": "conditions",
                                "MedicationRequest": "medications",
                                "Observation": "observations",
                            }[resource_type]
                        ]
                        if resource_type != "Patient"
                        else [document["patient"]]
                    )

                peak_rss_bytes = max(peak_rss_bytes, process.memory_info().rss)
                if bundle_number % progress_interval == 0:
                    elapsed = time.monotonic() - started_clock
                    print(
                        f"Loaded {bundle_number:,}/{len(bundle_paths):,} patient Bundles "
                        f"in {elapsed:.1f}s",
                        file=sys.stderr,
                        flush=True,
                    )

            row_counts = {
                table: cursor.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in TABLES_TO_RESET
            }
            database_size_bytes = cursor.execute(
                "SELECT pg_database_size(current_database())"
            ).fetchone()[0]
            total_relation_size_bytes = cursor.execute(
                "SELECT COALESCE(sum(pg_total_relation_size(relid)), 0)::bigint "
                "FROM pg_catalog.pg_statio_user_tables"
            ).fetchone()[0]

    finished_at = datetime.now(timezone.utc)
    elapsed_seconds = time.monotonic() - started_clock
    return {
        "design": "relational",
        "data_dir": str(source_dir),
        "source_bundle_count": len(bundle_paths),
        "loaded_rows_by_table": dict(sorted(counts.items())),
        "rows_in_tables_after_load": dict(sorted(row_counts.items())),
        "database_size_bytes": database_size_bytes,
        "user_relation_sizes_bytes": total_relation_size_bytes,
        "peak_observed_rss_bytes": peak_rss_bytes,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "elapsed_seconds": elapsed_seconds,
        "bundles_per_second": len(bundle_paths) / elapsed_seconds if elapsed_seconds else None,
        "reset_before_load": reset,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Load selected Synthea FHIR resources into normalized PostgreSQL tables."
    )
    parser.add_argument("--dsn", required=True, help="PostgreSQL connection string for the target database.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--summary-file", type=Path, default=DEFAULT_SUMMARY_FILE)
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Truncate benchmark tables before loading; this discards existing table contents.",
    )
    parser.add_argument("--progress-interval", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.progress_interval < 1:
        parser.error("--progress-interval must be at least 1")

    try:
        summary = load_relational_dataset(
            args.dsn,
            args.data_dir,
            reset=args.reset,
            progress_interval=args.progress_interval,
        )
    except (OSError, ValueError, psycopg.Error) as exc:
        print(f"Relational load failed: {exc}", file=sys.stderr)
        return 1

    args.summary_file.parent.mkdir(parents=True, exist_ok=True)
    args.summary_file.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())