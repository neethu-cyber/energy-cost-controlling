-- Fact table: one row per hour with actual and budget volume, price and cost.
DROP TABLE IF EXISTS fct_hourly_procurement;
CREATE TABLE fct_hourly_procurement AS
SELECT
    h.ts_utc,
    h.ts_local,
    h.date_local,
    h.hour_local,
    h.year,
    h.month,
    h.temp_c,
    h.spot_eur_mwh,
    h.volume_actual_mwh,
    h.volume_actual_mwh * h.spot_eur_mwh       AS cost_actual_eur,
    h.volume_budget_mwh,
    b.base_price_eur_mwh                       AS budget_base_price_eur_mwh,
    b.profile_factor                           AS budget_profile_factor,
    b.budget_price_eur_mwh,
    h.volume_budget_mwh * b.budget_price_eur_mwh AS cost_budget_eur
FROM stg_hourly h
JOIN stg_budget_assumptions b
  ON b.year = h.year AND b.month = h.month;
