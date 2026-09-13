-- Extensions the schema relies on (spec §36).
CREATE EXTENSION IF NOT EXISTS "pgcrypto";   -- gen_random_uuid()
CREATE EXTENSION IF NOT EXISTS "btree_gin";  -- composite tenant/time indexes

-- All timestamps are stored UTC timestamptz (spec §57); site-local rendering happens
-- in the application layer using each site's IANA timezone.
ALTER DATABASE nightshift SET timezone TO 'UTC';
