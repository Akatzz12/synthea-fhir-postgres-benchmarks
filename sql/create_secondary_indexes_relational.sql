\timing on
CREATE INDEX IF NOT EXISTS encounters_patient_id_idx ON encounters(patient_id);
CREATE INDEX IF NOT EXISTS encounters_organization_date_idx ON encounters(organization_id, start_time);
CREATE INDEX IF NOT EXISTS encounter_providers_provider_id_idx ON encounter_providers(provider_id);
CREATE INDEX IF NOT EXISTS conditions_patient_id_idx ON conditions(patient_id);
CREATE INDEX IF NOT EXISTS medications_patient_id_idx ON medications(patient_id);
CREATE INDEX IF NOT EXISTS medications_encounter_id_idx ON medications(encounter_id);
CREATE INDEX IF NOT EXISTS medications_code_idx ON medications(code_system, code);
CREATE INDEX IF NOT EXISTS observations_patient_code_idx ON observations(patient_id, code);
CREATE INDEX IF NOT EXISTS observations_observed_at_idx ON observations(observed_at);
\timing off