"""Reporting: the printed report, the figures, and the heat-model cross-check.

The plotting tests assert that a figure comes back with the panels it promised
and that nothing raises, not that it looks right. What they do check carefully
is the degenerate cases, because the whole point of this module is that it can
be pointed at an arbitrary DisCoolPy network: a run with no store, no envelope,
no satellite and no time series at all still has to produce a figure and say
why the empty panels are empty.
"""

from __future__ import annotations

import math

import matplotlib
import pandas as pd
import pytest

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from discoolpy import reporting  # noqa: E402
from discoolpy.reporting import (  # noqa: E402
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
    report_balance,
    report_buildings,
    report_chiller,
    report_pipes,
    report_satellites,
    report_storage,
    report_system,
    resolve_source,
)


@pytest.fixture(autouse=True)
def _close_figures():
    yield
    plt.close("all")


def make_frame(n: int = 24, storage: bool = True, envelope: bool = True) -> pd.DataFrame:
    """A results frame with the columns ``collect_result`` writes."""
    rows = []
    for i in range(n):
        hour = i % 24
        ambient = 30.0 + 12.0 * math.sin(math.pi * (hour - 6) / 12.0) ** 2
        demand = 400e3 + 200e3 * math.sin(math.pi * hour / 24.0)
        row = {
            "case": "base",
            "snapshot_index": i,
            "timestamp": pd.Timestamp("2026-07-01") + pd.Timedelta(hours=i),
            "resolution_hours": 1.0,
            "ambient_temperature_degC": ambient,
            "ground_temperature_degC": 29.4,
            "solar_irradiance_W_m2": max(0.0, 900.0 * math.sin(math.pi * hour / 24.0)),
            "actual_building_total_Q_W": demand,
            "chiller_Q_evap_W": demand * 1.08,
            "total_cooling_produced_W": demand * 1.08,
            "compressor_power_W": demand / 3.6,
            "total_compressor_power_W": demand / 3.6,
            "cop": 3.6,
            "fleet_cop": 3.6,
            "heat_rejection_W": demand * 1.08 + demand / 3.6,
            "pipe_heat_gain_W": 20e3,
            "pipe_supply_heat_gain_W": 11e3,
            "pipe_return_heat_gain_W": 9e3,
            "pipe_network_UA_W_K": 1046.0,
            "pipe_supply_1_Q_W": 7e3,
            "pipe_supply_2_Q_W": 4e3,
            "pipe_return_1_Q_W": 5e3,
            "pipe_return_2_Q_W": 4e3,
            "cw_in_T_degC": 33.0,
            "cw_out_T_degC": 41.0,
            "cw_m_kg_s": 30.0,
            "chw_plant_supply_T_degC": 7.0,
            "chw_supply_T_degC": 7.0,
            "chw_return_T_degC": 12.0,
            "chw_delta_T_K": 5.0,
            "chw_total_m_kg_s": 34.0,
            "pump_power_W": 12.8e3,
            "chw_energy_residual_W": 1e-9,
            "building_1_effective_Q_W": demand * 0.6,
            "building_2_effective_Q_W": demand * 0.4,
        }
        if envelope:
            row.update({
                "building_1_conduction_W": 40e3,
                "building_1_solar_W": 30e3,
                "building_1_infiltration_W": 10e3,
                "building_1_internal_W": demand * 0.6 - 80e3,
                "building_1_T_indoor_degC": 24.0,
                "building_2_conduction_W": 25e3,
                "building_2_solar_W": 20e3,
                "building_2_infiltration_W": 6e3,
                "building_2_internal_W": demand * 0.4 - 51e3,
                "building_2_T_indoor_degC": 23.5,
            })
        if storage:
            charging = hour < 6
            row.update({
                "storage_mode": "charge" if charging else "discharge",
                "storage_charge_power_W": 200e3 if charging else 0.0,
                "storage_discharge_power_W": 0.0 if charging else 120e3,
                "storage_soc_after": 0.5 + 0.4 * math.sin(math.pi * hour / 24.0),
                "storage_ambient_heat_gain_W": 82.2 * ambient,
                "storage_chiller_load_offset_W": 0.0,
                "storage_curtailed_request_W": 0.0,
            })
        else:
            row.update({"storage_mode": "none", "storage_charge_power_W": 0.0,
                        "storage_discharge_power_W": 0.0,
                        "storage_ambient_heat_gain_W": 0.0})
        rows.append(row)
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Source resolution
# ---------------------------------------------------------------------------

class TestResolveSource:
    def test_takes_a_frame(self):
        src = resolve_source(make_frame())
        assert src.has_results
        assert src.dt_h == 1.0

    def test_takes_a_csv_path(self, tmp_path):
        path = tmp_path / "results.csv"
        make_frame().to_csv(path, index=False)
        assert resolve_source(path).has_results

    def test_is_idempotent(self):
        src = resolve_source(make_frame())
        assert resolve_source(src) is src

    def test_idempotent_call_can_still_take_new_results(self):
        src = resolve_source(make_frame(n=8))
        again = resolve_source(src, make_frame(n=16))
        assert len(again.results) == 16
        assert len(src.results) == 8

    def test_energy_uses_the_snapshot_resolution(self):
        frame = make_frame(n=4)
        frame["resolution_hours"] = 0.5
        src = resolve_source(frame)
        # 4 snapshots x 20 kW x 0.5 h
        assert src.energy_kWh("pipe_heat_gain_W") == pytest.approx(40.0)

    def test_missing_column_is_none_not_an_error(self):
        assert resolve_source(make_frame()).col("no_such_column") is None

    def test_rejects_something_it_cannot_read(self):
        with pytest.raises(TypeError, match="Cannot read results"):
            resolve_source(object(), results=42)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

class TestComponentReport:
    def test_skips_missing_and_nan_values(self):
        report = ComponentReport("x")
        report.add("present", 1.0).add("absent", None).add("nan", float("nan"))
        assert [label for label, _ in report.rows] == ["present"]

    def test_text_is_empty_when_there_is_nothing_to_say(self):
        assert ComponentReport("x").to_text() == ""


class TestReports:
    def test_balance_reports_the_parasitic_shares(self):
        report = report_balance(make_frame())
        assert report.data["produced_share_pct"] == pytest.approx(108.0, abs=0.01)
        assert report.data["pipe_gain_kWh"] == pytest.approx(480.0)

    def test_balance_warns_when_the_loop_does_not_close(self):
        frame = make_frame()
        frame["chw_energy_residual_W"] = 250.0
        assert any("does not close" in note for note in report_balance(frame).notes)

    def test_balance_is_quiet_when_the_loop_closes(self):
        notes = report_balance(make_frame()).notes
        assert not any("does not close" in note for note in notes)

    def test_pipes_reports_gain_share(self):
        frame = make_frame()
        report = report_pipes(frame)
        demand_kWh = frame["actual_building_total_Q_W"].sum() / 1e3
        assert report.data["gain_kWh"] == pytest.approx(480.0)
        assert report.data["gain_share_pct"] == pytest.approx(
            100 * 480.0 / demand_kWh)
        assert report.data["peak_gain_kW"] == pytest.approx(20.0)
        assert report.data["supply_gain_kWh"] + report.data["return_gain_kWh"] == (
            pytest.approx(report.data["gain_kWh"]))

    def test_pipes_notices_a_flat_buried_gain(self):
        assert any("damps" in note for note in report_pipes(make_frame()).notes)

    def test_buildings_reports_each_building(self):
        data = report_buildings(make_frame()).data
        assert set(data["per_building_kWh"]) == {"building_1", "building_2"}

    def test_buildings_omits_the_envelope_split_without_an_envelope(self):
        report = report_buildings(make_frame(envelope=False))
        assert not any(k.startswith("envelope_") for k in report.data)

    def test_buildings_says_so_when_the_split_is_all_internal(self):
        """Envelope columns present but weather-driven paths empty."""
        frame = make_frame()
        for label in ("building_1", "building_2"):
            for part in ("conduction_W", "solar_W", "infiltration_W"):
                frame[f"{label}_{part}"] = 0.0
        report = report_buildings(frame)
        assert not any(k.startswith("envelope_") for k in report.data)
        assert any("no weather sensitivity" in note for note in report.notes)

    def test_chiller_reports_efficiency(self):
        assert report_chiller(make_frame()).data["cop_mean"] == pytest.approx(3.6)

    def test_storage_reports_the_hot_cool_gain_ratio(self):
        data = report_storage(make_frame()).data
        assert data["gain_hot_cool_ratio"] > 1.0

    def test_storage_is_silent_without_a_store(self):
        report = report_storage(make_frame(storage=False))
        assert "charged_kWh" not in report.data
        assert any("No central store" in note for note in report.notes)

    def test_satellites_says_so_when_there_are_none(self):
        report = report_satellites(make_frame())
        assert any("no distributed plants" in note for note in report.notes)

    def test_system_report_renders(self):
        report = report_system(make_frame())
        assert isinstance(report, SystemReport)
        text = report.to_text()
        assert "Plant energy balance" in text
        assert "Central chiller" in text
        assert set(report.to_dict()) == {s.title for s in report.sections}


# ---------------------------------------------------------------------------
# Figures
# ---------------------------------------------------------------------------

EVERY_FIGURE = [plot_system, plot_balance, plot_pipes, plot_buildings,
                plot_chiller, plot_cooling_tower, plot_storage]


class TestFigures:
    @pytest.mark.parametrize("function", EVERY_FIGURE)
    def test_every_figure_draws_from_a_frame(self, function):
        figure = function(make_frame())
        assert figure.axes

    @pytest.mark.parametrize("function", EVERY_FIGURE)
    def test_every_figure_survives_an_empty_frame(self, function):
        """A frame with nothing in it is the degenerate case, not an error."""
        figure = function(pd.DataFrame())
        assert figure.axes

    @pytest.mark.parametrize("function", EVERY_FIGURE)
    def test_every_figure_survives_a_bare_scenario(self, function):
        """No store, no envelope, no satellite: the panels say so and stay drawn."""
        figure = function(make_frame(storage=False, envelope=False))
        assert figure.axes

    def test_figures_are_written_where_asked(self, tmp_path):
        target = tmp_path / "nested" / "pipes.png"
        plot_pipes(make_frame(), save_path=str(target))
        assert target.exists() and target.stat().st_size > 0

    def test_a_title_can_be_overridden(self):
        figure = plot_system(make_frame(), title="my district")
        assert figure._suptitle.get_text() == "my district"

    def test_plot_all_writes_a_figure_per_component(self, tmp_path):
        paths = plot_all(make_frame(), tmp_path, prefix="case")
        assert set(paths) == set(reporting.FIGURES) | {"report"}
        assert all(path.exists() for path in paths.values())
        assert "Plant energy balance" in paths["report"].read_text(encoding="utf-8")

    def test_plot_all_skips_the_layout_without_a_system(self, tmp_path):
        assert "layout" not in plot_all(make_frame(), tmp_path)

    def test_an_adiabatic_network_is_named_rather_than_plotted(self):
        """Solver dust around zero looks like a signal on its own axis."""
        frame = make_frame()
        for column in ("pipe_heat_gain_W", "pipe_supply_heat_gain_W",
                       "pipe_return_heat_gain_W", "pipe_supply_1_Q_W",
                       "pipe_supply_2_Q_W", "pipe_return_1_Q_W", "pipe_return_2_Q_W"):
            frame[column] = 1e-12
        figure = plot_pipes(frame)
        text = " ".join(t.get_text() for axis in figure.axes for t in axis.texts)
        assert "adiabatic" in text
        # Nothing was drawn on the three time-series panels.
        assert all(not axis.lines and not axis.patches
                   for axis in figure.axes[1:])


class TestPalette:
    def test_hues_are_assigned_in_the_published_order(self):
        assert reporting.PALETTE[0] == "#2a78d6"
        assert len(set(reporting.PALETTE)) == len(reporting.PALETTE)

    def test_ambient_and_soil_are_not_a_red_green_pair(self):
        """The one panel that draws both must not rely on a red/green contrast."""
        assert reporting.C["ground"] != "#008300"
        assert reporting.C["ground"] != reporting.C["ambient"]


# ---------------------------------------------------------------------------
# Heat-model cross-check
# ---------------------------------------------------------------------------

class TestCompareHeatModels:
    """These solve real TESPy networks, so they are slower than the rest."""

    def test_the_two_formulations_agree_on_ordinary_geometry(self):
        result = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05,
        )
        assert result["native_available"]
        # They differ only by TESPy counting the steel wall as insulation.
        assert 1.0 < result["ratio"] < 1.25

    def test_removing_the_steel_wall_closes_the_gap(self):
        """The discrepancy is the wall, and nothing else."""
        result = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05,
            pipe_wall_thickness_m=1e-6,
        )
        assert result["ratio"] == pytest.approx(1.0, abs=0.03)

    def test_the_twin_trench_correction_lowers_the_gain(self):
        single = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05)
        twin = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05,
            twin_spacing_m=0.7)
        assert twin["closed_form_Q_W"] < single["closed_form_Q_W"]

    @pytest.mark.parametrize("ground", ["sand", "clay", "rock", "wet sand"])
    def test_native_is_unavailable_outside_tespys_four_media(self, ground):
        result = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05,
            ground=ground)
        assert not result["native_available"]
        assert "buried group knows only" in result["reason"]
        # The closed form still answers, which is the point.
        assert result["closed_form_UA_per_m_W_mK"] > 0

    def test_native_is_unavailable_for_a_bare_pipe(self):
        result = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.0)
        assert not result["native_available"]
        assert result["closed_form_UA_per_m_W_mK"] > 1.0

    def test_native_is_unavailable_in_still_air(self):
        result = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05,
            placement="surface", wind_velocity_m_s=0.0,
            ambient_temperature_degC=43.0)
        assert not result["native_available"]
        assert "still air" in result["reason"]

    def test_the_surface_models_agree_in_moving_air(self):
        result = compare_pipe_heat_models(
            inner_diameter_m=0.2, length_m=800.0, insulation_thickness_m=0.05,
            placement="surface", wind_velocity_m_s=2.0,
            ambient_temperature_degC=43.0, pipe_wall_thickness_m=1e-6)
        assert result["native_available"]
        assert result["ratio"] == pytest.approx(1.0, abs=0.06)
