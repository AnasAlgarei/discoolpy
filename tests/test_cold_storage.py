"""Cold-storage dispatch, losses and state-of-charge bookkeeping."""

from datetime import datetime, timedelta

import pytest

from discoolpy.cold_storage import ColdStorage
from discoolpy.thermal import StorageThermal
from discoolpy.time_snapshot import TimeSnapshot


def snapshot(hour=12, ambient=40.0, resolution="hourly", **metadata):
    return TimeSnapshot(
        timestamp=datetime(2026, 7, 1, hour),
        building_loads={"b": 500_000.0},
        resolution=resolution,
        ambient_temperature=ambient,
        metadata=metadata,
    )


def make_store(**kwargs):
    defaults = dict(
        label="store", capacity_kWh=1000.0, initial_soc=0.5,
        max_charge_kW=200.0, max_discharge_kW=200.0,
        charge_efficiency=0.9, discharge_efficiency=0.95,
        min_soc=0.05, max_soc=0.95,
        # Loss-free by default so bookkeeping tests isolate the charge/discharge
        # arithmetic; loss tests below switch it on explicitly.
        standby_loss_fraction_per_day=0.0,
    )
    defaults.update(kwargs)
    return ColdStorage(**defaults)


class TestConstruction:
    def test_rejects_nonpositive_capacity(self):
        with pytest.raises(ValueError):
            make_store(capacity_kWh=0.0)

    def test_rejects_inverted_soc_bounds(self):
        with pytest.raises(ValueError):
            make_store(min_soc=0.8, max_soc=0.2)

    def test_ua_loss_model_demands_a_conductance(self):
        with pytest.raises(ValueError, match="StorageThermal"):
            make_store(loss_model="ua")

    def test_unknown_loss_model_is_rejected(self):
        with pytest.raises(ValueError):
            make_store(loss_model="magic")

    def test_unknown_coupling_is_rejected(self):
        with pytest.raises(ValueError):
            make_store(coupling="telepathic")

    def test_initial_energy_is_clamped_into_the_operating_band(self):
        store = make_store(initial_soc=0.99, max_soc=0.9)
        assert store.soc == pytest.approx(0.9)


class TestAmbientHeatGain:
    def test_gain_scales_with_ambient_temperature(self):
        store = make_store(loss_model="ua",
                           thermal=StorageThermal(UA_W_K=100.0, storage_temperature_degC=0.0))
        assert store.ambient_heat_gain_W(45.0) == pytest.approx(4500.0)
        assert store.ambient_heat_gain_W(15.0) == pytest.approx(1500.0)

    def test_fraction_model_ignores_ambient_entirely(self):
        # This is precisely the deficiency the UA model fixes.
        store = make_store(loss_model="fraction", standby_loss_fraction_per_day=0.02)
        assert store.ambient_heat_gain_W(45.0) == 0.0
        hot = store.dispatch(snapshot(ambient=45.0), requested_storage_power_W=0.0)
        store.reset()
        cold = store.dispatch(snapshot(ambient=5.0), requested_storage_power_W=0.0)
        assert hot.standby_loss_kWh == pytest.approx(cold.standby_loss_kWh)

    def test_ua_model_loses_more_on_a_hot_afternoon(self):
        store = make_store(loss_model="ua",
                           thermal=StorageThermal(UA_W_K=100.0, storage_temperature_degC=0.0))
        hot = store.dispatch(snapshot(ambient=45.0), requested_storage_power_W=0.0)
        store.reset()
        cold = store.dispatch(snapshot(ambient=15.0), requested_storage_power_W=0.0)
        assert hot.standby_loss_kWh == pytest.approx(3 * cold.standby_loss_kWh)

    def test_both_model_adds_the_two_channels(self):
        thermal = StorageThermal(UA_W_K=100.0, storage_temperature_degC=0.0)
        both = make_store(loss_model="both", thermal=thermal,
                          standby_loss_fraction_per_day=0.02)
        result = both.dispatch(snapshot(ambient=40.0), requested_storage_power_W=0.0)
        assert result.ambient_heat_gain_kWh == pytest.approx(4.0)
        assert result.fractional_loss_kWh > 0
        assert result.standby_loss_kWh == pytest.approx(
            result.ambient_heat_gain_kWh + result.fractional_loss_kWh
        )

    def test_losses_never_take_the_store_below_its_reserve(self):
        store = make_store(initial_soc=0.06, loss_model="ua",
                           thermal=StorageThermal(UA_W_K=5000.0, storage_temperature_degC=0.0))
        store.dispatch(snapshot(ambient=45.0, resolution="daily"),
                       requested_storage_power_W=0.0)
        assert store.soc >= store.min_soc - 1e-12

    def test_storage_ambient_can_be_overridden_in_metadata(self):
        store = make_store(loss_model="ua",
                           thermal=StorageThermal(UA_W_K=100.0, storage_temperature_degC=0.0))
        result = store.dispatch(
            snapshot(ambient=40.0, storage_ambient_temperature_degC=20.0),
            requested_storage_power_W=0.0,
        )
        assert result.ambient_heat_gain_W == pytest.approx(2000.0)


class TestEnergyBookkeeping:
    def test_charging_stores_the_efficiency_weighted_energy(self):
        store = make_store()
        before = store.energy_kWh
        store.dispatch(snapshot(), requested_storage_power_W=-100_000.0)
        assert store.energy_kWh - before == pytest.approx(0.9 * 100.0)

    def test_discharging_draws_more_than_it_delivers(self):
        store = make_store()
        before = store.energy_kWh
        result = store.dispatch(snapshot(), requested_storage_power_W=100_000.0)
        assert result.discharge_power_W == pytest.approx(100_000.0)
        assert before - store.energy_kWh == pytest.approx(100.0 / 0.95)

    def test_power_limits_are_respected(self):
        store = make_store(max_charge_kW=50.0)
        result = store.dispatch(snapshot(), requested_storage_power_W=-500_000.0)
        assert result.charge_power_W == pytest.approx(50_000.0)
        assert result.curtailed_request_W == pytest.approx(450_000.0)

    def test_charging_stops_at_the_upper_soc_bound(self):
        store = make_store(initial_soc=0.94, max_soc=0.95)
        store.dispatch(snapshot(), requested_storage_power_W=-200_000.0)
        assert store.soc <= 0.95 + 1e-12

    def test_discharging_stops_at_the_lower_soc_bound(self):
        store = make_store(initial_soc=0.06, min_soc=0.05)
        store.dispatch(snapshot(), requested_storage_power_W=200_000.0)
        assert store.soc >= 0.05 - 1e-12

    def test_soc_stays_in_band_over_a_long_random_walk(self):
        store = make_store(loss_model="ua",
                           thermal=StorageThermal(UA_W_K=80.0, storage_temperature_degC=0.0))
        for hour in range(200):
            power = -150_000.0 if hour % 24 < 8 else 120_000.0
            store.dispatch(snapshot(hour=hour % 24, ambient=25 + 15 * (hour % 24) / 24),
                           requested_storage_power_W=power)
            assert store.min_soc - 1e-12 <= store.soc <= store.max_soc + 1e-12

    def test_offset_sign_convention(self):
        store = make_store()
        charge = store.dispatch(snapshot(), requested_storage_power_W=-50_000.0)
        assert charge.chiller_load_offset_W == pytest.approx(50_000.0)  # plant does more
        store.reset()
        discharge = store.dispatch(snapshot(), requested_storage_power_W=50_000.0)
        assert discharge.chiller_load_offset_W == pytest.approx(-50_000.0)  # plant does less

    def test_sub_hourly_resolution_scales_energy(self):
        store = make_store()
        before = store.energy_kWh
        store.dispatch(snapshot(resolution="30min"), requested_storage_power_W=-100_000.0)
        assert store.energy_kWh - before == pytest.approx(0.9 * 100.0 * 0.5)


class TestDispatchModes:
    def test_load_levelling_discharges_above_the_target(self):
        store = make_store(target_chiller_load_kW=400.0)
        result = store.dispatch(snapshot(), base_chiller_load_W=600_000.0)
        assert result.mode == "discharge"
        assert result.discharge_power_W > 0

    def test_load_levelling_charges_below_the_target(self):
        store = make_store(target_chiller_load_kW=400.0)
        result = store.dispatch(snapshot(), base_chiller_load_W=250_000.0)
        assert result.mode == "charge"
        assert result.charge_power_W > 0

    def test_explicit_metadata_power_overrides_the_controller(self):
        store = make_store(target_chiller_load_kW=400.0)
        result = store.dispatch(snapshot(storage_power_W=12_345.0), base_chiller_load_W=900_000.0)
        assert result.storage_power_W == pytest.approx(12_345.0)

    def test_idle_mode_does_nothing(self):
        store = make_store()
        result = store.dispatch(snapshot(), mode="idle", requested_storage_power_W=None)
        assert result.storage_power_W == 0.0

    def test_temperature_guard_blocks_charging_when_too_warm(self):
        store = make_store(charge_allowed_above_degC=25.0)
        result = store.dispatch(snapshot(ambient=40.0), requested_storage_power_W=-100_000.0)
        assert result.mode == "idle_temperature_guard"
        assert result.storage_power_W == 0.0

    def test_unsupported_mode_is_rejected(self):
        with pytest.raises(ValueError):
            make_store().dispatch(snapshot(), mode="freeze-everything")


class TestHydraulicCoupling:
    def test_element_is_created_once(self):
        store = make_store(coupling="hydraulic")
        assert store.create_hydraulic_element() is store.create_hydraulic_element()

    def test_accessing_the_element_before_creation_is_an_error(self):
        with pytest.raises(RuntimeError, match="hydraulic"):
            _ = make_store().heat_exchanger

    def test_dispatch_maps_onto_the_element_duty(self):
        store = make_store(coupling="hydraulic")
        store.create_hydraulic_element()
        result = store.dispatch(snapshot(), requested_storage_power_W=-80_000.0)
        # Charging adds heat to the chilled water: the plant must remove it again.
        assert store.apply_dispatch_to_network(result) == pytest.approx(80_000.0)
        store.reset()
        result = store.dispatch(snapshot(), requested_storage_power_W=80_000.0)
        assert store.apply_dispatch_to_network(result) == pytest.approx(-80_000.0)


class TestGeometryHelper:
    def test_thermal_from_geometry_produces_a_sane_conductance(self):
        thermal = ColdStorage.thermal_from_geometry(
            volume_m3=420.0, insulation_thickness_m=0.10, insulation_conductivity="pur",
            storage_temperature_degC=0.0,
        )
        assert 40.0 < thermal.UA_W_K < 200.0
        assert thermal.storage_temperature_degC == 0.0


class TestReporting:
    def test_records_are_flat_and_prefixed(self):
        store = make_store()
        store.dispatch(snapshot(), requested_storage_power_W=-50_000.0)
        record = store.history_records(prefix="ice")[0]
        assert record["ice_charge_power_W"] == pytest.approx(50_000.0)
        assert "ice_ambient_heat_gain_W" in record
        assert all(isinstance(k, str) for k in record)

    def test_reset_clears_history(self):
        store = make_store()
        store.dispatch(snapshot(), requested_storage_power_W=-50_000.0)
        store.reset()
        assert store.history == []
        assert store.soc == pytest.approx(0.5)
