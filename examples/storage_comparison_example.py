"""Paired storage / no-storage run with a full flexibility assessment.

Run two identical networks over the same weather and demand profile, one with a
cold store and one without, then quantify what the store actually bought.

    python storage_comparison_example.py
    python storage_comparison_example.py --config ../configs/config_campus_five_buildings.yaml
    python storage_comparison_example.py --periods 96          # shorter run

Design values, pipe geometry and insulation, storage size and coupling,
weather, tariff, solver settings and output paths all come from the YAML
scenario file. Nothing in this script is specific to one scenario.

When the scenario's store is a stratified tank, the run also reports what a
scalar model cannot: the temperature the tank actually delivered, how far the
thermocline spread, and how much of the stored cooling came out below the
distribution setpoint. The campus scenario carries one:

    python storage_comparison_example.py --config ../configs/config_campus_five_buildings.yaml
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from discoolpy import (
    assess_flexibility,
    build_system,
    load_yaml_config,
    make_storage_comparison_plot,
    make_weather_and_load_profiles,
    output_path,
    run_configured_case,
    write_storage_comparison_summary,
)

# Resolve relative to this file, not the caller's working directory, so the
# example runs the same from the repo root, from examples/ or from a notebook.
HERE = Path(__file__).resolve().parent
DEFAULT_CONFIG = HERE.parent / "configs" / "config_riyadh_heat_gains.yaml"


def report_stratification(config, results) -> None:
    """Print what the resolved tank did, when the scenario has one.

    A scalar store has an amount but no temperature, so none of this exists
    for one. These are the numbers that decide whether a tank of a given size
    can serve the district, rather than merely holding enough kWh on paper.
    """
    if "storage_cold_port_degC" not in results.columns:
        return

    storage = build_system(config).storage
    tank = storage.stratified
    setpoint = float((config.get("design") or {}).get("supply_temperature_degC", 7.0))
    layers = sorted(
        (c for c in results.columns if c.startswith("storage_tank_T")),
        key=lambda c: int(c.rsplit("T", 1)[1].split("_")[0]),
    )

    print()
    print("## Stratified tank")
    print()
    described = tank.describe()
    print(f"  {described['volume_m3']:.0f} m3 in {described['layers']} layers, "
          f"{described['height_m']:.1f} m tall, {described['capacity_kWh']:.0f} kWh "
          f"between {described['charged_temperature_degC']:.1f} and "
          f"{described['discharged_temperature_degC']:.1f} degC")

    # A tank charged from the plant supply can at best return water *at* the
    # setpoint, never below it, so the useful question is how much came back
    # within an acceptable band of it rather than under it.
    tolerance = 0.5
    discharging = results[results["storage_discharge_power_W"] > 1e3]
    if len(discharging):
        outlet = discharging["storage_tank_outlet_temperature_degC"].dropna()
        energy = discharging["storage_discharge_power_W"] * discharging["resolution_hours"]
        delivered_kWh = energy.sum() / 1e3
        within = outlet.index[outlet <= setpoint + tolerance]
        useful_kWh = energy.loc[within].sum() / 1e3
        weighted = float((outlet * energy.loc[outlet.index]).sum() / max(energy.loc[outlet.index].sum(), 1e-9))
        print()
        print(f"  delivered              : {delivered_kWh:8.1f} kWh over "
              f"{len(discharging)} snapshots")
        print(f"  within {tolerance:.1f} K of setpoint: {useful_kWh:8.1f} kWh "
              f"({useful_kWh / max(delivered_kWh, 1e-9):.1%} of it)")
        print(f"  outlet temperature     : {outlet.min():.2f} to {outlet.max():.2f} degC, "
              f"mean {weighted:.2f} (setpoint {setpoint:.1f})")

    print()
    print(f"  cold port            : {results['storage_cold_port_degC'].min():.2f} to "
          f"{results['storage_cold_port_degC'].max():.2f} degC")
    print(f"  thermocline          : {results['storage_thermocline_m'].min():.2f} to "
          f"{results['storage_thermocline_m'].max():.2f} m "
          f"of {described['height_m']:.1f} m")

    print()
    print("  Final profile, top layer first:")
    final = results.iloc[-1]
    for column in reversed(layers):
        index = int(column.rsplit("T", 1)[1].split("_")[0])
        height = (index + 0.5) * tank.layer_height_m
        temperature = float(final[column])
        share = (temperature - tank.charged_temperature_degC) / max(
            tank.discharged_temperature_degC - tank.charged_temperature_degC, 1e-9
        )
        bar = "#" * int(round(min(max(1.0 - share, 0.0), 1.0) * 30))
        print(f"    {height:5.1f} m  {temperature:6.2f} degC  |{bar:<30}|")
    print("    (bar length = how charged that layer is)")


def run_storage_comparison(
    config_path: str | Path = DEFAULT_CONFIG,
    periods: int | None = None,
    quiet: bool = False,
):
    """Run paired examples with and without cold storage, and assess the result."""
    config = load_yaml_config(config_path)
    if periods is not None:
        config.setdefault("profiles", {})["periods"] = int(periods)

    storage_cfg = config.get("storage") or {}
    if not storage_cfg.get("enabled"):
        raise SystemExit(
            f"{Path(config_path).name} has no storage section enabled, so there is nothing to "
            "compare. Use a scenario with 'storage: {enabled: true}'."
        )

    profile = make_weather_and_load_profiles(config)
    profile.to_csv(output_path(config, "profile_csv", "input_profile.csv"), index=False)

    no_storage = run_configured_case(config, profile, "without_storage", use_storage=False,
                                     progress=not quiet)
    no_storage.to_csv(output_path(config, "without_storage_csv", "without_storage_results.csv"),
                      index=False)

    with_storage = run_configured_case(config, profile, "with_storage", use_storage=True,
                                       progress=not quiet)
    with_storage.to_csv(output_path(config, "with_storage_csv", "with_storage_results.csv"),
                        index=False)

    write_storage_comparison_summary(config, no_storage, with_storage)
    make_storage_comparison_plot(config, no_storage, with_storage)

    if not quiet:
        report_stratification(config, with_storage)

    economics = config.get("economics") or {}
    report = assess_flexibility(
        no_storage,
        with_storage,
        reference_name="without_storage",
        flexible_name="with_storage",
        tariff=economics.get("tariff"),
        carbon_intensity=economics.get("carbon_intensity_kg_kWh"),
    )
    if not quiet:
        print()
        print(report.to_markdown())
        print()
        print(f"Summary  : {output_path(config, 'summary_md', 'flexibility_summary.md')}")
        print(f"Plot     : {output_path(config, 'plot_png', 'storage_comparison_results.png')}")
    return no_storage, with_storage, report


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the YAML-driven district-cooling storage comparison example."
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG),
                        help="Path to the YAML scenario file.")
    parser.add_argument("--periods", type=int, default=None,
                        help="Override profiles.periods for a shorter run.")
    parser.add_argument("--quiet", action="store_true", help="Suppress progress output.")
    args = parser.parse_args()
    run_storage_comparison(args.config, periods=args.periods, quiet=args.quiet)


if __name__ == "__main__":
    main()
