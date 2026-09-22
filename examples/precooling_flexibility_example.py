"""Demand-side flexibility: pre-cool the buildings, coast through the peak.

Runs the same three-building network twice over identical weather:

  * tracking: every zone holds its setpoint. The inflexible reference.
  * precool: zones are pre-cooled in the cheap early-morning hours, then
                     allowed to coast up through the expensive afternoon block,
                     always inside their comfort band.

This is flexibility that has nothing to do with a storage tank: it comes from
the buildings' own thermal mass. Representing it at all requires the indoor
temperature to be a state variable. A building modelled as a single scalar
``Q`` cannot express it at all.

    python precooling_flexibility_example.py
    python precooling_flexibility_example.py --precool-hours 2 7 --coast-hours 13 18
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from discoolpy import (
    assess_flexibility,
    load_yaml_config,
    make_weather_and_load_profiles,
    output_path,
    run_configured_case,
)

warnings.filterwarnings("ignore", category=FutureWarning)

HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "configs" / "config_precooling_flexibility.yaml"

#: With load_model 'thermal_mass' the profile supplies internal gains only, so
#: the archetype curve is scaled to a plausible internal-gain fraction of peak.
INTERNAL_GAIN_FRACTION = 0.22

#: Hours of rate-limited recovery after the coast window, to avoid a rebound peak.
RECOVERY_HOURS = 4


def build_profile(config, precool_hours, coast_hours) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (tracking_profile, precooling_profile) differing only in control mode."""
    base = make_weather_and_load_profiles(config)
    labels = [b["label"] for b in config["buildings"]]
    for label in labels:
        base[f"{label}_Q_W"] *= INTERNAL_GAIN_FRACTION

    tracking = base.copy()
    for label in labels:
        tracking[f"{label}_mass_mode"] = "track"

    flexible = base.copy()
    hour = pd.to_datetime(flexible["timestamp"]).dt.hour
    for offset, label in enumerate(labels):
        # Stagger the pre-cool starts by an hour per building. Together with the
        # per-building precool_max_cooling_W this keeps the pre-cool block below
        # the afternoon peak instead of replacing it with a bigger night-time
        # one, the failure mode of naive "everyone pre-cools at 03:00" control.
        start = precool_hours[0] + offset
        mode = pd.Series("track", index=flexible.index)
        mode[(hour >= start) & (hour < precool_hours[1])] = "precool"
        mode[(hour >= coast_hours[0]) & (hour < coast_hours[1])] = "coast"
        # Rate-limited recovery after the event. Returning straight to 'track'
        # from the band ceiling makes every coil in the district go to full
        # output at the same instant, and that rebound peak can exceed the one
        # the event was called to avoid.
        mode[(hour >= coast_hours[1]) & (hour < coast_hours[1] + RECOVERY_HOURS)] = "recover"
        flexible[f"{label}_mass_mode"] = mode.to_numpy()
    return tracking, flexible


def plot(config, tracking: pd.DataFrame, flexible: pd.DataFrame, labels) -> Path:
    t = pd.to_datetime(tracking["timestamp"])
    plt.style.use("seaborn-v0_8-whitegrid")
    fig, axes = plt.subplots(4, 1, figsize=(13, 15), sharex=True, facecolor="white")
    for ax in axes:
        ax.set_facecolor("white")

    axes[0].plot(t, tracking["compressor_power_W"] / 1e3, color="tab:red", label="Setpoint tracking")
    axes[0].plot(t, flexible["compressor_power_W"] / 1e3, color="tab:blue", label="Pre-cool + coast")
    axes[0].set_ylabel("Compressor power [kW]")
    axes[0].set_title("Electrical demand: the whole point of the exercise")
    axes[0].legend(loc="upper left")

    axes[1].plot(t, tracking["chiller_Q_evap_W"] / 1e3, color="tab:red", ls="--", label="Plant duty, tracking")
    axes[1].plot(t, flexible["chiller_Q_evap_W"] / 1e3, color="tab:blue", label="Plant duty, flexible")
    axes[1].plot(t, tracking["actual_building_total_Q_W"] / 1e3, color="black", lw=1,
                 label="Building cooling delivered, tracking")
    axes[1].set_ylabel("Cooling [kW]")
    axes[1].set_title("Plant duty versus cooling delivered (gap = pipe gain + pump heat)")
    axes[1].legend(loc="upper left", fontsize=9)

    for label, colour in zip(labels, ["tab:blue", "tab:orange", "tab:green"]):
        safe = label.replace(" ", "_")
        axes[2].plot(t, flexible[f"{safe}_T_indoor_degC"], color=colour, label=f"{label} (flexible)")
        axes[2].plot(t, tracking[f"{safe}_T_indoor_degC"], color=colour, ls=":", alpha=0.7,
                     label=f"{label} (tracking)")
    axes[2].set_ylabel("Indoor temperature [degC]")
    axes[2].set_title("Comfort is the constraint: zones stay inside their band throughout")
    axes[2].legend(loc="upper left", fontsize=8, ncol=2)

    axes[3].plot(t, tracking["pipe_heat_gain_W"] / 1e3, color="tab:brown", label="Pipe heat gain, tracking")
    axes[3].plot(t, flexible["pipe_heat_gain_W"] / 1e3, color="tab:olive", label="Pipe heat gain, flexible")
    axes[3].set_ylabel("Pipe gain [kW]")
    axes[3].set_xlabel("Timestamp")
    axes[3].set_title("Distribution heat gain: near-constant, because the soil barely moves")
    axes[3].legend(loc="upper left")

    fig.autofmt_xdate()
    fig.tight_layout()
    path = output_path(config, "plot_png", "precooling_results.png")
    fig.savefig(path, dpi=160, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


def run(config_path=DEFAULT_CONFIG, precool_hours=(3, 8), coast_hours=(13, 18),
        periods=None, quiet=False):
    config = load_yaml_config(config_path)
    if periods is not None:
        config.setdefault("profiles", {})["periods"] = int(periods)
    labels = [b["label"] for b in config["buildings"]]

    tracking_profile, flexible_profile = build_profile(config, precool_hours, coast_hours)
    tracking_profile.to_csv(output_path(config, "profile_csv", "input_profile.csv"), index=False)

    tracking = run_configured_case(config, tracking_profile, "tracking", use_storage=False,
                                   progress=not quiet)
    tracking.to_csv(output_path(config, "baseline_csv", "setpoint_tracking_results.csv"), index=False)

    flexible = run_configured_case(config, flexible_profile, "precooling", use_storage=False,
                                   progress=not quiet)
    flexible.to_csv(output_path(config, "flexible_csv", "precooling_results.csv"), index=False)

    economics = config.get("economics") or {}
    report = assess_flexibility(
        tracking, flexible,
        reference_name="setpoint_tracking", flexible_name="precool_and_coast",
        tariff=economics.get("tariff"),
        carbon_intensity=economics.get("carbon_intensity_kg_kWh"),
        peak_hours=range(coast_hours[0], coast_hours[1]),
    )
    report.notes.append(
        f"Flexibility here comes entirely from building thermal mass: pre-cooling "
        f"{precool_hours[0]:02d}:00-{precool_hours[1]:02d}:00 and coasting "
        f"{coast_hours[0]:02d}:00-{coast_hours[1]:02d}:00, with no cold store in the system."
    )
    for label in labels:
        safe = label.replace(" ", "_")
        report.notes.append(
            f"{label}: indoor temperature ranged "
            f"{flexible[f'{safe}_T_indoor_degC'].min():.2f}-"
            f"{flexible[f'{safe}_T_indoor_degC'].max():.2f} degC "
            f"(band {[b for b in config['buildings'] if b['label'] == label][0]['thermal_mass']['min_temperature_degC']}"
            f"-{[b for b in config['buildings'] if b['label'] == label][0]['thermal_mass']['max_temperature_degC']} degC)."
        )

    summary = output_path(config, "summary_md", "precooling_flexibility_summary.md")
    summary.write_text(report.to_markdown() + "\n", encoding="utf-8")
    plot_path = plot(config, tracking, flexible, labels)

    if not quiet:
        print()
        print(report.to_markdown())
        print(f"\nSummary : {summary}\nPlot    : {plot_path}")
    return tracking, flexible, report


def main() -> None:
    parser = argparse.ArgumentParser(description="Building pre-cooling flexibility example.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG))
    parser.add_argument("--precool-hours", type=int, nargs=2, default=(3, 8), metavar=("START", "END"))
    parser.add_argument("--coast-hours", type=int, nargs=2, default=(13, 18), metavar=("START", "END"))
    parser.add_argument("--periods", type=int, default=None)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    run(args.config, tuple(args.precool_hours), tuple(args.coast_hours), args.periods, args.quiet)


if __name__ == "__main__":
    main()
