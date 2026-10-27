-- Mart 1: monthly budget vs. actual with a three-way variance decomposition.
--
--   Total variance  = actual cost - budget cost = V_a * P_a - V_b * P_b
--   Volume effect   = (V_a - V_b) * P_b                       more or less energy than planned, at plan price
--   Market effect   = (S_a - B_b) * f_b * V_a                 average spot price vs. planned base price
--   Profile effect  = S_a * (f_a - f_b) * V_a                 WHEN we buy: our demand shape vs. the average hour
--
--   V = volume, P = volume-weighted price, S_a = simple average spot price, B_b = budget base price,
--   f = profile factor (volume-weighted price / average price). The three effects add up exactly to the total.
DROP TABLE IF EXISTS mart_monthly_variance;
CREATE TABLE mart_monthly_variance AS
WITH m AS (
    SELECT
        year,
        month,
        SUM(volume_actual_mwh)                         AS volume_actual_mwh,
        SUM(volume_budget_mwh)                         AS volume_budget_mwh,
        SUM(cost_actual_eur)                           AS cost_actual_eur,
        SUM(cost_budget_eur)                           AS cost_budget_eur,
        AVG(spot_eur_mwh)                              AS avg_spot_eur_mwh,
        MAX(budget_base_price_eur_mwh)                 AS budget_base_price_eur_mwh,
        MAX(budget_profile_factor)                     AS budget_profile_factor,
        MAX(budget_price_eur_mwh)                      AS budget_price_eur_mwh,
        SUM(CASE WHEN spot_eur_mwh < 0 THEN 1 ELSE 0 END) AS negative_price_hours,
        AVG(temp_c)                                    AS avg_temp_c,
        COUNT(*)                                       AS hours
    FROM fct_hourly_procurement
    GROUP BY year, month
), p AS (
    SELECT
        *,
        cost_actual_eur / volume_actual_mwh            AS actual_price_eur_mwh,
        (cost_actual_eur / volume_actual_mwh) / avg_spot_eur_mwh AS actual_profile_factor
    FROM m
)
SELECT
    year,
    month,
    printf('%04d-%02d', year, month)                  AS year_month,
    hours,
    avg_temp_c,
    negative_price_hours,
    volume_budget_mwh,
    volume_actual_mwh,
    budget_price_eur_mwh,
    actual_price_eur_mwh,
    budget_base_price_eur_mwh,
    avg_spot_eur_mwh,
    budget_profile_factor,
    actual_profile_factor,
    cost_budget_eur,
    cost_actual_eur,
    cost_actual_eur - cost_budget_eur                                                     AS variance_total_eur,
    (volume_actual_mwh - volume_budget_mwh) * budget_price_eur_mwh                        AS variance_volume_eur,
    (avg_spot_eur_mwh - budget_base_price_eur_mwh) * budget_profile_factor * volume_actual_mwh AS variance_market_price_eur,
    avg_spot_eur_mwh * (actual_profile_factor - budget_profile_factor) * volume_actual_mwh AS variance_profile_eur
FROM p
ORDER BY year, month;

-- Mart 2: year-to-date view (cumulative budget and actual per year).
DROP TABLE IF EXISTS mart_ytd;
CREATE TABLE mart_ytd AS
SELECT
    year,
    month,
    year_month,
    SUM(cost_budget_eur)    OVER w AS ytd_cost_budget_eur,
    SUM(cost_actual_eur)    OVER w AS ytd_cost_actual_eur,
    SUM(volume_budget_mwh)  OVER w AS ytd_volume_budget_mwh,
    SUM(volume_actual_mwh)  OVER w AS ytd_volume_actual_mwh,
    SUM(variance_total_eur) OVER w AS ytd_variance_eur
FROM mart_monthly_variance
WINDOW w AS (PARTITION BY year ORDER BY month ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW);

-- Mart 3: daily KPIs (input for the dashboard and for anomaly detection).
DROP TABLE IF EXISTS mart_daily_kpis;
CREATE TABLE mart_daily_kpis AS
SELECT
    date_local,
    year,
    month,
    COUNT(*)                                          AS hours,
    SUM(volume_actual_mwh)                            AS volume_mwh,
    SUM(cost_actual_eur)                              AS cost_eur,
    SUM(cost_actual_eur) / SUM(volume_actual_mwh)     AS cost_per_mwh_eur,
    AVG(spot_eur_mwh)                                 AS avg_spot_eur_mwh,
    MIN(spot_eur_mwh)                                 AS min_spot_eur_mwh,
    MAX(spot_eur_mwh)                                 AS max_spot_eur_mwh,
    SUM(CASE WHEN spot_eur_mwh < 0 THEN 1 ELSE 0 END) AS negative_price_hours,
    AVG(temp_c)                                       AS avg_temp_c
FROM fct_hourly_procurement
GROUP BY date_local, year, month
ORDER BY date_local;
