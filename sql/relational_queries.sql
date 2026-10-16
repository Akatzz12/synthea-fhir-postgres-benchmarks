-- name: patient_history
SELECT jsonb_build_object(
    'patient', to_jsonb(p),
    'encounters', COALESCE((
        SELECT jsonb_agg(to_jsonb(e) ORDER BY e.encounter_id)
        FROM encounters e
        WHERE e.patient_id = p.patient_id
    ), '[]'::jsonb),
    'conditions', COALESCE((
        SELECT jsonb_agg(to_jsonb(c) ORDER BY c.condition_id)
        FROM conditions c
        WHERE c.patient_id = p.patient_id
    ), '[]'::jsonb),
    'medications', COALESCE((
        SELECT jsonb_agg(to_jsonb(m) ORDER BY m.medication_id)
        FROM medications m
        WHERE m.patient_id = p.patient_id
    ), '[]'::jsonb),
    'observations', COALESCE((
        SELECT jsonb_agg(to_jsonb(o) ORDER BY o.observation_id)
        FROM observations o
        WHERE o.patient_id = p.patient_id
    ), '[]'::jsonb),
    'organizations', COALESCE((
        SELECT jsonb_agg(to_jsonb(o) ORDER BY o.organization_id)
        FROM organizations o
        WHERE o.organization_id IN (
            SELECT e.organization_id
            FROM encounters e
            WHERE e.patient_id = p.patient_id
              AND e.organization_id IS NOT NULL
            UNION
            SELECT po.organization_id
            FROM provider_organizations po
            JOIN encounter_providers ep ON ep.provider_id = po.provider_id
            JOIN encounters e ON e.encounter_id = ep.encounter_id
            WHERE e.patient_id = p.patient_id
        )
    ), '[]'::jsonb),
    'providers', COALESCE((
        SELECT jsonb_agg(to_jsonb(pr) ORDER BY pr.provider_id)
        FROM providers pr
        WHERE pr.provider_id IN (
            SELECT ep.provider_id
            FROM encounter_providers ep
            JOIN encounters e ON e.encounter_id = ep.encounter_id
            WHERE e.patient_id = p.patient_id
        )
    ), '[]'::jsonb),
    'provider_organizations', COALESCE((
        SELECT jsonb_agg(to_jsonb(po) ORDER BY po.role_id)
        FROM provider_organizations po
        WHERE po.provider_id IN (
            SELECT ep.provider_id
            FROM encounter_providers ep
            JOIN encounters e ON e.encounter_id = ep.encounter_id
            WHERE e.patient_id = p.patient_id
        )
    ), '[]'::jsonb)
) AS patient_history
FROM patients p
WHERE p.patient_id = %s;

-- name: bmi_by_gender
SELECT count(*)::bigint AS observation_count,
       round(avg(o.value_numeric)::numeric, 6) AS avg_bmi
FROM patients p
JOIN observations o ON o.patient_id = p.patient_id
WHERE p.gender = %s
  AND o.code_system = 'http://loinc.org'
  AND o.code = '39156-5'
  AND o.value_numeric IS NOT NULL;

-- name: medication_by_region_date
SELECT DISTINCT p.patient_id
FROM patients p
JOIN medications m ON m.patient_id = p.patient_id
JOIN encounters e ON e.encounter_id = m.encounter_id
JOIN organizations o ON o.organization_id = e.organization_id
WHERE m.code_system = %s
  AND m.code = %s
  AND o.state = %s
  AND e.start_time >= %s::timestamptz
  AND e.start_time < %s::timestamptz
ORDER BY p.patient_id;