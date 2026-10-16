-- name: patient_history
SELECT document AS patient_history
FROM patient_documents
WHERE patient_id = %s;

-- name: bmi_by_gender
SELECT count(*)::bigint AS observation_count,
       round(avg((observation->'valueQuantity'->>'value')::numeric), 6) AS avg_bmi
FROM patient_documents d
CROSS JOIN LATERAL jsonb_array_elements(d.document->'observations') AS observation
WHERE d.document->'patient'->>'gender' = %s
  AND observation->'code'->'coding'->0->>'system' = 'http://loinc.org'
  AND observation->'code'->'coding'->0->>'code' = '39156-5'
  AND observation->'valueQuantity'->>'value' IS NOT NULL;

-- name: medication_by_region_date
WITH medication_requests AS MATERIALIZED (
    SELECT d.patient_id,
           medication->'encounter'->>'reference' AS encounter_reference
    FROM patient_documents d
    CROSS JOIN LATERAL jsonb_array_elements(d.document->'medications') AS medication
    WHERE medication->'medicationCodeableConcept'->'coding'->0->>'system' = %s
      AND medication->'medicationCodeableConcept'->'coding'->0->>'code' = %s
),
encounters AS MATERIALIZED (
    SELECT d.patient_id,
           'urn:uuid:' || (encounter->>'id') AS encounter_reference,
           encounter->'serviceProvider'->>'reference' AS organization_reference
    FROM patient_documents d
    CROSS JOIN LATERAL jsonb_array_elements(d.document->'encounters') AS encounter
    WHERE (encounter->'period'->>'start')::timestamptz >= %s::timestamptz
      AND (encounter->'period'->>'start')::timestamptz < %s::timestamptz
),
regional_organizations AS MATERIALIZED (
    SELECT d.patient_id,
           'Organization?identifier='
               || (organization->'identifier'->0->>'system')
               || '|'
               || (organization->'identifier'->0->>'value') AS organization_reference
    FROM patient_documents d
    CROSS JOIN LATERAL jsonb_array_elements(d.document->'organizations') AS organization
    WHERE organization->'address'->0->>'state' = %s
)
SELECT DISTINCT medication_requests.patient_id
FROM medication_requests
JOIN encounters USING (patient_id, encounter_reference)
JOIN regional_organizations
  ON regional_organizations.patient_id = encounters.patient_id
 AND regional_organizations.organization_reference = encounters.organization_reference
ORDER BY medication_requests.patient_id;