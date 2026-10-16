\pset pager off
SELECT current_database() AS database_name,
       pg_database_size(current_database()) AS database_size_bytes;

SELECT relation.relname AS table_name,
       pg_relation_size(relation.oid) AS table_bytes,
       pg_indexes_size(relation.oid) AS index_bytes,
       pg_total_relation_size(relation.oid) AS total_bytes
FROM pg_class AS relation
JOIN pg_namespace AS schema ON schema.oid = relation.relnamespace
WHERE schema.nspname = 'public'
  AND relation.relkind = 'r'
ORDER BY relation.relname;