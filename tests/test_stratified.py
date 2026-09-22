"""The stratified chilled-water tank: physics, dispatch, and network coupling.

The physics tests check the tank against closed-form quantities rather than
against remembered output. Energy in has to equal energy stored, the standing
loss has to equal shell UA times the driving temperature difference, and the
profile has to stay buoyantly stable. The behavioural tests then pin down what
the model is for: a store stops delivering useful cooling once the thermocline
reaches the outlet, which happens well before its state of charge hits zero.
"""

import warnings

import pytest

from discoolpy import (
    ColdStorage,
    StratifiedTank,
    TimeSnapshot,
    build_system,
    load_yaml_config,
    make_weather_and_load_profiles,
    run_configured_case,
)
from discoolpy.config_schema import make_storage, make_stratified_tank

warnings.filterwarnings("ignore", category=FutureWarning)

CP = 4180.0
CAMPUS = "config_campus_five_buildings.yaml"


def tank(**kwargs) -> StratifiedTank:
    base = dict(
        volume_m3=600.0,
        height_to_diameter=2.5,
        layers=12,
        charged_temperature_degC=5.0,
        discharged_temperature_degC=13.0,
        initial_soc=0.0,
    )
    base.update(kwargs)
    return StratifiedTank(**base)


def hours(t: StratifiedTank, n: int, flow: float, inlet: float, mode: str, env=None):
    for _ in range(n):
        t.step(1.0, flow, inlet, mode, env)


# Geometry and capacity

class TestGeometry:
    def test_capacity_is_mass_times_cp_times_span(self):
        t = tank()
        expected = t.total_mass_kg * CP * 8.0 / 3.6e6
        assert t.capacity_kWh == pytest.approx(expected)

    def test_geometry_is_consistent_however_it_is_given(self):
        by_ratio = tank(volume_m3=600.0, height_to_diameter=2.5)
        by_height = tank(volume_m3=600.0, height_m=by_ratio.height_m)
        by_both = tank(volume_m3=None, height_m=by_ratio.height_m,
                       diameter_m=by_ratio.diameter_m)
        for other in (by_height, by_both):
            assert other.volume_m3 == pytest.approx(by_ratio.volume_m3, rel=1e-9)
            assert other.diameter_m == pytest.approx(by_ratio.diameter_m, rel=1e-9)

    def test_under_specified_geometry_is_refused(self):
        with pytest.raises(ValueError, match="under-specified"):
            StratifiedTank(charged_temperature_degC=5.0, discharged_temperature_degC=13.0)

    def test_an_inverted_temperature_span_is_refused(self):
        with pytest.raises(ValueError, match="must be above"):
            tank(charged_temperature_degC=13.0, discharged_temperature_degC=5.0)

    def test_a_tank_needs_at_least_two_layers(self):
        with pytest.raises(ValueError, match="at least 2 layers"):
            tank(layers=1)

    def test_initial_soc_places_a_sharp_thermocline(self):
        """Half charged means half the height cold, not all of it lukewarm."""
        t = tank(initial_soc=0.5, layers=10)
        profile = t.temperatures_degC
        assert profile[:5] == pytest.approx([5.0] * 5)
        assert profile[5:] == pytest.approx([13.0] * 5)
        assert t.soc == pytest.approx(0.5)


# Conservation

class TestConservation:
    def test_charging_stores_exactly_what_the_loop_gave_up(self):
        t = tank(initial_soc=0.0)
        before = t.energy_kWh
        result = t.step(1.0, 20.0, 5.0, "charge", ambient_temperature_degC=None)
        assert t.energy_kWh - before == pytest.approx(
            result.charge_power_W / 1e3, rel=1e-9
        )

    def test_discharging_gives_up_exactly_what_it_delivered(self):
        t = tank(initial_soc=1.0)
        before = t.energy_kWh
        result = t.step(1.0, 20.0, 13.0, "discharge", ambient_temperature_degC=None)
        assert before - t.energy_kWh == pytest.approx(
            result.discharge_power_W / 1e3, rel=1e-9
        )

    def test_a_sealed_idle_tank_holds_its_energy(self):
        t = tank(initial_soc=0.6)
        before = t.energy_kWh
        hours(t, 24, 0.0, None, "idle", env=None)
        assert t.energy_kWh == pytest.approx(before, rel=1e-9)

    def test_conduction_moves_heat_without_creating_it(self):
        """Vertical conduction is internal, so it must sum to zero."""
        t = tank(initial_soc=0.5)
        before = t.energy_kWh
        fresh = t.thermocline_thickness_m()
        hours(t, 12, 0.0, None, "idle", env=None)
        assert t.energy_kWh == pytest.approx(before, rel=1e-9)
        # ...but it has smeared the step. Conduction alone is slow, so 12 h
        # barely moves it. Mixing is what destroys stratification in practice.
        assert t.thermocline_thickness_m() > fresh

    def test_standing_loss_matches_the_shell_conductance(self):
        t = tank(initial_soc=1.0)
        ua = t.describe()["shell_UA_W_K"]
        result = t.step(1.0, 0.0, None, "idle", ambient_temperature_degC=35.0)
        expected_W = ua * (35.0 - 5.0)
        assert result.ambient_heat_gain_W == pytest.approx(expected_W, rel=0.02)


# Stratification

class TestStratification:
    @staticmethod
    def monotonic(profile):
        return all(profile[i] <= profile[i + 1] + 1e-9 for i in range(len(profile) - 1))

    def test_the_profile_stays_buoyantly_stable(self):
        t = tank(initial_soc=0.5)
        for _ in range(10):
            t.step(0.5, 25.0, 5.0, "charge", 35.0)
            assert self.monotonic(t.temperatures_degC)
        for _ in range(10):
            t.step(0.5, 25.0, 13.0, "discharge", 35.0)
            assert self.monotonic(t.temperatures_degC)

    def test_mixing_an_unstable_pair_conserves_energy(self):
        from discoolpy.stratified import _mix_unstable

        unstable = [12.0, 6.0, 9.0, 5.0]
        mixed = _mix_unstable(unstable)
        assert sum(mixed) == pytest.approx(sum(unstable))
        assert self.monotonic(mixed)

    def test_charging_grows_the_cold_zone_from_the_bottom(self):
        t = tank(initial_soc=0.0)
        hours(t, 3, 25.0, 5.0, "charge", 35.0)
        profile = t.temperatures_degC
        assert profile[0] < profile[-1]
        assert profile[0] == pytest.approx(5.0, abs=0.2)

    def test_discharging_grows_the_warm_zone_from_the_top(self):
        t = tank(initial_soc=1.0)
        hours(t, 3, 25.0, 13.0, "discharge", 35.0)
        profile = t.temperatures_degC
        assert profile[-1] == pytest.approx(13.0, abs=0.2)
        assert profile[0] < profile[-1]

    def test_a_uniform_tank_reports_no_thermocline(self):
        assert tank(initial_soc=0.0).thermocline_thickness_m() == 0.0
        assert tank(initial_soc=1.0).thermocline_thickness_m() == 0.0

    def test_the_thermocline_thickens_as_the_tank_stands(self):
        t = tank(initial_soc=0.5)
        fresh = t.thermocline_thickness_m()
        hours(t, 48, 0.0, None, "idle", 35.0)
        assert t.thermocline_thickness_m() > fresh


# The behaviour the model exists for

class TestOutletDegradation:
    def test_the_outlet_warms_as_the_store_drains(self):
        t = tank(volume_m3=600.0, initial_soc=1.0)
        outlets = [
            t.step(1.0, 40.0, 13.0, "discharge", 38.0).outlet_temperature_degC
            for _ in range(6)
        ]
        assert outlets[0] == pytest.approx(5.0, abs=0.05)
        assert outlets == sorted(outlets), "the outlet must warm monotonically"
        assert outlets[-1] > 10.0, "a drained tank delivers return-temperature water"

    def test_a_worn_thermocline_costs_usable_capacity(self):
        """The whole point of resolving the tank.

        Stored kWh is not the same as *deliverable* kWh. A tank that has been
        cycled carries a thick thermocline, so a larger share of its charge can
        only be handed over as lukewarm water, useless to a district that needs
        a supply temperature. Measured as the fraction of stored cooling that can
        be delivered below a temperature limit, a worn tank is materially worse
        than a fresh one holding the same energy per kWh. A scalar store cannot
        tell the two apart and so overstates the worn one.
        """
        def mean_delivery_temperature(t):
            """Energy-weighted mean temperature over a full drain.

            Weighted by energy rather than time, and taken over the whole
            drain rather than up to a threshold, so the answer does not hinge
            on where a cut-off happens to land between two layers.
            """
            weighted = total = 0.0
            for _ in range(40):
                result = t.step(0.5, 40.0, 13.0, "discharge", 38.0)
                if result.discharge_power_W <= 0:
                    break
                energy = result.discharge_power_W * 0.5
                weighted += result.outlet_temperature_degC * energy
                total += energy
            return weighted / total

        fresh = tank(volume_m3=600.0, initial_soc=1.0)
        worn = tank(volume_m3=600.0, initial_soc=1.0)
        for _ in range(3):                      # partial cycles blur the front
            hours(worn, 3, 40.0, 13.0, "discharge", 38.0)
            hours(worn, 3, 40.0, 5.0, "charge", 38.0)
        hours(worn, 12, 0.0, None, "idle", 38.0)

        assert fresh.thermocline_thickness_m() < 1.0
        assert mean_delivery_temperature(worn) > mean_delivery_temperature(fresh) + 0.3

    @pytest.mark.parametrize("layers", [8, 12, 20])
    def test_the_wear_penalty_is_not_an_artefact_of_the_grid(self, layers):
        """The same conclusion must hold however finely the tank is resolved."""
        def mean_delivery(t):
            weighted = total = 0.0
            for _ in range(40):
                result = t.step(0.5, 40.0, 13.0, "discharge", 38.0)
                if result.discharge_power_W <= 0:
                    break
                energy = result.discharge_power_W * 0.5
                weighted += result.outlet_temperature_degC * energy
                total += energy
            return weighted / total

        fresh = tank(volume_m3=600.0, layers=layers, initial_soc=1.0)
        worn = tank(volume_m3=600.0, layers=layers, initial_soc=1.0)
        for _ in range(3):
            hours(worn, 3, 40.0, 13.0, "discharge", 38.0)
            hours(worn, 3, 40.0, 5.0, "charge", 38.0)
        hours(worn, 12, 0.0, None, "idle", 38.0)
        assert mean_delivery(worn) > mean_delivery(fresh) + 0.3

    def test_delivered_power_collapses_as_the_thermocline_arrives(self):
        t = tank(volume_m3=600.0, initial_soc=1.0)
        powers = [
            t.step(1.0, 40.0, 13.0, "discharge", 38.0).discharge_power_W
            for _ in range(6)
        ]
        assert powers[0] > 1.2e6
        assert powers[-1] < 0.5 * powers[0]

    def test_the_flow_needed_for_a_given_power_runs_away(self):
        t = tank(volume_m3=600.0, initial_soc=1.0)
        fresh = t.mass_flow_for_power(1e6, 13.0, "discharge")
        hours(t, 5, 40.0, 13.0, "discharge", 38.0)
        assert t.mass_flow_for_power(1e6, 13.0, "discharge") > fresh

    def test_more_layers_resolve_a_sharper_thermocline(self):
        coarse = tank(layers=4, initial_soc=1.0)
        fine = tank(layers=24, initial_soc=1.0)
        for t in (coarse, fine):
            hours(t, 3, 25.0, 13.0, "discharge", 35.0)
        assert fine.thermocline_thickness_m() < coarse.thermocline_thickness_m()


# ColdStorage integration

def store(**kwargs) -> ColdStorage:
    base = dict(
        label="tes",
        capacity_kWh=1.0,                 # ignored: the tank defines it
        initial_soc=1.0,
        max_charge_kW=1500.0,
        max_discharge_kW=1500.0,
        coupling="hydraulic",
        loss_model="ua",
        min_soc=0.05,
        max_soc=0.95,
        stratified=tank(volume_m3=600.0, initial_soc=1.0),
    )
    base.update(kwargs)
    return ColdStorage(**base)


def snapshot(**kwargs) -> TimeSnapshot:
    from datetime import timedelta

    base = dict(
        timestamp="2026-07-01 12:00",
        building_loads={"b": 1.0},
        resolution=timedelta(hours=1),
        ambient_temperature=38.0,
    )
    base.update(kwargs)
    return TimeSnapshot(**base)


class TestColdStorageIntegration:
    def test_the_tank_defines_the_capacity(self):
        s = store()
        assert s.is_stratified
        assert s.capacity_kWh == pytest.approx(s.stratified.capacity_kWh)
        assert s.energy_kWh == pytest.approx(s.stratified.energy_kWh)

    def test_energy_cannot_drift_from_the_profile(self):
        s = store()
        s.dispatch(snapshot(), requested_storage_power_W=800e3,
                   return_temperature_degC=13.0)
        assert s.energy_kWh == pytest.approx(s.stratified.energy_kWh)
        assert s.soc == pytest.approx(s.stratified.soc)

    def test_reset_restores_the_profile(self):
        s = store()
        s.dispatch(snapshot(), requested_storage_power_W=1200e3,
                   return_temperature_degC=13.0)
        s.reset(1.0)
        assert s.soc == pytest.approx(1.0)
        assert s.stratified.cold_port_temperature_degC == pytest.approx(5.0)

    def test_a_request_the_tank_cannot_meet_is_curtailed(self):
        s = store()
        records = [
            s.dispatch(snapshot(), requested_storage_power_W=1200e3,
                       return_temperature_degC=13.0)
            for _ in range(6)
        ]
        assert records[0].curtailed_request_W == pytest.approx(0.0, abs=1e3)
        assert any(r.curtailed_request_W > 1e4 for r in records), (
            "a draining tank must eventually fail to meet its request"
        )

    def test_the_soc_band_is_respected(self):
        s = store(min_soc=0.2, max_soc=0.8, initial_soc=0.8,
                  stratified=tank(volume_m3=600.0, initial_soc=0.8))
        for _ in range(20):
            s.dispatch(snapshot(), requested_storage_power_W=1500e3,
                       return_temperature_degC=13.0)
        assert s.soc >= 0.2 - 0.02
        for _ in range(20):
            s.dispatch(snapshot(), requested_storage_power_W=-1500e3,
                       supply_temperature_degC=5.0)
        assert s.soc <= 0.8 + 0.02

    def test_the_loop_offset_is_charge_minus_discharge(self):
        s = store()
        charging = s.dispatch(snapshot(), requested_storage_power_W=-600e3,
                              supply_temperature_degC=5.0)
        assert charging.chiller_load_offset_W == pytest.approx(
            charging.charge_power_W - charging.discharge_power_W
        )
        assert charging.chiller_load_offset_W >= 0.0

    def test_temperature_guards_still_apply(self):
        s = store(discharge_allowed_below_degC=35.0)
        result = s.dispatch(snapshot(ambient_temperature=18.0),
                            requested_storage_power_W=800e3,
                            return_temperature_degC=13.0)
        assert result.mode == "idle_temperature_guard"
        assert result.storage_power_W == 0.0

    def test_the_record_carries_the_profile(self):
        s = store()
        record = s.dispatch(snapshot(), requested_storage_power_W=800e3,
                            return_temperature_degC=13.0).to_record()
        assert record["storage_cold_port_degC"] == pytest.approx(
            s.stratified.cold_port_temperature_degC
        )
        layers = [k for k in record if k.startswith("storage_tank_T")]
        assert len(layers) == s.stratified.layers

    def test_a_scalar_store_reports_no_tank_columns(self):
        plain = ColdStorage(label="plain", capacity_kWh=1000.0, initial_soc=0.5)
        record = plain.dispatch(snapshot(), requested_storage_power_W=100e3).to_record()
        assert not any("tank" in key for key in record)


class TestConfiguration:
    def test_a_stratified_section_builds_a_tank(self):
        cfg = {
            "storage": {
                "enabled": True, "label": "tes", "capacity_kWh": 1.0,
                "coupling": "hydraulic", "loss_model": "ua",
                "stratified": {
                    "enabled": True, "volume_m3": 500.0, "layers": 10,
                    "charged_temperature_degC": 6.0,
                    "discharged_temperature_degC": 13.0,
                },
            }
        }
        built = make_storage(cfg)
        assert built.is_stratified and built.stratified.layers == 10

    def test_temperatures_fall_back_to_the_thermal_block(self):
        cfg = {
            "storage": {
                "enabled": True, "capacity_kWh": 1.0, "loss_model": "ua",
                "thermal": {
                    "volume_m3": 500.0,
                    "storage_temperature_degC": 6.0,
                    "discharged_temperature_degC": 12.0,
                },
                "stratified": {"enabled": True, "layers": 8},
            }
        }
        built = make_storage(cfg)
        assert built.stratified.charged_temperature_degC == pytest.approx(6.0)
        assert built.stratified.volume_m3 == pytest.approx(500.0)

    def test_a_stratified_tank_needs_its_temperature_span(self):
        with pytest.raises(ValueError, match="charged_temperature_degC"):
            make_stratified_tank({"stratified": {"enabled": True, "volume_m3": 100.0}})

    def test_double_counting_the_standing_loss_is_refused(self):
        cfg = {
            "storage": {
                "enabled": True, "capacity_kWh": 1.0, "loss_model": "both",
                "stratified": {
                    "enabled": True, "volume_m3": 500.0,
                    "charged_temperature_degC": 6.0,
                    "discharged_temperature_degC": 13.0,
                },
            }
        }
        with pytest.raises(ValueError, match="twice"):
            make_storage(cfg)

    def test_no_stratified_section_leaves_a_scalar_store(self, tmp_config):
        built = make_storage(tmp_config("config_riyadh_heat_gains.yaml"))
        assert built is not None and not built.is_stratified


# In the network

class TestInTheNetwork:
    def test_the_campus_scenario_carries_a_stratified_tank(self, tmp_config):
        system = build_system(tmp_config(CAMPUS))
        assert system.storage.is_stratified
        assert system.storage.coupling == "hydraulic"

    def test_it_reaches_a_converged_design_point(self, tmp_config):
        system = build_system(tmp_config(CAMPUS))
        system.network.solve(mode="design", max_iter=300)
        assert system.network.converged

    def test_tank_temperatures_are_checked_against_the_setpoints(self, tmp_config):
        """A tank that cannot reach its charged temperature is called out."""
        cfg = tmp_config(CAMPUS)
        cfg["storage"]["stratified"]["charged_temperature_degC"] = 2.0
        with pytest.warns(UserWarning, match="can never reach"):
            system = build_system(cfg)
        assert any("never reach" in note for note in system.notes)

    @pytest.mark.slow
    def test_it_runs_offdesign_and_closes_its_energy_balance(self, tmp_config):
        cfg = tmp_config(CAMPUS)
        cfg["profiles"]["periods"] = 24
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "strat", use_storage=True,
                                      progress=False)
        assert len(results) == 24
        assert results["chw_energy_residual_W"].abs().max() < 1.0

        # The profile reaches the results, and the ports stay inside the span.
        assert len([c for c in results.columns if c.startswith("storage_tank_T")]) == 14
        cold = results["storage_cold_port_degC"]
        warm = results["storage_warm_port_degC"]
        assert (cold >= 6.9).all() and (cold <= 13.6).all()
        assert (warm >= cold - 1e-6).all()

    @pytest.mark.slow
    def test_the_store_actually_cycles(self, tmp_config):
        cfg = tmp_config(CAMPUS)
        cfg["profiles"]["periods"] = 48
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "strat", use_storage=True,
                                      progress=False)
        soc = results["storage_soc_after"]
        assert soc.max() - soc.min() > 0.2, "the store must charge and discharge"
        assert (results["storage_charge_power_W"] > 1e3).any()
        assert (results["storage_discharge_power_W"] > 1e3).any()
