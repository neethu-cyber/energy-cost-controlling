-- Data quality tests. Each test is a query that must return ZERO rows; any row returned is a failure.

-- test: no duplicate hours
SELECT ts_utc, COUNT(*) AS n FROM fct_hourly_procurement GROUP BY ts_utc HAVING COUNT(*) > 1;

-- test: no missing hours inside the data range (consecutive UTC hours)
SELECT a.ts_utc
FROM fct_hourly_procurement a
LEFT JOIN fct_hourly_procurement b ON b.ts_utc = datetime(a.ts_utc, '+1 hour')
WHERE b.ts_utc IS NULL
  AND a.ts_utc < (SELECT MAX(ts_utc) FROM fct_hourly_procurement);

-- test: every hour has a price and a budget
SELECT ts_utc FROM fct_hourly_procurement WHERE spot_eur_mwh IS NULL OR budget_price_eur_mwh IS NULL;

-- test: volumes are non-negative
SELECT ts_utc FROM fct_hourly_procurement WHERE volume_actual_mwh < 0 OR volume_budget_mwh < 0;

-- test: prices within the day-ahead market limits (-500 to 4000 EUR/MWh)
SELECT ts_utc, spot_eur_mwh FROM fct_hourly_procurement WHERE spot_eur_mwh < -500 OR spot_eur_mwh > 4000;

-- test: variance components add up to the total variance
SELECT year_month FROM mart_monthly_variance
WHERE ABS(variance_volume_eur + variance_market_price_eur + variance_profile_eur - variance_total_eur) > 0.01;

-- test: monthly costs reconcile with the hourly fact table
SELECT 1 FROM (SELECT SUM(cost_actual_eur) AS a FROM fct_hourly_procurement),
              (SELECT SUM(cost_actual_eur) AS b FROM mart_monthly_variance)
WHERE ABS(a - b) > 0.01;
