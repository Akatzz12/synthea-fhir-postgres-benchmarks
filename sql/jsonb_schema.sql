CREATE TABLE patient_documents (
    patient_id TEXT PRIMARY KEY,
    document JSONB NOT NULL,
    loaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (jsonb_typeof(document) = 'object'),
    CHECK (COALESCE(jsonb_typeof(document -> 'patient') = 'object', FALSE)),
    CHECK (COALESCE(document #>> '{patient,resourceType}' = 'Patient', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'encounters') = 'array', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'conditions') = 'array', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'medications') = 'array', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'observations') = 'array', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'organizations') = 'array', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'providers') = 'array', FALSE)),
    CHECK (COALESCE(jsonb_typeof(document -> 'provider_organizations') = 'array', FALSE))
);

CREATE INDEX patient_documents_document_gin_idx
    ON patient_documents USING GIN (document);

CREATE INDEX patient_documents_patient_gender_idx
    ON patient_documents ((document #>> '{patient,gender}'));