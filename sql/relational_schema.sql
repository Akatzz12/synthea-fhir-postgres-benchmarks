CREATE TABLE organizations (
    organization_id TEXT PRIMARY KEY,
    name TEXT,
    state TEXT,
    city TEXT
);

CREATE TABLE organization_identifiers (
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    system TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (organization_id, system, value),
    UNIQUE (system, value)
);

CREATE TABLE providers (
    provider_id TEXT PRIMARY KEY,
    first_name TEXT,
    last_name TEXT
);

CREATE TABLE provider_identifiers (
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    system TEXT NOT NULL,
    value TEXT NOT NULL,
    PRIMARY KEY (provider_id, system, value),
    UNIQUE (system, value)
);

CREATE TABLE provider_organizations (
    role_id TEXT PRIMARY KEY,
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    organization_id TEXT NOT NULL REFERENCES organizations(organization_id),
    role_code TEXT NOT NULL DEFAULT ''
);

CREATE TABLE patients (
    patient_id TEXT PRIMARY KEY,
    first_name TEXT,
    last_name TEXT,
    birth_date DATE,
    gender TEXT,
    race TEXT,
    state TEXT,
    deceased_at TIMESTAMPTZ
);

CREATE TABLE encounters (
    encounter_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    organization_id TEXT REFERENCES organizations(organization_id),
    encounter_type TEXT,
    start_time TIMESTAMPTZ,
    end_time TIMESTAMPTZ,
    CHECK (end_time IS NULL OR end_time >= start_time)
);

CREATE TABLE encounter_providers (
    encounter_id TEXT NOT NULL REFERENCES encounters(encounter_id),
    provider_id TEXT NOT NULL REFERENCES providers(provider_id),
    participation_type TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (encounter_id, provider_id, participation_type)
);

CREATE TABLE conditions (
    condition_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    encounter_id TEXT REFERENCES encounters(encounter_id),
    code_system TEXT,
    code TEXT,
    description TEXT,
    onset_at TIMESTAMPTZ,
    abatement_at TIMESTAMPTZ
);

CREATE TABLE medications (
    medication_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    encounter_id TEXT REFERENCES encounters(encounter_id),
    code_system TEXT,
    code TEXT,
    description TEXT,
    authored_at TIMESTAMPTZ,
    status TEXT
);

CREATE TABLE observations (
    observation_id TEXT PRIMARY KEY,
    patient_id TEXT NOT NULL REFERENCES patients(patient_id),
    encounter_id TEXT REFERENCES encounters(encounter_id),
    code_system TEXT,
    code TEXT,
    description TEXT,
    value_numeric DOUBLE PRECISION,
    value_text TEXT,
    value_json JSONB,
    unit TEXT,
    observed_at TIMESTAMPTZ
);

CREATE INDEX encounters_patient_id_idx ON encounters(patient_id);
CREATE INDEX encounters_organization_date_idx ON encounters(organization_id, start_time);
CREATE INDEX encounter_providers_provider_id_idx ON encounter_providers(provider_id);
CREATE INDEX conditions_patient_id_idx ON conditions(patient_id);
CREATE INDEX medications_patient_id_idx ON medications(patient_id);
CREATE INDEX medications_encounter_id_idx ON medications(encounter_id);
CREATE INDEX medications_code_idx ON medications(code_system, code);
CREATE INDEX observations_patient_code_idx ON observations(patient_id, code);
CREATE INDEX observations_observed_at_idx ON observations(observed_at);