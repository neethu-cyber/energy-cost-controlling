"""End-to-end pipeline: load real data -> SQLite -> SQL models -> data tests -> analytics -> dashboard exports.

Usage:  python src/run.py     (from the project root)
Outputs: results/ (CSV + charts), results/looker_studio/ (CSV files for the dashboard), build/controlling.db
"""
import re
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from analytics import detect_anomalies, rolling_forecast
from portfolio import ROOT, build_hourly

OUT = ROOT / "results"
LOOKER = OUT / "looker_studio"
BUILD = ROOT / "build"
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def run_sql_file(con, path):
    con.executescript(Path(path).read_text())


def run_tests(con, path):
    text = Path(path).read_text()
    tests = re.findall(r"-- test: (.+?)\n(.*?;)", text, flags=re.S)
    results = []
    for name, sql in tests:
        failures = len(con.execute(sql).fetchall())
        results.append((name.strip(), failures))
    return results


def main():
    for p in (OUT, LOOKER, BUILD):
        p.mkdir(exist_ok=True, parents=True)
    db = BUILD / "controlling.db"
    db.unlink(missing_ok=True)
    con = sqlite3.connect(db)

    print("1. Loading real SMARD prices and Open-Meteo weather, modelling portfolio demand ...")
    hourly = build_hourly()
    hourly.drop(columns=["cloud_pct"]).to_sql("raw_hourly", con, index=False)
    pd.read_csv(ROOT / "seeds" / "budget_assumptions.csv").to_sql("seed_budget", con, index=False)

    print("2. Running SQL models (staging -> facts -> marts) ...")
    for f in ["01_staging.sql", "02_facts.sql", "03_marts.sql"]:
        run_sql_file(con, ROOT / "sql" / f)

    print("3. Running data quality tests ...")
    tests = run_tests(con, ROOT / "tests" / "data_tests.sql")
    for name, failures in tests:
        print(f"   {'PASS' if failures == 0 else 'FAIL'}  {name}" + (f"  ({failures} rows)" if failures else ""))
    if any(f for _, f in tests):
        raise SystemExit("Data tests failed")

    monthly = pd.read_sql("SELECT * FROM mart_monthly_variance", con)
    daily = pd.read_sql("SELECT * FROM mart_daily_kpis", con)

    print("4. Rolling forecast and anomaly detection ...")
    fc25 = rolling_forecast(monthly, 2025)
    # Lesson learned from 2025: the budget's flat profile factor (1.10) was too low for a PV-heavy portfolio.
    # For 2026, use each month's ACTUAL 2025 profile factor, and budget Oct-Dec with 2025's weather-normal volume.
    m25 = monthly[monthly["year"] == 2025].set_index("month")
    learned = m25["actual_profile_factor"].to_dict()
    seed = pd.read_csv(ROOT / "seeds" / "budget_assumptions.csv")
    missing = sorted(set(range(1, 13)) - set(monthly.loc[monthly["year"] == 2026, "month"]))
    rem26 = pd.DataFrame({
        "month": missing,
        "volume_budget_mwh": [m25.loc[k, "volume_budget_mwh"] for k in missing],
        "budget_base_price_eur_mwh": [seed.query("year == 2026 and month == @k")["base_price_eur_mwh"].iloc[0] for k in missing],
        "budget_profile_factor": [seed.query("year == 2026 and month == @k")["profile_factor"].iloc[0] for k in missing],
    })
    fc26 = rolling_forecast(monthly, 2026, remaining_budget=rem26, profile_by_month=learned)
    y26m = monthly[monthly["year"] == 2026].copy()
    y26m["profile_learned"] = y26m["month"].map(learned)
    y26m["cost_budget_rebased_eur"] = y26m["volume_budget_mwh"] * y26m["budget_base_price_eur_mwh"] * y26m["profile_learned"]
    y26m["profile_effect_rebased_eur"] = (y26m["avg_spot_eur_mwh"] * (y26m["actual_profile_factor"] - y26m["profile_learned"])
                                         * y26m["volume_actual_mwh"])
    rebase = pd.Series({
        "ytd_budget_original_eur": y26m["cost_budget_eur"].sum(),
        "ytd_budget_rebased_eur": y26m["cost_budget_rebased_eur"].sum(),
        "ytd_actual_eur": y26m["cost_actual_eur"].sum(),
        "profile_effect_original_eur": y26m["variance_profile_eur"].sum(),
        "profile_effect_rebased_eur": y26m["profile_effect_rebased_eur"].sum(),
        "market_price_effect_eur": y26m["variance_market_price_eur"].sum(),
        "volume_effect_eur": y26m["variance_volume_eur"].sum(),
    })
    rebase.to_csv(OUT / "budget_rebase_2026.csv", header=["value"])
    final25 = monthly.loc[monthly["year"] == 2025, "cost_actual_eur"].sum()
    fc25["final_actual_eur"] = final25
    for k in ["naive", "adjusted"]:
        fc25[f"abs_error_{k}_pct"] = 100 * (fc25[f"forecast_{k}_eur"] - final25).abs() / final25
    forecast = pd.concat([fc25, fc26], ignore_index=True)
    forecast.to_sql("mart_rolling_forecast", con, index=False)

    anomalies = detect_anomalies(daily)
    anomalies.to_sql("mart_daily_anomalies", con, index=False)
    con.close()

    # ---------- exports ----------
    monthly.to_csv(OUT / "monthly_variance.csv", index=False)
    forecast.to_csv(OUT / "rolling_forecast.csv", index=False)
    flagged = anomalies[anomalies["is_anomaly"]][["date_local", "cost_per_mwh_eur", "baseline_eur_mwh", "robust_z",
                                                  "direction", "category", "negative_price_hours", "min_spot_eur_mwh",
                                                  "max_spot_eur_mwh", "avg_temp_c"]]
    flagged.to_csv(OUT / "anomalies.csv", index=False)
    monthly.to_csv(LOOKER / "monthly_variance.csv", index=False)
    anomalies.to_csv(LOOKER / "daily_kpis_with_anomalies.csv", index=False)
    forecast.to_csv(LOOKER / "rolling_forecast.csv", index=False)

    # ---------- console summary ----------
    y25 = monthly[monthly["year"] == 2025]
    y26 = monthly[monthly["year"] == 2026]
    fy = lambda d, c: d[c].sum()
    pd.set_option("display.width", 220, "display.max_columns", 30)
    print("\n=== FY 2025: budget vs. actual (EUR) ===")
    summary25 = pd.Series({
        "budget_cost": fy(y25, "cost_budget_eur"), "actual_cost": fy(y25, "cost_actual_eur"),
        "total_variance": fy(y25, "variance_total_eur"), "volume_effect": fy(y25, "variance_volume_eur"),
        "market_price_effect": fy(y25, "variance_market_price_eur"), "profile_effect": fy(y25, "variance_profile_eur"),
        "budget_volume_mwh": fy(y25, "volume_budget_mwh"), "actual_volume_mwh": fy(y25, "volume_actual_mwh"),
        "budget_price_eur_mwh": fy(y25, "cost_budget_eur") / fy(y25, "volume_budget_mwh"),
        "actual_price_eur_mwh": fy(y25, "cost_actual_eur") / fy(y25, "volume_actual_mwh"),
        "avg_spot_eur_mwh": (y25["avg_spot_eur_mwh"] * y25["hours"]).sum() / y25["hours"].sum(),
    })
    print(summary25.to_string(float_format=lambda v: f"{v:,.1f}"))
    print("\n=== Monthly variance 2025 (EUR) ===")
    print(y25[["year_month", "avg_temp_c", "volume_budget_mwh", "volume_actual_mwh", "budget_price_eur_mwh",
               "actual_price_eur_mwh", "actual_profile_factor", "variance_total_eur", "variance_volume_eur",
               "variance_market_price_eur", "variance_profile_eur"]].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))
    print("\n=== Rolling forecast 2025 (full-year cost, EUR) ===")
    print(fc25[["as_of_month", "forecast_naive_eur", "forecast_adjusted_eur", "abs_error_naive_pct",
                "abs_error_adjusted_pct"]].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))
    print(f"   mean abs error Jan-Nov: naive {fc25['abs_error_naive_pct'][:-1].mean():.1f}%, "
          f"adjusted {fc25['abs_error_adjusted_pct'][:-1].mean():.1f}%")
    print("\n=== 2026: rolling outlook (EUR) ===")
    print(fc26[["as_of_month", "actual_ytd_eur", "forecast_naive_eur", "forecast_adjusted_eur", "market_ratio_recent",
                "full_year_budget_eur"]].to_string(index=False, float_format=lambda v: f"{v:,.2f}"))
    print("\n=== 2026 Jan-Sep: original budget vs. budget rebased on 2025 profile factors (EUR) ===")
    print(rebase.to_string(float_format=lambda v: f"{v:,.0f}"))
    print(f"\n=== Anomalies: {len(flagged)} of {len(anomalies)} days flagged ===")
    print(flagged.groupby(["category"]).size().to_string())
    top = flagged.reindex(flagged["robust_z"].abs().sort_values(ascending=False).index).head(8)
    print(top[["date_local", "cost_per_mwh_eur", "baseline_eur_mwh", "robust_z", "max_spot_eur_mwh", "negative_price_hours",
               "category"]].to_string(index=False, float_format=lambda v: f"{v:,.1f}"))

    summary25.to_csv(OUT / "fy2025_summary.csv", header=["value"])
    plot_monthly(y25)
    plot_bridge(summary25)
    plot_forecast(fc25, final25, fc26)
    plot_anomalies(anomalies)
    print(f"\nResults written to {OUT}")


def plot_monthly(y25):
    fig, ax = plt.subplots(figsize=(10, 4.5))
    x = np.arange(len(y25))
    ax.bar(x - 0.2, y25["cost_budget_eur"] / 1000, 0.4, label="Budget", color="#adb5bd")
    ax.bar(x + 0.2, y25["cost_actual_eur"] / 1000, 0.4, label="Actual", color="#1d3557")
    for xi, v, a in zip(x, y25["variance_total_eur"], y25["cost_actual_eur"]):
        ax.text(xi + 0.2, a / 1000 + 1, f"{v / 1000:+.0f}k", ha="center", fontsize=7,
                color="#c1121f" if v > 0 else "#2a9d8f")
    ax.set_xticks(x, MONTHS)
    ax.set_ylabel("Procurement cost [thousand EUR]")
    ax.set_title("Energy procurement 2025: budget vs. actual (1,000 homes, real SMARD prices)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(OUT / "monthly_budget_vs_actual_2025.png", dpi=150)
    plt.close(fig)


def plot_bridge(s):
    steps = [("Budget", s["budget_cost"]), ("Volume effect", s["volume_effect"]),
             ("Market price effect", s["market_price_effect"]), ("Profile effect", s["profile_effect"]),
             ("Actual", s["actual_cost"])]
    fig, ax = plt.subplots(figsize=(9, 4.8))
    running = 0
    for i, (name, v) in enumerate(steps):
        if i in (0, len(steps) - 1):
            ax.bar(i, v / 1000, color="#1d3557")
            ax.text(i, v / 1000 + 4, f"{v / 1000:,.0f}k", ha="center", fontsize=9)
            running = v
        else:
            bottom = running if v >= 0 else running + v
            ax.bar(i, abs(v) / 1000, bottom=bottom / 1000, color="#c1121f" if v > 0 else "#2a9d8f")
            ax.text(i, (running + max(v, 0)) / 1000 + 4, f"{v / 1000:+,.0f}k", ha="center", fontsize=9)
            running += v
    ax.set_xticks(range(len(steps)), [n for n, _ in steps])
    ax.set_ylabel("Thousand EUR")
    ax.set_title("FY 2025 variance bridge: why actual cost differs from budget")
    ax.set_ylim(0, max(s["budget_cost"], s["actual_cost"]) / 1000 * 1.15)
    fig.tight_layout()
    fig.savefig(OUT / "variance_bridge_fy2025.png", dpi=150)
    plt.close(fig)


def plot_forecast(fc, final, fc26=None):
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.6))
    ax = axes[0]
    x = fc["as_of_month"]
    ax.plot(x, fc["forecast_naive_eur"] / 1000, "o-", color="#adb5bd", label="Naive (actual YTD + remaining budget)")
    ax.plot(x, fc["forecast_adjusted_eur"] / 1000, "o-", color="#1d3557", label="Adjusted (volume, market and profile)")
    ax.axhline(final / 1000, color="#2a9d8f", ls="--", label=f"Final actual {final / 1000:,.0f}k")
    ax.axhline(fc["full_year_budget_eur"].iloc[0] / 1000, color="#c1121f", ls=":", label="Budget")
    ax.set_xticks(range(1, 13), MONTHS)
    ax.set_xlabel("Forecast made at the end of")
    ax.set_ylabel("Full-year cost forecast [thousand EUR]")
    ax.set_title("2025: rolling forecast vs. final result")
    ax.legend(fontsize=7)
    ax.grid(alpha=0.3)
    if fc26 is not None:
        ax = axes[1]
        x = fc26["as_of_month"]
        ax.plot(x, fc26["forecast_naive_eur"] / 1000, "o-", color="#adb5bd", label="Naive")
        ax.plot(x, fc26["forecast_adjusted_eur"] / 1000, "o-", color="#1d3557", label="Adjusted")
        ax.plot(x, fc26["actual_ytd_eur"] / 1000, "s--", color="#2a9d8f", label="Actual so far (YTD)")
        ax.axhline(fc26["full_year_budget_eur"].iloc[0] / 1000, color="#c1121f", ls=":", label="Budget 2026")
        ax.set_xticks(range(1, 13), MONTHS)
        ax.set_xlabel("Forecast made at the end of")
        ax.set_title("2026: early warning of a budget overrun")
        ax.legend(fontsize=7, loc="lower right")
        ax.grid(alpha=0.3)
    fig.tight_layout()
    fig.savefig(OUT / "rolling_forecast.png", dpi=150)
    plt.close(fig)


def plot_anomalies(a):
    d = a.copy()
    d["date"] = pd.to_datetime(d["date_local"])
    fig, ax = plt.subplots(figsize=(12, 4.5))
    ax.plot(d["date"], d["cost_per_mwh_eur"], color="#1d3557", lw=0.8, label="Daily cost per MWh")
    ax.plot(d["date"], d["baseline_eur_mwh"], color="#adb5bd", lw=1.2, label="28-day baseline (median)")
    f = d[d["is_anomaly"]]
    for direction, color, label in [("expensive", "#c1121f", "Flagged: unusually expensive day"),
                                    ("cheap", "#2a9d8f", "Flagged: unusually cheap day")]:
        g = f[f["direction"] == direction]
        ax.scatter(g["date"], g["cost_per_mwh_eur"], color=color, s=30, zorder=3, label=label)
    ax.axhline(0, color="#999999", lw=0.6)
    ax.set_ylabel("EUR per MWh bought")
    ax.set_title("Daily procurement cost per MWh with automatic anomaly flags (Jan 2025 - Sep 2026)")
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(OUT / "daily_anomalies.png", dpi=150)
    plt.close(fig)


if __name__ == "__main__":
    main()
