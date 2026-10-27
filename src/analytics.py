"""Rolling full-year forecast and anomaly detection on top of the SQL marts."""
import numpy as np
import pandas as pd


def rolling_forecast(monthly: pd.DataFrame, year: int, remaining_budget: pd.DataFrame = None,
                     profile_by_month: dict = None, lookback: int = 3) -> pd.DataFrame:
    """Full-year cost forecast at every month-end.

    naive:     actual cost so far + budget cost of the remaining months
    adjusted:  actual cost so far + for each remaining month:
                   budget volume x YTD volume ratio                          (weather / portfolio trend)
                 x budget base price x recent market ratio                   (spot level of the last `lookback` months
                                                                              vs. the budgeted base price)
                 x profile factor                                            (seasonal: last year's actual factor for
                                                                              that month if known, else the budget's)
    remaining_budget: months of the year that have no actuals yet (columns month, volume_budget_mwh,
                      budget_base_price_eur_mwh, budget_profile_factor)
    """
    m = monthly[monthly["year"] == year].sort_values("month").reset_index(drop=True)
    future = remaining_budget if remaining_budget is not None else m.iloc[0:0]
    rows = []
    for i in range(len(m)):
        done = m.iloc[:i + 1]
        rest = pd.concat([m.iloc[i + 1:], future], ignore_index=True)
        recent = done.tail(lookback)
        vol_ratio = done["volume_actual_mwh"].sum() / done["volume_budget_mwh"].sum()
        market_ratio = (recent["avg_spot_eur_mwh"] * recent["hours"]).sum() / \
                       (recent["budget_base_price_eur_mwh"] * recent["hours"]).sum()
        profile = rest["month"].map(profile_by_month) if profile_by_month else rest["budget_profile_factor"]
        profile = profile.fillna(rest["budget_profile_factor"])
        rest_budget_cost = (rest["volume_budget_mwh"] * rest["budget_base_price_eur_mwh"] * rest["budget_profile_factor"]).sum()
        actual_ytd = done["cost_actual_eur"].sum()
        rows.append({
            "year": year,
            "as_of_month": int(m.loc[i, "month"]),
            "actual_ytd_eur": actual_ytd,
            "forecast_naive_eur": actual_ytd + rest_budget_cost,
            "forecast_adjusted_eur": actual_ytd + (rest["volume_budget_mwh"] * vol_ratio
                                                  * rest["budget_base_price_eur_mwh"] * market_ratio * profile).sum(),
            "volume_ratio_ytd": vol_ratio,
            "market_ratio_recent": market_ratio,
            "full_year_budget_eur": m["cost_budget_eur"].sum()
                                    + (future["volume_budget_mwh"] * future["budget_base_price_eur_mwh"]
                                       * future["budget_profile_factor"]).sum(),
        })
    return pd.DataFrame(rows)


def detect_anomalies(daily: pd.DataFrame, window: int = 28, threshold: float = 3.5) -> pd.DataFrame:
    """Robust z-score of the daily cost per MWh against the trailing `window` days (median and MAD).

    Only past days are used, so this works the same way as a daily alert in production.
    """
    d = daily.sort_values("date_local").reset_index(drop=True).copy()
    x = d["cost_per_mwh_eur"]
    med = x.shift(1).rolling(window, min_periods=14).median()
    mad = (x.shift(1) - med).abs().rolling(window, min_periods=14).median()
    d["baseline_eur_mwh"] = med
    d["robust_z"] = (x - med) / (1.4826 * mad.replace(0, np.nan))
    d["is_anomaly"] = d["robust_z"].abs() > threshold
    d["direction"] = np.where(d["robust_z"] > 0, "expensive", "cheap")

    def category(r):
        if not r["is_anomaly"]:
            return ""
        if r["negative_price_hours"] >= 4:
            return "negative prices (sunny/windy, low demand)"
        if r["max_spot_eur_mwh"] > 300:
            return "price spike (> 300 EUR/MWh)"
        return "high price level all day" if r["direction"] == "expensive" else "low price level all day"

    d["category"] = d.apply(category, axis=1)
    return d
