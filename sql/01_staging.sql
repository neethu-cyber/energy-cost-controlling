-- Staging: clean, typed hourly data with calendar fields in German local time.
DROP VIEW IF EXISTS stg_hourly;
CREATE VIEW stg_hourly AS
SELECT
    ts_utc,
    ts_local,
    substr(ts_local, 1, 10)                 AS date_local,
    CAST(substr(ts_local, 12, 2) AS INTEGER) AS hour_local,
    year,
    month,
    spot_eur_mwh,
    temp_c,
    ghi_w_m2,
    volume_actual_mwh,
    volume_budget_mwh
FROM raw_hourly
WHERE spot_eur_mwh IS NOT NULL;

DROP VIEW IF EXISTS stg_budget_assumptions;
CREATE VIEW stg_budget_assumptions AS
SELECT
    year,
    month,
    normal_temp_c,
    base_price_eur_mwh,
    profile_factor,
    base_price_eur_mwh * profile_factor AS budget_price_eur_mwh
FROM seed_budget;
