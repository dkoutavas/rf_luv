-- PHASE 1: per-database identities for the shared ClickHouse server.
--
-- One physical ClickHouse now hosts all six pipelines (the consolidation:
-- the old per-pipeline ClickHouse containers on ports 8123-8128 / 9000-9005
-- collapse into a single server on 8123 / 9000). Each pipeline still owns its
-- own database and its own least-privilege user; the user name equals the db
-- name and the password is '<db>_local', preserving the credential convention
-- every pipeline's config already hardcodes as its default.
--
-- Applied by infra/bootstrap.sh as the built-in 'default' admin user. Every
-- statement is IF NOT EXISTS, so re-running this file (e.g. ch-bootstrap
-- restarting) is a no-op.

CREATE DATABASE IF NOT EXISTS adsb;
CREATE USER IF NOT EXISTS adsb IDENTIFIED BY 'adsb_local';
GRANT ALL ON adsb.* TO adsb;

CREATE DATABASE IF NOT EXISTS ais;
CREATE USER IF NOT EXISTS ais IDENTIFIED BY 'ais_local';
GRANT ALL ON ais.* TO ais;

CREATE DATABASE IF NOT EXISTS ism;
CREATE USER IF NOT EXISTS ism IDENTIFIED BY 'ism_local';
GRANT ALL ON ism.* TO ism;

CREATE DATABASE IF NOT EXISTS spectrum;
CREATE USER IF NOT EXISTS spectrum IDENTIFIED BY 'spectrum_local';
GRANT ALL ON spectrum.* TO spectrum;

CREATE DATABASE IF NOT EXISTS acars;
CREATE USER IF NOT EXISTS acars IDENTIFIED BY 'acars_local';
GRANT ALL ON acars.* TO acars;

CREATE DATABASE IF NOT EXISTS noaa;
CREATE USER IF NOT EXISTS noaa IDENTIFIED BY 'noaa_local';
GRANT ALL ON noaa.* TO noaa;

-- Cross-grant: the spectrum-acars feedback path (spectrum/acars_feedback.py)
-- now runs against ONE server, so it can read acars.messages directly as the
-- spectrum user instead of the old cross-instance HTTP hop. Read-only.
GRANT SELECT ON acars.* TO spectrum;
