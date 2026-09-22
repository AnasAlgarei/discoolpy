"""DisCoolPy: modular district-cooling modelling on TESPy.

Branching street networks, heat gains in pipes, buildings and stores, thermal
storage and flexibility assessment, a plan-view layout plotter, and printed and
plotted results for every component and for the system as a whole.

The short way in is a scenario file and two calls::

    from discoolpy import check_scenario, run_scenario

    check_scenario("my_scenario.yaml")        # design point, seconds
    result = run_scenario("my_scenario.yaml") # the time series

or the same thing from a shell, with ``discoolpy new`` to start from a
template. Everything underneath stays available: :func:`build_system` hands
back the assembled TESPy network, and the component wrappers can be driven
directly by a study that needs its own control logic between snapshots.
"""

__version__ = "1.0.0"

from .errors import DisCoolPyError, ScenarioError
from .chiller import Chiller
from .building import Building, ThermalMass, ThermalMassState
from .branch import Branch, EndBypass, SatellitePlant, SubBranch, Terminal
from .cooling_tower import CoolingTower
from .time_snapshot import SnapshotSchedule, TimeSnapshot
from .cold_storage import ColdStorage, StorageDispatchResult
from .stratified import StratifiedTank, TankStepResult
from .thermal import (
    EnvelopeThermal,
    PipeThermal,
    StorageThermal,
    buried_pipe_UA_per_m,
    lmtd_to_ambient,
    soil_temperature_degC,
    surface_pipe_UA_per_m,
    tank_UA_W_K,
)
from .hydraulics import (
    BranchTopology,
    TerminalTopology,
    analyse_network_topology,
    analyse_pressure_topology,
    resolve_network_pressure_specification,
    resolve_pressure_specification,
    suggest_pressure_specification,
    validate_network_topology,
    validate_pressure_topology,
    validate_thermal_degrees_of_freedom,
)
from .flexibility import (
    FlexibilityReport,
    HeatGainSummary,
    LoadShapeMetrics,
    assess_flexibility,
    flexibility_envelope,
    heat_gain_summary,
    load_shape_metrics,
)
from .config_schema import (
    AUTO_PIPE_PRESSURE_RATIO,
    SCENARIO_KEYS,
    BranchSpec,
    TerminalSpec,
    autofill_branch_pressure,
    autofill_network_energy,
    branch_pipe_thermal,
    building_design_load_W,
    validate_scenario,
    make_branch_specs,
    make_buildings,
    make_network_pipe_thermal,
    make_pipe_thermal,
    make_storage,
    make_stratified_tank,
    plant_control_mode,
)
from .reporting import (
    ComponentReport,
    SystemReport,
    compare_pipe_heat_models,
    plot_all,
    plot_balance,
    plot_buildings,
    plot_chiller,
    plot_cooling_tower,
    plot_pipes,
    plot_storage,
    plot_system,
    print_report,
    report_balance,
    report_buildings,
    report_chiller,
    report_cooling_tower,
    report_pipes,
    report_satellites,
    report_storage,
    report_system,
)
from .layout import (
    LayoutEdge,
    LayoutNode,
    NODE_MARKERS,
    NetworkLayout,
    compute_layout,
    plot_network,
)
from .utils import (
    DESIGN_DEFAULTS,
    DistrictCoolingSystem,
    OCCUPANCY_ARCHETYPES,
    default_pump_power_W,
    branch_pipe_attrs,
    branch_pipe_headings,
    branch_pipe_lengths,
    build_standard_branch_system,
    build_system,
    check_stratified_temperatures,
    compute_building_mass_flows,
    dispatch_satellite_plants,
    design_evaporator_load,
    ensure_output_dir,
    load_yaml_config,
    make_chiller,
    make_cooling_tower,
    make_network_pipe_attrs,
    make_pipe_attrs,
    make_riyadh_weather_and_load_profiles,
    make_schedule_from_profile,
    make_storage_comparison_plot,
    make_storage_from_config,
    make_weather_and_load_profiles,
    network_design_mass_flows,
    network_topology,
    output_path,
    run_configured_case,
    total_design_mass_flow,
    write_storage_comparison_summary,
)
from .scenario import (
    CheckResult,
    ScenarioResult,
    check_scenario,
    load_scenario,
    run_scenario,
)

__all__ = [
    "__version__",
    # errors
    "DisCoolPyError",
    "ScenarioError",
    # the short way in
    "run_scenario",
    "check_scenario",
    "load_scenario",
    "ScenarioResult",
    "CheckResult",
    # components
    "Chiller",
    "Building",
    "Branch",
    "Terminal",
    "EndBypass",
    "SatellitePlant",
    "SubBranch",
    "CoolingTower",
    "TimeSnapshot",
    "SnapshotSchedule",
    "ColdStorage",
    "StorageDispatchResult",
    "StratifiedTank",
    "TankStepResult",
    "ThermalMass",
    "ThermalMassState",
    # heat-gain physics
    "EnvelopeThermal",
    "PipeThermal",
    "StorageThermal",
    "buried_pipe_UA_per_m",
    "surface_pipe_UA_per_m",
    "tank_UA_W_K",
    "soil_temperature_degC",
    "lmtd_to_ambient",
    # structural checks
    "BranchTopology",
    "TerminalTopology",
    "analyse_network_topology",
    "analyse_pressure_topology",
    "validate_network_topology",
    "validate_pressure_topology",
    "resolve_network_pressure_specification",
    "resolve_pressure_specification",
    "suggest_pressure_specification",
    "validate_thermal_degrees_of_freedom",
    # layout
    "LayoutNode",
    "LayoutEdge",
    "NetworkLayout",
    "NODE_MARKERS",
    "compute_layout",
    "plot_network",
    # reporting: printed and plotted results, per component and whole system
    "ComponentReport",
    "SystemReport",
    "report_system",
    "print_report",
    "report_balance",
    "report_pipes",
    "report_buildings",
    "report_chiller",
    "report_cooling_tower",
    "report_storage",
    "report_satellites",
    "plot_system",
    "plot_balance",
    "plot_pipes",
    "plot_buildings",
    "plot_chiller",
    "plot_cooling_tower",
    "plot_storage",
    "plot_all",
    "compare_pipe_heat_models",
    # flexibility
    "FlexibilityReport",
    "HeatGainSummary",
    "LoadShapeMetrics",
    "assess_flexibility",
    "flexibility_envelope",
    "heat_gain_summary",
    "load_shape_metrics",
    # configuration
    "DESIGN_DEFAULTS",
    "AUTO_PIPE_PRESSURE_RATIO",
    "autofill_branch_pressure",
    "autofill_network_energy",
    "default_pump_power_W",
    "BranchSpec",
    "TerminalSpec",
    "branch_pipe_thermal",
    "building_design_load_W",
    "validate_scenario",
    "SCENARIO_KEYS",
    "make_branch_specs",
    "make_buildings",
    "make_network_pipe_thermal",
    "make_pipe_thermal",
    "make_storage",
    "make_stratified_tank",
    "plant_control_mode",
    # orchestration
    "DistrictCoolingSystem",
    "OCCUPANCY_ARCHETYPES",
    "build_system",
    "build_standard_branch_system",
    "load_yaml_config",
    "ensure_output_dir",
    "output_path",
    "branch_pipe_attrs",
    "branch_pipe_headings",
    "branch_pipe_lengths",
    "make_chiller",
    "make_cooling_tower",
    "make_network_pipe_attrs",
    "make_pipe_attrs",
    "network_design_mass_flows",
    "network_topology",
    "dispatch_satellite_plants",
    "compute_building_mass_flows",
    "total_design_mass_flow",
    "design_evaporator_load",
    "make_storage_from_config",
    "make_weather_and_load_profiles",
    "make_riyadh_weather_and_load_profiles",
    "make_schedule_from_profile",
    "run_configured_case",
    "write_storage_comparison_summary",
    "make_storage_comparison_plot",
]
