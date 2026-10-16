from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit


class FHIRMappingError(ValueError):
    pass


@dataclass
class SharedFHIRIndex:
    resources_by_id: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    resources_by_identifier: dict[tuple[str, str, str], dict[str, Any]] = field(default_factory=dict)
    practitioner_roles: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_resources(cls, resources: list[dict[str, Any]]) -> SharedFHIRIndex:
        index = cls()
        for resource in resources:
            resource_type = resource.get("resourceType")
            resource_id = resource.get("id")
            if not resource_type or not resource_id:
                continue

            key = (str(resource_type), str(resource_id))
            if key in index.resources_by_id:
                raise FHIRMappingError(f"Duplicate shared resource: {key}")
            index.resources_by_id[key] = resource

            if resource_type == "PractitionerRole":
                index.practitioner_roles.append(resource)

            for identifier in resource.get("identifier", []):
                system = identifier.get("system")
                value = identifier.get("value")
                if not system or not value:
                    continue
                identifier_key = (str(resource_type), str(system), str(value))
                existing = index.resources_by_identifier.get(identifier_key)
                if existing and existing.get("id") != resource_id:
                    raise FHIRMappingError(f"Ambiguous shared identifier: {identifier_key}")
                index.resources_by_identifier[identifier_key] = resource
        return index

    @classmethod
    def from_data_directory(cls, data_dir: str | Path) -> SharedFHIRIndex:
        root = Path(data_dir)
        if (root / "fhir").is_dir():
            root = root / "fhir"

        shared_resources: list[dict[str, Any]] = []
        for pattern in ("hospitalInformation*.json", "practitionerInformation*.json"):
            paths = sorted(root.glob(pattern))
            if len(paths) != 1:
                raise FHIRMappingError(
                    f"Expected one file matching {pattern} in {root}, found {len(paths)}"
                )
            bundle = json.loads(paths[0].read_text(encoding="utf-8"))
            if bundle.get("resourceType") != "Bundle":
                raise FHIRMappingError(f"Expected a FHIR Bundle in {paths[0]}")
            shared_resources.extend(_bundle_resources(bundle))
        return cls.from_resources(shared_resources)

    def resolve(self, reference: dict[str, Any] | None, expected_type: str) -> dict[str, Any] | None:
        if not isinstance(reference, dict):
            return None

        identifier = reference.get("identifier")
        if isinstance(identifier, dict):
            system = identifier.get("system")
            value = identifier.get("value")
            if system and value:
                return self.resources_by_identifier.get(
                    (expected_type, str(system), str(value))
                )

        reference_value = reference.get("reference")
        if not isinstance(reference_value, str):
            return None

        if reference_value.startswith("urn:uuid:"):
            resource_id = reference_value.removeprefix("urn:uuid:")
            return self.resources_by_id.get((expected_type, resource_id))

        parsed = urlsplit(reference_value)
        if parsed.query:
            query = parse_qs(parsed.query)
            identifiers = query.get("identifier", [])
            if identifiers:
                system, separator, value = identifiers[0].partition("|")
                if separator and system and value:
                    return self.resources_by_identifier.get(
                        (expected_type, system, value)
                    )

        parts = parsed.path.strip("/").split("/")
        if len(parts) == 2 and parts[0] == expected_type:
            return self.resources_by_id.get((expected_type, parts[1]))
        return None


def _bundle_resources(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    if bundle.get("resourceType") != "Bundle":
        raise FHIRMappingError("Expected a FHIR Bundle")
    return [
        entry["resource"]
        for entry in bundle.get("entry", [])
        if isinstance(entry, dict) and isinstance(entry.get("resource"), dict)
    ]


def iter_patient_bundle_paths(data_dir: str | Path):
    root = Path(data_dir)
    if (root / "fhir").is_dir():
        root = root / "fhir"
    for path in sorted(root.glob("*.json")):
        if path.name.startswith(("hospitalInformation", "practitionerInformation")):
            continue
        yield path


def project_patient_bundle(
    bundle: dict[str, Any], shared: SharedFHIRIndex
) -> tuple[str, dict[str, Any]]:
    resources = _bundle_resources(bundle)
    by_type: dict[str, list[dict[str, Any]]] = {}
    for resource in resources:
        resource_type = resource.get("resourceType")
        if resource_type in {
            "Patient",
            "Encounter",
            "Condition",
            "MedicationRequest",
            "Observation",
        }:
            by_type.setdefault(str(resource_type), []).append(resource)

    patients = by_type.get("Patient", [])
    if len(patients) != 1 or not patients[0].get("id"):
        raise FHIRMappingError("Each patient Bundle must contain exactly one identified Patient")
    patient = patients[0]
    patient_id = str(patient["id"])

    organizations: dict[str, dict[str, Any]] = {}
    providers: dict[str, dict[str, Any]] = {}
    for encounter in by_type.get("Encounter", []):
        organization_ref = encounter.get("serviceProvider")
        if organization_ref:
            organization = shared.resolve(organization_ref, "Organization")
            if organization is None:
                raise FHIRMappingError(
                    f"Unresolved Encounter.serviceProvider in patient {patient_id}: "
                    f"{organization_ref}"
                )
            organizations[str(organization["id"])] = organization

        for participant in encounter.get("participant", []):
            provider_ref = participant.get("individual")
            if not provider_ref:
                continue
            provider = shared.resolve(provider_ref, "Practitioner")
            if provider is None:
                raise FHIRMappingError(
                    f"Unresolved Encounter participant in patient {patient_id}: "
                    f"{provider_ref}"
                )
            providers[str(provider["id"])] = provider

    provider_organizations: dict[str, dict[str, Any]] = {}
    for role in shared.practitioner_roles:
        practitioner = shared.resolve(role.get("practitioner"), "Practitioner")
        if practitioner is None or str(practitioner.get("id")) not in providers:
            continue
        organization = shared.resolve(role.get("organization"), "Organization")
        if organization is None:
            raise FHIRMappingError(
                f"Unresolved PractitionerRole.organization: {role.get('organization')}"
            )
        organizations[str(organization["id"])] = organization
        provider_organizations[str(role["id"])] = role

    document = {
        "patient": patient,
        "encounters": by_type.get("Encounter", []),
        "conditions": by_type.get("Condition", []),
        "medications": by_type.get("MedicationRequest", []),
        "observations": by_type.get("Observation", []),
        "organizations": list(organizations.values()),
        "providers": list(providers.values()),
        "provider_organizations": list(provider_organizations.values()),
    }
    return patient_id, document