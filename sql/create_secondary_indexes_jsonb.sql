\timing on
CREATE INDEX IF NOT EXISTS patient_documents_document_gin_idx
    ON patient_documents USING GIN (document);
CREATE INDEX IF NOT EXISTS patient_documents_patient_gender_idx
    ON patient_documents ((document #>> '{patient,gender}'));
\timing off