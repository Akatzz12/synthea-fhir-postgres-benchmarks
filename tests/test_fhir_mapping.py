from src.fhir_mapping import SharedFHIRIndex, project_patient_bundle


def test_project_patient_bundle_resolves_shared_resources() -> None:
    organization = {
        "resourceType": "Organization",
        "id": "org-1",
        "identifier": [{"system": "urn:system:org", "value": "org-ext-1"}],
        "name": "Clinic",
    }
    practitioner = {
        "resourceType": "Practitioner",
        "id": "provider-1",
        "identifier": [{"system": "urn:system:npi", "value": "123"}],
        "name": [{"family": "Example"}],
    }
    role = {
        "resourceType": "PractitionerRole",
        "id": "role-1",
        "practitioner": {"identifier": {"system": "urn:system:npi", "value": "123"}},
        "organization": {
            "identifier": {"system": "urn:system:org", "value": "org-ext-1"}
        },
    }
    shared = SharedFHIRIndex.from_resources([organization, practitioner, role])
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "patient-1"}},
            {
                "resource": {
                    "resourceType": "Encounter",
                    "id": "encounter-1",
                    "subject": {"reference": "urn:uuid:patient-1"},
                    "serviceProvider": {
                        "reference": "Organization?identifier=urn:system:org|org-ext-1"
                    },
                    "participant": [
                        {
                            "individual": {
                                "reference": "Practitioner?identifier=urn:system:npi|123"
                            }
                        }
                    ],
                }
            },
            {
                "resource": {
                    "resourceType": "Condition",
                    "id": "condition-1",
                    "subject": {"reference": "urn:uuid:patient-1"},
                }
            },
        ],
    }

    patient_id, document = project_patient_bundle(bundle, shared)

    assert patient_id == "patient-1"
    assert document["patient"]["id"] == patient_id
    assert [item["id"] for item in document["encounters"]] == ["encounter-1"]
    assert [item["id"] for item in document["conditions"]] == ["condition-1"]
    assert [item["id"] for item in document["organizations"]] == ["org-1"]
    assert [item["id"] for item in document["providers"]] == ["provider-1"]
    assert [item["id"] for item in document["provider_organizations"]] == ["role-1"]


def test_shared_reference_index_resolves_resource_id_and_identifier() -> None:
    organization = {
        "resourceType": "Organization",
        "id": "org-1",
        "identifier": [{"system": "urn:system:org", "value": "org-ext-1"}],
    }
    shared = SharedFHIRIndex.from_resources([organization])

    assert shared.resolve({"reference": "Organization/org-1"}, "Organization") is organization
    assert shared.resolve(
        {"identifier": {"system": "urn:system:org", "value": "org-ext-1"}},
        "Organization",
    ) is organization