"""A district cooling network on a street grid that forks.

Runs ``configs/config_branching_grid.yaml``: a trunk from the central plant that
splits into a north spur and an east spur, the east spur splitting again into a
dead-end leg and a satellite plant with its own chiller, cooling tower and cold
store. Six buildings, four branches, three kinds of terminal.

The run answers three questions the single-street model could not:

1. **Does a fork solve at all, and does its energy balance close?**
   A fork puts two paths in parallel between one pair of hydraulic nodes, and a
   satellite puts a second evaporator in the chilled-water loop. Both change the
   degrees of freedom, so the closure residual is worth printing.
2. **What does a distributed plant buy?** The same scenario is run twice, once
   with the satellite and once with a plain bypass in its place, so the load it
   lifts off the central machine is a measured difference rather than an
   assertion.
3. **Where does the heat get in?** Gain is reported branch by branch, which is
   the only way to see that a 280 m exposed spur can matter more than a 520 m
   buried main.

    python branching_network_example.py
    python branching_network_example.py --periods 96
"""

from __future__ import annotations

import argparse
import warnings
from pathlib import Path

import matplotlib.pyplot as plt
import pandas as pd

from discoolpy import (
    SatellitePlant,
    build_system,
    ensure_output_dir,
    load_yaml_config,
    make_weather_and_load_profiles,
    output_path,
    plot_network,
    run_configured_case,
)

warnings.filterwarnings("ignore", category=FutureWarning)

HERE = Path(__file__).resolve().parent
CONFIG = HERE.parent / "configs" / "config_branching_grid.yaml"


def without_the_satellite(config: dict) -> dict:
    """The same network with a plain bypass where the satellite plant was.

    Same slipstream, same hydraulics, only without the chiller and its store.
    That makes the difference between the two runs attributable to the plant and
    not to a change in flow distribution.
    """
    scenario = load_yaml_config(CONFIG)
    scenario["outputs"] = dict(config["outputs"])
    scenario["profiles"] = dict(config["profiles"])
    east = scenario["branch"]["terminals"][1]
    plant = east["terminals"][1]
    east["terminals"][1] = {
        "type": "bypass",
        "label": "airport dead end",
        "m_kg_s": plant["m_kg_s"],
        "dp": None,
        "heading_deg": plant.get("heading_deg", 0),
    }
    return scenario


def describe(system) -> None:
    print("\n-- Network --")
    for branch in system.branches:
        parent = "central plant" if branch is system.branch else "its parent"
        print(f"  [{branch.label}] fed from {parent}, {branch.n_buildings} building(s)")
        for order, building in enumerate(branch.buildings, start=1):
            print(f"      {order}. {building.label:<20} {building.Q_design/1e3:6.1f} kW")
        for terminal in branch.terminals:
            kind = type(terminal).__name__
            detail = ""
            if isinstance(terminal, SatellitePlant):
                detail = f" - {terminal.design_Q_evap_W/1e3:.0f} kW, dispatch={terminal.dispatch}"
                if terminal.storage is not None:
                    detail += f", store {terminal.storage.capacity_kWh:.0f} kWh"
            print(f"      -> {kind}: {terminal.label}{detail}")


def branch_heat_gains(system) -> pd.DataFrame:
    """Solved pipe gain per branch, as a share of the buildings it serves."""
    per_pipe = system.branch.heat_gain_report()["per_pipe_W"]
    rows = []
    for branch in system.branches:
        prefix = "" if branch is system.branch else f"{branch.label}/"
        keys = [f"{prefix}{key}" for key, _ in branch.pipe_items()]
        gain = sum(per_pipe.get(key, 0.0) for key in keys)
        length = sum((branch.pipe_lengths or {}).get(key, 0.0) for key, _ in branch.pipe_items())
        served = sum(b.component.Q.val for b in branch.buildings)
        rows.append({
            "branch": branch.label,
            "route_length_m": length,
            "buildings_kW": served / 1e3,
            "pipe_gain_kW": gain / 1e3,
            "gain_W_per_m": gain / length if length else float("nan"),
        })
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--periods", type=int, default=48,
                        help="Number of snapshots to simulate (default 48 = 1 day at 30 min).")
    parser.add_argument("--no-plots", action="store_true")
    args = parser.parse_args()

    config = load_yaml_config(CONFIG)
    config.setdefault("profiles", {})["periods"] = args.periods
    output_dir = ensure_output_dir(config)

    # ---- 1. the layout, before anything is solved ------------------------
    system = build_system(config)
    describe(system)
    for note in system.notes:
        print(f"\n[note] {note}")

    layout_png = output_path(config, "layout_png", "network_layout.png")
    if not args.no_plots:
        ax = plot_network(system, annotate_pipes=True, save_path=str(layout_png))
        plt.close(ax.figure)
        print(f"\nLayout written to {layout_png}")

    # ---- 2. design point --------------------------------------------------
    system.network.solve(mode="design", max_iter=400)
    if not system.network.converged:
        raise SystemExit("Design solve did not converge.")

    satellites = system.branch.satellite_report()
    gains = system.branch.heat_gain_report()
    buildings = sum(b.component.Q.val for b in system.buildings)
    pump = system.branch.pump.P.val
    print("\n-- Design point --")
    print(f"  buildings                : {buildings/1e3:8.1f} kW")
    print(f"  pipe gain                : {gains['total_heat_gain_W']/1e3:8.1f} kW")
    print(f"  pump heat                : {pump/1e3:8.1f} kW")
    print(f"  satellite plants deliver : {-satellites['total_delivered_Q_W']/1e3:8.1f} kW")
    print(f"  central plant duty       : {system.chiller.solved_Q_evap_W/1e3:8.1f} kW")
    residual = (
        system.chiller.solved_Q_evap_W + satellites["total_Q_evap_W"]
        - satellites["total_storage_Q_W"] - buildings
        - gains["total_heat_gain_W"] - pump
    )
    print(f"  closure residual         : {residual:8.4f} W")
    print(f"\n  {system.branch.pressure_feasibility()['message']}")

    print("\n-- Heat gain by branch --")
    print(branch_heat_gains(system).round(3).to_string(index=False))

    # ---- 3. time series, with and without the satellite -------------------
    profile = make_weather_and_load_profiles(config)
    profile.to_csv(output_path(config, "profile_csv", "input_profile.csv"), index=False)

    print(f"\n-- Time series: {args.periods} snapshots --")
    with_plant = run_configured_case(config, profile, "with_satellite", use_storage=False)
    without = run_configured_case(
        without_the_satellite(config), profile, "no_satellite", use_storage=False
    )
    with_plant.to_csv(output_dir / "with_satellite_results.csv", index=False)
    without.to_csv(output_dir / "no_satellite_results.csv", index=False)

    hours = float(with_plant["resolution_hours"].iloc[0])
    print("\n-- What the satellite plant buys --")
    rows = [
        ("central plant peak duty", "kW",
         without["chiller_Q_evap_W"].max() / 1e3, with_plant["chiller_Q_evap_W"].max() / 1e3),
        ("central compressor peak", "kW",
         without["compressor_power_W"].max() / 1e3, with_plant["compressor_power_W"].max() / 1e3),
        ("fleet compressor peak", "kW",
         without["total_compressor_power_W"].max() / 1e3,
         with_plant["total_compressor_power_W"].max() / 1e3),
        ("fleet compressor energy", "kWh",
         without["total_compressor_power_W"].sum() * hours / 1e3,
         with_plant["total_compressor_power_W"].sum() * hours / 1e3),
        ("mean fleet COP", "-",
         without["fleet_cop"].mean(), with_plant["fleet_cop"].mean()),
    ]
    print(f"  {'':32s} {'without':>12s} {'with':>12s} {'change':>10s}")
    for name, unit, a, b in rows:
        change = (b - a) / a * 100 if a else float("nan")
        print(f"  {name + ' (' + unit + ')':32s} {a:12.2f} {b:12.2f} {change:+9.1f} %")

    print(
        "\n  A satellite plant moves duty, it does not create it. Whether that is worth "
        "\n  doing depends on the pumping and pipe-gain it avoids on the index run, and on "
        "\n  whether its own condenser sits somewhere cooler than the central tower."
    )

    # ---- 4. plots ---------------------------------------------------------
    if args.no_plots:
        return
    fig, axes = plt.subplots(3, 1, figsize=(11, 10), sharex=True)
    index = pd.to_datetime(with_plant["timestamp"])

    axes[0].plot(index, without["chiller_Q_evap_W"] / 1e3, label="central plant, no satellite")
    axes[0].plot(index, with_plant["chiller_Q_evap_W"] / 1e3, label="central plant, with satellite")
    axes[0].plot(index, with_plant["actual_building_total_Q_W"] / 1e3, "--",
                 color="0.4", label="building demand")
    axes[0].set_ylabel("cooling duty (kW)")
    axes[0].legend(fontsize=8)
    axes[0].set_title("What the central machine has to make")

    axes[1].plot(index, with_plant["satellite_Q_evap_W"] / 1e3, label="satellite produces")
    axes[1].plot(index, with_plant["satellite_delivered_Q_W"] / 1e3,
                 label="satellite delivers to the district")
    axes[1].set_ylabel("duty (kW)")
    axes[1].legend(fontsize=8)
    axes[1].set_title("The store lets the satellite run flat while its output follows demand")

    soc = [c for c in with_plant.columns if c.endswith("_storage_soc")]
    if soc:
        axes[2].plot(index, with_plant[soc[0]] * 100, color="#2d6ca2")
        axes[2].set_ylabel("satellite store SOC (%)")
    axes[2].set_title("Satellite store state of charge")
    axes[2].set_xlabel("time")
    for ax in axes:
        ax.grid(True, linestyle=":", alpha=0.6)
    fig.tight_layout()
    target = output_path(config, "plot_png", "branching_results.png")
    fig.savefig(target, dpi=150)
    plt.close(fig)
    print(f"\nResults plot written to {target}")


if __name__ == "__main__":
    main()
