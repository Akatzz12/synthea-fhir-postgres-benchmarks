import json
from pathlib import Path
import sys

import pytest

from src.generate_synthea_data import (
    _person_is_deceased,
    _prepare_output_directory,
    _run_synthea,
    summarize_fhir_directory,
)


@pytest.fixture
def sample_fhir_dir(tmp_path: Path) -> Path:
    bundle = {
        "resourceType": "Bundle",
        "type": "collection",
        "entry": [
            {"resource": {"resourceType": "Patient", "id": "p1", "gender": "female"}},
            {"resource": {"resourceType": "Encounter", "id": "e1", "subject": {"reference": "Patient/p1"}}},
            {"resource": {"resourceType": "Condition", "id": "c1", "subject": {"reference": "Patient/p1"}}},
        ],
    }
    file_path = tmp_path / "bundle_001.json"
    file_path.write_text(json.dumps(bundle), encoding="utf-8")
    return tmp_path


def test_summarize_fhir_directory_counts_resources(sample_fhir_dir: Path) -> None:
    summary = summarize_fhir_directory(sample_fhir_dir)

    assert summary["file_count"] == 1
    assert summary["resource_counts"]["Patient"] == 1
    assert summary["resource_counts"]["Encounter"] == 1
    assert summary["resource_counts"]["Condition"] == 1
    assert summary["patient_count"] == 1


def test_summarize_fhir_directory_rejects_invalid_json(tmp_path: Path) -> None:
    bad_file = tmp_path / "bad.json"
    bad_file.write_text("{not-valid-json}", encoding="utf-8")

    with pytest.raises(ValueError):
        summarize_fhir_directory(tmp_path)


def test_person_progress_parser_distinguishes_deceased_and_alive() -> None:
    assert _person_is_deceased("1 -- Person (40 y/o F) Boston, MA") is False
    assert _person_is_deceased("2 -- Person (72 y/o M) Boston, MA DECEASED") is True
    assert _person_is_deceased("Loading module modules/example.json") is None


def test_synthea_runner_logs_progress_and_process_output(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_path = tmp_path / "generation.log"
    output_line = "1 -- Person (40 y/o F) Boston, MA"

    _run_synthea(
        [sys.executable, "-c", f"print({output_line!r})"],
        population_size=1,
        log_path=log_path,
        progress_interval=1,
    )

    assert "Generated/exported bundles: 1" in capsys.readouterr().err
    assert output_line in log_path.read_text(encoding="utf-8")


def test_prepare_output_directory_preserves_existing_data_without_overwrite(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "dataset"
    output_dir.mkdir()
    existing_file = output_dir / "keep.json"
    existing_file.write_text("{}", encoding="utf-8")

    with pytest.raises(FileExistsError, match="--overwrite"):
        _prepare_output_directory(output_dir, overwrite=False)

    assert existing_file.read_text(encoding="utf-8") == "{}"


def test_prepare_output_directory_replaces_data_only_with_overwrite(
    tmp_path: Path,
) -> None:
    output_dir = tmp_path / "dataset"
    output_dir.mkdir()
    (output_dir / "old.json").write_text("{}", encoding="utf-8")

    _prepare_output_directory(output_dir, overwrite=True)

    assert list(output_dir.iterdir()) == []
