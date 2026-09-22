"""Flexibility metrics computed from result frames."""

import numpy as np
import pandas as pd
import pytest

from discoolpy.building import Building, ThermalMass
from discoolpy.cold_storage import ColdStorage
from discoolpy.flexibility import (
    assess_flexibility,
    flexibility_envelope,
    heat_gain_summary,
    load_shape_metrics,
)
from discoolpy.thermal import EnvelopeThermal, StorageThermal


def make_frame(power_W, *, charge=None, discharge=None, cop=4.0, pipe_gain=0.0,
               storage_gain=0.0, demand=None, hours=1.0):
    n = len(power_W)
    stamps = pd.date_range("2026-07-01", periods=n, freq=f"{int(hours * 60)}min")
    return pd.DataFrame({
        "timestamp": [s.isoformat() for s in stamps],
        "resolution_hours": [hours] * n,
        "compressor_power_W": power_W,
        "cop": [cop] * n if np.isscalar(cop) else cop,
        "chiller_Q_evap_W": [p * (cop if np.isscalar(cop) else 4.0) for p in power_W],
        "actual_building_total_Q_W": demand if demand is not None else [500_000.0] * n,
        "pipe_heat_gain_W": [pipe_gain] * n,
        "pump_power_W": [0.0] * n,
        "storage_charge_power_W": charge if charge is not None else [0.0] * n,
        "storage_discharge_power_W": discharge if discharge is not None else [0.0] * n,
        "storage_ambient_heat_gain_W": [storage_gain] * n,
        "storage_fractional_loss_kWh": [0.0] * n,
    })


class TestLoadShape:
    def test_basic_statistics(self):
        m = load_shape_metrics([100_000, 200_000, 300_000, 200_000], dt_h=1.0)
        assert m.peak_kW == pytest.approx(300.0)
        assert m.mean_kW == pytest.approx(200.0)
        assert m.min_kW == pytest.approx(100.0)
        assert m.energy_kWh == pytest.approx(800.0)
        assert m.load_factor == pytest.approx(2 / 3)
        assert m.peak_to_mean == pytest.approx(1.5)

    def test_flat_profile_has_unit_load_factor(self):
        m = load_shape_metrics([250_000] * 10, dt_h=1.0)
        assert m.load_factor == pytest.approx(1.0)
        assert m.max_ramp_up_kW_per_h == pytest.approx(0.0)

    def test_ramp_rates_scale_with_resolution(self):
        hourly = load_shape_metrics([0, 100_000], dt_h=1.0)
        half = load_shape_metrics([0, 100_000], dt_h=0.5)
        assert half.max_ramp_up_kW_per_h == pytest.approx(2 * hourly.max_ramp_up_kW_per_h)

    def test_empty_input_is_rejected(self):
        with pytest.raises(ValueError):
            load_shape_metrics([], dt_h=1.0)


class TestHeatGainSummary:
    def test_shares_are_relative_to_building_demand(self):
        frame = make_frame([100_000] * 24, pipe_gain=25_000, storage_gain=3_000,
                           demand=[500_000] * 24)
        summary = heat_gain_summary(frame)
        assert summary.building_demand_kWh == pytest.approx(12_000.0)
        assert summary.pipe_gain_kWh == pytest.approx(600.0)
        assert summary.pipe_gain_percent_of_demand == pytest.approx(5.0)
        assert summary.storage_gain_percent_of_demand == pytest.approx(0.6)

    def test_works_on_a_frame_with_no_heat_gain_columns(self):
        # So the same function can summarise a pre-upgrade adiabatic run.
        frame = make_frame([100_000] * 4).drop(
            columns=["pipe_heat_gain_W", "storage_ambient_heat_gain_W"]
        )
        summary = heat_gain_summary(frame)
        assert summary.pipe_gain_kWh == 0.0
        assert summary.storage_gain_kWh == 0.0

    def test_peak_gains_are_reported(self):
        frame = make_frame([100_000] * 4, pipe_gain=30_000)
        assert heat_gain_summary(frame).peak_pipe_gain_kW == pytest.approx(30.0)


class TestAssessFlexibility:
    def test_peak_reduction_is_measured_correctly(self):
        ref = make_frame([100_000, 400_000, 100_000, 100_000])
        flex = make_frame([200_000, 250_000, 150_000, 150_000])
        report = assess_flexibility(ref, flex)
        assert report.peak_reduction_kW == pytest.approx(150.0)
        assert report.peak_reduction_percent == pytest.approx(37.5)

    def test_energy_penalty_is_signed_the_right_way(self):
        ref = make_frame([100_000] * 4)
        flex = make_frame([110_000] * 4)
        report = assess_flexibility(ref, flex)
        assert report.energy_penalty_kWh == pytest.approx(40.0)
        assert report.energy_penalty_percent == pytest.approx(10.0)

    def test_load_factor_improves_when_the_peak_is_shaved(self):
        ref = make_frame([100_000, 400_000, 100_000, 100_000])
        flex = make_frame([175_000, 175_000, 175_000, 175_000])
        report = assess_flexibility(ref, flex)
        assert report.flexible.load_factor > report.reference.load_factor

    def test_thermal_round_trip_is_discharged_over_charged(self):
        charge = [200_000, 200_000, 0, 0]
        discharge = [0, 0, 180_000, 0]
        ref = make_frame([100_000] * 4)
        flex = make_frame([150_000, 150_000, 50_000, 100_000],
                          charge=charge, discharge=discharge)
        report = assess_flexibility(ref, flex)
        assert report.storage_charged_kWh == pytest.approx(400.0)
        assert report.storage_discharged_kWh == pytest.approx(180.0)
        assert report.storage_round_trip_thermal == pytest.approx(0.45)

    def test_electric_round_trip_uses_only_charging_and_discharging_periods(self):
        ref = make_frame([100_000] * 4)
        flex = make_frame([150_000, 150_000, 60_000, 100_000],
                          charge=[200_000, 200_000, 0, 0],
                          discharge=[0, 0, 180_000, 0])
        report = assess_flexibility(ref, flex)
        # extra while charging = 2 * 50 kWh = 100; avoided while discharging = 40
        assert report.electric_round_trip == pytest.approx(0.4)

    def test_round_trip_is_corrected_for_a_store_that_ends_fuller(self):
        # 400 kWh charged, 180 discharged, but 100 kWh is still in the tank: the
        # honest ratio is 180/(400-100), not 180/400.
        ref = make_frame([100_000] * 4)
        flex = make_frame([150_000, 150_000, 50_000, 100_000],
                          charge=[200_000, 200_000, 0, 0], discharge=[0, 0, 180_000, 0])
        flex["storage_energy_after_kWh"] = [500.0, 600.0, 580.0, 600.0]
        report = assess_flexibility(ref, flex)
        assert report.storage_net_soc_change_kWh == pytest.approx(100.0)
        assert report.storage_round_trip_thermal == pytest.approx(180.0 / 300.0)

    def test_round_trip_is_corrected_for_a_store_that_ends_emptier(self):
        ref = make_frame([100_000] * 4)
        flex = make_frame([150_000, 100_000, 50_000, 100_000],
                          charge=[200_000, 0, 0, 0], discharge=[0, 0, 180_000, 0])
        flex["storage_energy_after_kWh"] = [600.0, 590.0, 520.0, 500.0]
        report = assess_flexibility(ref, flex)
        assert report.storage_net_soc_change_kWh == pytest.approx(-100.0)
        # 100 kWh of the discharge came from stock, not from this horizon.
        assert report.storage_round_trip_thermal == pytest.approx(80.0 / 200.0)

    def test_electric_round_trip_is_withheld_on_a_non_cyclic_horizon(self):
        ref = make_frame([100_000] * 4)
        flex = make_frame([150_000, 150_000, 50_000, 100_000],
                          charge=[200_000, 200_000, 0, 0], discharge=[0, 0, 180_000, 0])
        flex["storage_energy_after_kWh"] = [500.0, 900.0, 850.0, 900.0]  # +400 of 400 charged
        report = assess_flexibility(ref, flex)
        assert report.electric_round_trip != report.electric_round_trip  # nan
        assert any("not meaningful" in n for n in report.notes)

    def test_charging_cop_penalty_compares_the_same_hours(self):
        # Flexible run is 0.2 COP worse while charging, better otherwise. A
        # charging-vs-non-charging comparison would be polluted by that; a
        # same-hour comparison against the reference is not.
        ref = make_frame([100_000] * 4, cop=[4.0, 4.0, 3.0, 3.0])
        flex = make_frame([100_000] * 4, cop=[3.8, 3.8, 3.5, 3.5],
                          charge=[100_000, 100_000, 0, 0])
        report = assess_flexibility(ref, flex)
        assert report.charging_cop_penalty == pytest.approx(-0.2)

    def test_mismatched_horizons_are_rejected(self):
        with pytest.raises(ValueError, match="different lengths"):
            assess_flexibility(make_frame([1e5] * 4), make_frame([1e5] * 3))

    def test_scalar_tariff_costs_are_computed(self):
        ref = make_frame([100_000] * 10)
        flex = make_frame([90_000] * 10)
        report = assess_flexibility(ref, flex, tariff=0.2)
        assert report.cost_reference == pytest.approx(1000 * 0.2)
        assert report.cost_flexible == pytest.approx(900 * 0.2)
        assert report.cost_saving == pytest.approx(20.0)

    def test_hour_of_day_tariff_is_applied(self):
        ref = make_frame([100_000] * 24)
        flex = make_frame([100_000] * 24)
        report = assess_flexibility(ref, flex, tariff={h: (1.0 if h == 12 else 0.0)
                                                       for h in range(24)})
        assert report.cost_reference == pytest.approx(100.0)

    def test_carbon_is_computed_when_intensity_is_supplied(self):
        ref = make_frame([100_000] * 10)
        flex = make_frame([80_000] * 10)
        report = assess_flexibility(ref, flex, carbon_intensity=0.5)
        assert report.carbon_saving_kg == pytest.approx(100.0)

    def test_peak_window_energy_shift(self):
        stamps = pd.date_range("2026-07-01", periods=24, freq="1h")
        ref = make_frame([200_000] * 24)
        flex = make_frame([200_000] * 24)
        flex.loc[flex.index[12:18], "compressor_power_W"] = 100_000
        report = assess_flexibility(ref, flex, peak_hours=range(12, 18))
        assert report.shifted_energy_kWh == pytest.approx(600.0)

    def test_markdown_report_is_produced(self):
        ref = make_frame([100_000, 400_000, 100_000, 100_000])
        flex = make_frame([200_000, 250_000, 150_000, 150_000],
                          charge=[100_000, 0, 0, 0], discharge=[0, 90_000, 0, 0])
        text = assess_flexibility(ref, flex, tariff=0.2, carbon_intensity=0.5).to_markdown()
        assert "Flexibility assessment" in text
        assert "Peak compressor power" in text
        assert "round-trip" in text

    def test_report_frame_is_one_row(self):
        ref = make_frame([100_000] * 4)
        flex = make_frame([100_000] * 4)
        assert len(assess_flexibility(ref, flex).to_frame()) == 1


class TestFlexibilityEnvelope:
    def _building(self):
        return Building(
            "b", Q_design=300_000.0, load_model="thermal_mass",
            envelope=EnvelopeThermal(UA_W_K=6000.0, indoor_setpoint_degC=24.0),
            thermal_mass=ThermalMass(capacitance_J_K=400e6, setpoint_degC=24.0,
                                     min_temperature_degC=21.0, max_temperature_degC=26.0),
        )

    def test_buildings_and_storage_both_contribute(self):
        store = ColdStorage(label="s", capacity_kWh=1000.0, initial_soc=0.5,
                            max_charge_kW=200.0, max_discharge_kW=200.0,
                            loss_model="ua",
                            thermal=StorageThermal(UA_W_K=80.0, storage_temperature_degC=0.0))
        env = flexibility_envelope([self._building()], store, dt_h=1.0,
                                   ambient_temperature_degC=40.0)
        assert env["building_increase_W"] > 0
        assert env["storage_increase_W"] > 0
        assert env["increase_W"] == pytest.approx(
            env["building_increase_W"] + env["storage_increase_W"]
        )

    def test_full_store_offers_no_charging_flexibility(self):
        store = ColdStorage(label="s", capacity_kWh=1000.0, initial_soc=0.95, max_soc=0.95,
                            max_charge_kW=200.0, max_discharge_kW=200.0,
                            standby_loss_fraction_per_day=0.0)
        env = flexibility_envelope([], store, dt_h=1.0, ambient_temperature_degC=40.0)
        assert env["storage_increase_W"] == pytest.approx(0.0)
        assert env["storage_decrease_W"] > 0

    def test_empty_store_offers_no_discharge_flexibility(self):
        store = ColdStorage(label="s", capacity_kWh=1000.0, initial_soc=0.05, min_soc=0.05,
                            max_charge_kW=200.0, max_discharge_kW=200.0,
                            standby_loss_fraction_per_day=0.0)
        env = flexibility_envelope([], store, dt_h=1.0, ambient_temperature_degC=40.0)
        assert env["storage_decrease_W"] == pytest.approx(0.0)
        assert env["storage_increase_W"] > 0

    def test_profile_buildings_contribute_nothing(self):
        plain = Building("plain", Q_design=100_000.0)
        env = flexibility_envelope([plain], None, dt_h=1.0, ambient_temperature_degC=40.0)
        assert env["increase_W"] == pytest.approx(0.0)
        assert env["decrease_W"] == pytest.approx(0.0)
