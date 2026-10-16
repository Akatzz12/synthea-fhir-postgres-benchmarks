from src.run_benchmarks import _history_signature, load_named_queries


def test_load_named_queries_reads_three_workloads() -> None:
    relational = load_named_queries("sql/relational_queries.sql")
    jsonb = load_named_queries("sql/jsonb_queries.sql")

    assert relational.keys() == jsonb.keys()
    assert set(relational) == {
        "patient_history",
        "bmi_by_gender",
        "medication_by_region_date",
    }


def test_history_signature_normalizes_relational_and_jsonb_ids() -> None:
    relational_history = {
        "patient": {"patient_id": "p1"},
        "encounters": [{"encounter_id": "e1"}],
        "conditions": [],
        "medications": [],
        "observations": [],
        "organizations": [],
        "providers": [],
        "provider_organizations": [],
    }
    jsonb_history = {
        "patient": {"id": "p1"},
        "encounters": [{"id": "e1"}],
        "conditions": [],
        "medications": [],
        "observations": [],
        "organizations": [],
        "providers": [],
        "provider_organizations": [],
    }

    assert _history_signature(relational_history, "relational") == _history_signature(
        jsonb_history, "jsonb"
    )