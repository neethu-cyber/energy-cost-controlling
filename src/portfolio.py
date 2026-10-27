"""Load real market and weather data and model the hourly grid demand of a portfolio of 1,000 homes.

Real data (data/):
  SMARD day-ahead prices, Germany/Luxembourg, hourly, German local time
  Open-Meteo hourly weather for Nürnberg (fixed UTC+1, values average the preceding hour)

Portfolio (modelled, no public meter data exists):
  1,000 homes, 3,500 kWh/year base load each
  600 with a heat pump (demand depends on outdoor temperature)
  700 with 8 kWp rooftop PV (reduces what must be bought from the market)
  300 with an EV charging 18:00-23:00

The same demand model is run twice: with the ACTUAL temperature (actual volume) and with temperatures shifted
to long-term monthly normals (budget volume). That is how a utility builds a weather-normalised volume budget.
"""
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
TZ = "Europe/Berlin"
HOMES, HP_HOMES, PV_HOMES, EV_HOMES = 1000, 600, 700, 300


def load_prices() -> pd.DataFrame:
    p = pd.read_csv(ROOT / "data" / "smard_day_ahead_prices_hourly.csv", sep=";", encoding="utf-8-sig", na_values="-")
    start = pd.to_datetime(p["Start date"], format="%b %d, %Y %I:%M %p").dt.tz_localize(TZ, ambiguous="infer")
    return pd.DataFrame({"ts_utc": start.dt.tz_convert("UTC"),
                         "spot_eur_mwh": pd.to_numeric(p.iloc[:, 2], errors="coerce")})


def load_weather() -> pd.DataFrame:
    w = pd.read_csv(ROOT / "data" / "open_meteo_nuernberg_hourly.csv", skiprows=3)
    w.columns = ["time", "temp_c", "ghi_w_m2", "cloud_pct"]
    w["ts_utc"] = (pd.to_datetime(w["time"]) - pd.Timedelta(hours=2)).dt.tz_localize("UTC")
    return w.drop(columns="time")


def demand_mwh(local: pd.Series, temp: np.ndarray, ghi: np.ndarray, seed: int = 5) -> np.ndarray:
    """Hourly grid demand of the whole portfolio in MWh."""
    rng = np.random.default_rng(seed)
    hour, dow, doy = local.dt.hour.to_numpy(), local.dt.dayofweek.to_numpy(), local.dt.dayofyear.to_numpy()
    wk = np.array([.30, .26, .24, .24, .25, .32, .55, .70, .55, .45, .42, .45,
                   .55, .48, .42, .42, .48, .62, .78, .85, .80, .68, .52, .38])
    we = np.array([.32, .28, .25, .24, .24, .26, .32, .45, .62, .70, .66, .65,
                   .70, .60, .52, .50, .55, .66, .80, .86, .80, .68, .55, .40])
    shape = np.where(dow >= 5, we[hour], wk[hour]) * (1 + 0.12 * np.cos(2 * np.pi * (doy - 15) / 365))
    base_kw = shape * 3500 / (shape.mean() * 8760)                       # per home, ~3,500 kWh/year
    base_kw = base_kw * rng.normal(1, 0.03, len(hour))                   # portfolio-level noise is small

    heat_kw = 0.20 * np.clip(15 - temp, 0, None) + 0.25                  # ~12 MWh space heat + ~2 MWh hot water per year
    cop = np.clip(0.45 * 313.15 / (40 - temp), 2.0, 5.5)
    hp_kw = heat_kw / cop

    pv_kw = 8 * ghi / 1000 * 0.9
    ev_kw = np.where((hour >= 18) & (hour < 23), 1.6, 0.0)

    home = base_kw
    hp_home = base_kw + hp_kw
    # grid demand per home type after own PV (excess PV is exported, not netted against other homes)
    total = (HOMES - HP_HOMES) * np.clip(home - pv_kw * (PV_HOMES / HOMES), 0, None) \
        + HP_HOMES * np.clip(hp_home - pv_kw * (PV_HOMES / HOMES), 0, None) \
        + EV_HOMES * ev_kw
    return total / 1000.0                                                 # kW over one hour -> MWh


def build_hourly() -> pd.DataFrame:
    df = load_prices().merge(load_weather(), on="ts_utc", how="inner").sort_values("ts_utc").reset_index(drop=True)
    df["ts_local"] = df["ts_utc"].dt.tz_convert(TZ)
    df["year"], df["month"] = df["ts_local"].dt.year, df["ts_local"].dt.month
    df = df[df["year"].isin([2025, 2026])].reset_index(drop=True)

    budget = pd.read_csv(ROOT / "seeds" / "budget_assumptions.csv")
    actual_monthly_temp = df.groupby(["year", "month"])["temp_c"].transform("mean")
    normal = df.merge(budget, on=["year", "month"], how="left")["normal_temp_c"].to_numpy()
    temp_normalised = df["temp_c"].to_numpy() - actual_monthly_temp.to_numpy() + normal

    df["volume_actual_mwh"] = demand_mwh(df["ts_local"], df["temp_c"].to_numpy(), df["ghi_w_m2"].to_numpy())
    df["volume_budget_mwh"] = demand_mwh(df["ts_local"], temp_normalised, df["ghi_w_m2"].to_numpy())
    df["ts_local"] = df["ts_local"].dt.strftime("%Y-%m-%d %H:%M:%S%z")
    df["ts_utc"] = df["ts_utc"].dt.strftime("%Y-%m-%d %H:%M:%S")
    return df
