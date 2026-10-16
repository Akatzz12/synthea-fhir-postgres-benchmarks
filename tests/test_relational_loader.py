from src.fhir_mapping import SharedFHIRIndex, project_patient_bundle
from src.load_relational import _patient_rows, _provider_organization_rows


def test_patient_fhir_resources_map_to_relational_rows() -> None:
    organization = {
        "resourceType": "Organization",
        "id": "org-1",
        "identifier": [{"system": "urn:org", "value": "org-external"}],
        "name": "Example Clinic",
        "address": [{"city": "Boston", "state": "MA"}],
    }
    practitioner = {
        "resourceType": "Practitioner",
        "id": "provider-1",
        "identifier": [{"system": "urn:npi", "value": "123"}],
        "name": [{"given": ["Taylor"], "family": "Example"}],
    }
    role = {
        "resourceType": "PractitionerRole",
        "id": "role-1",
        "practitioner": {"identifier": {"system": "urn:npi", "value": "123"}},
        "organization": {
            "identifier": {"system": "urn:org", "value": "org-external"}
        },
        "code": [{"coding": [{"system": "urn:role", "code": "P"}]}],
    }
    shared = SharedFHIRIndex.from_resources([organization, practitioner, role])
    patient = {
        "resourceType": "Patient",
        "id": "patient-1",
        "name": [{"given": ["Jordan", "A"], "family": "Sample"}],
        "birthDate": "1980-01-02",
        "gender": "female",
        "extension": [
            {
                "url": "http://hl7.org/fhir/us/core/StructureDefinition/us-core-race",
                "extension": [{"url": "text", "valueString": "White"}],
            }
        ],
        "address": [{"state": "MA"}],
    }
    encounter = {
        "resourceType": "Encounter",
        "id": "encounter-1",
        "subject": {"reference": "urn:uuid:patient-1"},
        "serviceProvider": {
            "reference": "Organization?identifier=urn:org|org-external"
        },
        "participant": [
            {
                "individual": {
                    "reference": "Practitioner?identifier=urn:npi|123"
                },
                "type": [{"coding": [{"code": "PPRF"}]}],
            }
        ],
        "type": [{"coding": [{"system": "urn:encounter", "code": "AMB"}]}],
        "period": {"start": "2025-01-01T10:00:00Z", "end": "2025-01-01T10:30:00Z"},
    }
    condition = {
        "resourceType": "Condition",
        "id": "condition-1",
        "subject": {"reference": "urn:uuid:patient-1"},
        "encounter": {"reference": "urn:uuid:encounter-1"},
        "code": {"coding": [{"system": "urn:condition", "code": "C1", "display": "Example"}]},
        "onsetDateTime": "2025-01-01T10:00:00Z",
    }
    medication = {
        "resourceType": "MedicationRequest",
        "id": "medication-1",
        "subject": {"reference": "urn:uuid:patient-1"},
        "encounter": {"reference": "urn:uuid:encounter-1"},
        "medicationCodeableConcept": {
            "coding": [{"system": "urn:med", "code": "M1", "display": "Medicine"}]
        },
        "authoredOn": "2025-01-01T10:00:00Z",
        "status": "active",
    }
    observation = {
        "resourceType": "Observation",
        "id": "observation-1",
        "subject": {"reference": "urn:uuid:patient-1"},
        "encounter": {"reference": "urn:uuid:encounter-1"},
        "code": {"coding": [{"system": "urn:loinc", "code": "L1", "display": "BMI"}]},
        "effectiveDateTime": "2025-01-01T10:10:00Z",
        "valueQuantity": {"value": 22.5, "unit": "kg/m2"},
    }
    bundle = {
        "resourceType": "Bundle",
        "entry": [
            {"resource": resource}
            for resource in (patient, encounter, condition, medication, observation)
        ],
    }

    _, document = project_patient_bundle(bundle, shared)
    rows = _patient_rows(document, shared)

    assert rows["patients"][0] == (
        "patient-1",
        "Jordan A",
        "Sample",
        "1980-01-02",
        "female",
        "White",
        "MA",
        None,
    )
    assert rows["encounters"][0][2:4] == ("org-1", "AMB")
    assert rows["encounter_providers"][0] == ("encounter-1", "provider-1", "PPRF")
    assert rows["conditions"][0][3:6] == ("urn:condition", "C1", "Example")
    assert rows["medications"][0][3:8] == (
        "urn:med",
        "M1",
        "Medicine",
        "2025-01-01T10:00:00Z",
        "active",
    )
    assert rows["observations"][0][3:7] == ("urn:loinc", "L1", "BMI", 22.5)
    assert rows["observations"][0][9:] == ("kg/m2", "2025-01-01T10:10:00Z")
    assert _provider_organization_rows(shared) == [
        ("role-1", "provider-1", "org-1", "P")
    ]