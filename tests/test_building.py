"""Building envelope and thermal-mass behaviour."""

import math

import pytest

from discoolpy.building import Building, ThermalMass
from discoolpy.thermal import EnvelopeThermal


def make_mass(**kwargs):
    defaults = dict(
        capacitance_J_K=400e6, setpoint_degC=24.0,
        min_temperature_degC=21.0, max_temperature_degC=26.0,
    )
    defaults.update(kwargs)
    return ThermalMass(**defaults)


UA = 6000.0


class TestThermalMassConstruction:
    def test_rejects_nonpositive_capacitance(self):
        with pytest.raises(ValueError):
            ThermalMass(capacitance_J_K=0.0)

    def test_rejects_setpoint_outside_the_band(self):
        with pytest.raises(ValueError):
            ThermalMass(capacitance_J_K=1e8, setpoint_degC=30.0,
                        min_temperature_degC=21.0, max_temperature_degC=26.0)

    def test_starts_at_the_setpoint(self):
        assert make_mass().indoor_temperature_degC == 24.0


class TestExactIntegration:
    def test_tracking_holds_the_setpoint_exactly(self):
        mass = make_mass()
        for _ in range(24):
            state = mass.step(1.0, UA, ambient_degC=40.0, gain_W=50_000, mode="track")
            assert state.indoor_temperature_after_degC == pytest.approx(24.0, abs=1e-9)

    def test_tracking_cooling_equals_the_steady_gain(self):
        mass = make_mass()
        state = mass.step(1.0, UA, ambient_degC=40.0, gain_W=50_000, mode="track")
        assert state.cooling_delivered_W == pytest.approx(UA * 16.0 + 50_000)

    def test_coasting_relaxes_towards_the_equilibrium_temperature(self):
        # With no cooling the zone must approach T_air + gain/UA exponentially.
        mass = make_mass(max_temperature_degC=200.0)
        t_equilibrium = 40.0 + 50_000 / UA
        tau_h = 400e6 / (UA * 3600.0)
        before = mass.indoor_temperature_degC
        mass.step(2.0, UA, 40.0, 50_000, mode="coast")
        expected = t_equilibrium + (before - t_equilibrium) * math.exp(-2.0 / tau_h)
        assert mass.indoor_temperature_degC == pytest.approx(expected)

    def test_zero_cooling_and_neutral_weather_leaves_temperature_unchanged(self):
        mass = make_mass()
        mass.step(1.0, UA, ambient_degC=24.0, gain_W=0.0, mode="coast")
        assert mass.indoor_temperature_degC == pytest.approx(24.0)

    def test_step_size_does_not_change_the_trajectory(self):
        # Exact integration means one 4 h step must land where four 1 h steps do.
        # A forward-Euler scheme would not, which is why the band could be
        # violated by a scheme chosen for convenience.
        coarse = make_mass(max_temperature_degC=200.0)
        coarse.step(4.0, UA, 40.0, 50_000, mode="coast")
        fine = make_mass(max_temperature_degC=200.0)
        for _ in range(4):
            fine.step(1.0, UA, 40.0, 50_000, mode="coast")
        assert coarse.indoor_temperature_degC == pytest.approx(fine.indoor_temperature_degC)


class TestComfortBand:
    def test_precooling_never_undershoots_the_floor(self):
        mass = make_mass()
        for _ in range(48):
            mass.step(0.5, UA, 38.0, 40_000, mode="precool")
            assert mass.indoor_temperature_degC >= mass.min_temperature_degC - 1e-9

    def test_coasting_never_overshoots_the_ceiling(self):
        mass = make_mass()
        for _ in range(48):
            state = mass.step(0.5, UA, 45.0, 80_000, mode="coast")
            assert mass.indoor_temperature_degC <= mass.max_temperature_degC + 1e-9
        # Once pinned at the ceiling it must be spending cooling, not coasting free.
        assert state.cooling_delivered_W > 0

    def test_terminal_capacity_limits_precooling(self):
        limited = make_mass(max_cooling_W=100_000)
        state = limited.step(0.5, UA, 42.0, 60_000, mode="precool")
        assert state.cooling_delivered_W == pytest.approx(100_000)
        assert limited.indoor_temperature_degC > limited.min_temperature_degC

    def test_cooling_is_never_negative(self):
        # A district-cooling building may not inject heat back into the loop.
        mass = make_mass()
        state = mass.step(1.0, UA, ambient_degC=5.0, gain_W=0.0, mode="track")
        assert state.cooling_delivered_W >= 0.0

    def test_precool_rate_limit_is_separate_from_coil_capacity(self):
        mass = make_mass(max_cooling_W=400_000, precool_max_cooling_W=250_000)
        precool = mass.step(0.5, UA, 40.0, 60_000, mode="precool")
        assert precool.cooling_delivered_W == pytest.approx(250_000)

    def test_defending_the_ceiling_may_use_full_coil_capacity(self):
        # The pre-cooling rate limit is a control choice; staying inside the
        # comfort band is an obligation, so the ceiling gets the whole coil.
        mass = make_mass(max_cooling_W=400_000, precool_max_cooling_W=100_000)
        mass.indoor_temperature_degC = mass.max_temperature_degC
        state = mass.step(0.5, UA, 45.0, 120_000, mode="coast")
        assert state.cooling_delivered_W > 100_000
        assert mass.indoor_temperature_degC <= mass.max_temperature_degC + 1e-9

    def test_recover_mode_is_rate_limited_tracking(self):
        limited = make_mass(max_cooling_W=400_000, precool_max_cooling_W=150_000)
        limited.indoor_temperature_degC = 25.5
        snap = make_mass(max_cooling_W=400_000, precool_max_cooling_W=150_000)
        snap.indoor_temperature_degC = 25.5
        recovered = limited.step(0.5, UA, 38.0, 60_000, mode="recover")
        tracked = snap.step(0.5, UA, 38.0, 60_000, mode="track")
        assert recovered.cooling_delivered_W == pytest.approx(150_000)
        assert tracked.cooling_delivered_W > recovered.cooling_delivered_W
        # ...and therefore comes back to setpoint more gradually.
        assert limited.indoor_temperature_degC > snap.indoor_temperature_degC

    def test_band_violation_is_reported_when_the_coil_runs_out(self):
        starved = make_mass(max_cooling_W=50_000)
        starved.indoor_temperature_degC = starved.max_temperature_degC
        state = starved.step(1.0, UA, 45.0, 200_000, mode="coast")
        assert state.band_violation_K > 0
        assert state.capacity_limited is True

    def test_no_violation_reported_when_the_coil_is_adequate(self):
        ample = make_mass(max_cooling_W=1_000_000)
        state = ample.step(1.0, UA, 45.0, 200_000, mode="coast")
        assert state.band_violation_K == pytest.approx(0.0)
        assert state.capacity_limited is False

    def test_manual_mode_requires_a_value(self):
        with pytest.raises(ValueError):
            make_mass().step(1.0, UA, 30.0, 0.0, mode="manual")

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError):
            make_mass().step(1.0, UA, 30.0, 0.0, mode="turbo")


class TestEnergyConservation:
    def test_precool_then_coast_conserves_energy_within_the_envelope(self):
        """Stored 'coolth' plus delivered cooling must equal the gains absorbed.

        Integrating C dT/dt = gains - Q_cool over the whole horizon gives
        C*(T_start - T_end) = sum(Q_cool*dt) - sum(gains*dt), where gains include
        the conduction term evaluated at the moving indoor temperature.
        """
        mass = make_mass()
        dt = 0.5
        delivered = 0.0
        conduction = 0.0
        modes = ["precool"] * 12 + ["coast"] * 12 + ["track"] * 24
        start = mass.indoor_temperature_degC
        for mode in modes:
            before = mass.indoor_temperature_degC
            state = mass.step(dt, UA, ambient_degC=40.0, gain_W=40_000, mode=mode)
            delivered += state.cooling_delivered_W * dt * 3600.0
            # Exact integral of UA*(Ta - T) over the step, from the ODE itself:
            # C*dT = (UA*(Ta-T) + G - Q)dt  =>  integral(UA*(Ta-T))dt
            #      = C*(T_after - T_before) - (G - Q)*dt
            conduction += (
                mass.capacitance_J_K * (state.indoor_temperature_after_degC - before)
                - (40_000 - state.cooling_delivered_W) * dt * 3600.0
            )
        stored = mass.capacitance_J_K * (start - mass.indoor_temperature_degC)
        gains = conduction + 40_000 * dt * 3600.0 * len(modes)
        assert delivered - gains == pytest.approx(stored, rel=1e-9, abs=1.0)

    def test_stored_energy_sign_convention(self):
        mass = make_mass()
        state = mass.step(1.0, UA, 40.0, 40_000, mode="precool")
        # Cooling the zone stores 'coolth', reported as a positive change.
        assert state.stored_energy_change_kWh > 0
        assert state.indoor_temperature_after_degC < 24.0


class TestFlexibility:
    def test_precooling_headroom_is_positive_below_the_ceiling(self):
        flex = make_mass().flexibility_W(1.0, UA, ambient_degC=40.0, gain_W=40_000)
        assert flex["increase_W"] > 0
        assert flex["decrease_W"] > 0

    def test_energy_headroom_vanishes_at_the_band_floor(self):
        # Power headroom does not vanish at the floor: a zone pinned there
        # keeps drawing cooling to stay there. What vanishes is room to store
        # any more, which is why the energy figure is the one to bid on.
        mass = make_mass()
        mass.indoor_temperature_degC = mass.min_temperature_degC
        flex = mass.flexibility_W(1.0, UA, 40.0, 40_000)
        assert flex["precool_energy_kWh"] == pytest.approx(0.0, abs=1e-9)
        assert flex["coast_energy_kWh"] > 0

    def test_energy_headroom_vanishes_at_the_band_ceiling(self):
        mass = make_mass()
        mass.indoor_temperature_degC = mass.max_temperature_degC
        flex = mass.flexibility_W(1.0, UA, 40.0, 40_000)
        assert flex["coast_energy_kWh"] == pytest.approx(0.0, abs=1e-9)
        assert flex["precool_energy_kWh"] > 0

    def test_energy_headroom_matches_the_band_width_at_setpoint(self):
        mass = make_mass()
        flex = mass.flexibility_W(1.0, UA, 40.0, 40_000)
        assert flex["precool_energy_kWh"] == pytest.approx(400e6 * 3.0 / 3.6e6)
        assert flex["coast_energy_kWh"] == pytest.approx(400e6 * 2.0 / 3.6e6)

    def test_a_wider_band_offers_more_flexibility(self):
        narrow = ThermalMass(capacitance_J_K=400e6, setpoint_degC=24.0,
                             min_temperature_degC=23.5, max_temperature_degC=24.5)
        wide = ThermalMass(capacitance_J_K=400e6, setpoint_degC=24.0,
                           min_temperature_degC=21.0, max_temperature_degC=27.0)
        assert (wide.flexibility_W(1.0, UA, 40.0, 40_000)["increase_W"]
                > narrow.flexibility_W(1.0, UA, 40.0, 40_000)["increase_W"])

    def test_heavier_mass_offers_more_flexibility(self):
        light = make_mass(capacitance_J_K=100e6)
        heavy = make_mass(capacitance_J_K=800e6)
        assert (heavy.flexibility_W(1.0, UA, 40.0, 40_000)["increase_W"]
                > light.flexibility_W(1.0, UA, 40.0, 40_000)["increase_W"])


class TestBuildingLoadModels:
    def test_profile_model_passes_the_value_through(self):
        b = Building("b", Q_design=100_000.0)
        assert b.compute_demand(80_000.0)["demand_W"] == pytest.approx(80_000.0)

    def test_envelope_model_adds_weather_driven_gain(self):
        env = EnvelopeThermal(UA_W_K=5000, solar_aperture_m2=100, indoor_setpoint_degC=24.0)
        b = Building("b", Q_design=300_000.0, load_model="envelope", envelope=env)
        out = b.compute_demand(20_000.0, ambient_temperature_degC=40.0,
                               solar_irradiance_W_m2=800.0)
        assert out["conduction_W"] == pytest.approx(5000 * 16)
        assert out["solar_W"] == pytest.approx(80_000)
        assert out["demand_W"] == pytest.approx(20_000 + 80_000 + 80_000)

    def test_envelope_demand_responds_to_ambient(self):
        env = EnvelopeThermal(UA_W_K=5000, indoor_setpoint_degC=24.0)
        b = Building("b", Q_design=300_000.0, load_model="envelope", envelope=env)
        hot = b.compute_demand(20_000.0, ambient_temperature_degC=45.0)["demand_W"]
        mild = b.compute_demand(20_000.0, ambient_temperature_degC=30.0)["demand_W"]
        assert hot > mild

    def test_envelope_demand_is_clipped_at_zero(self):
        env = EnvelopeThermal(UA_W_K=5000, indoor_setpoint_degC=24.0)
        b = Building("b", Q_design=300_000.0, load_model="envelope", envelope=env)
        assert b.compute_demand(0.0, ambient_temperature_degC=5.0)["demand_W"] == 0.0

    def test_envelope_model_requires_an_ambient_temperature(self):
        env = EnvelopeThermal(UA_W_K=5000)
        b = Building("b", Q_design=1.0, load_model="envelope", envelope=env)
        with pytest.raises(ValueError, match="ambient temperature"):
            b.compute_demand(1000.0)

    def test_thermal_mass_model_shifts_demand_in_time(self):
        env = EnvelopeThermal(UA_W_K=UA, indoor_setpoint_degC=24.0)
        b = Building("b", Q_design=400_000.0, load_model="thermal_mass",
                     envelope=env, thermal_mass=make_mass())
        precooled = b.compute_demand(40_000.0, ambient_temperature_degC=38.0, dt_h=0.5,
                                     mode="precool")["demand_W"]
        b.thermal_mass.reset()
        tracked = b.compute_demand(40_000.0, ambient_temperature_degC=38.0, dt_h=0.5,
                                   mode="track")["demand_W"]
        assert precooled > tracked

    def test_missing_models_are_rejected_at_construction(self):
        with pytest.raises(ValueError, match="no envelope"):
            Building("b", Q_design=1.0, load_model="envelope")
        with pytest.raises(ValueError, match="no ThermalMass"):
            Building("b", Q_design=1.0, load_model="thermal_mass",
                     envelope=EnvelopeThermal(UA_W_K=100))
        with pytest.raises(ValueError, match="load_model"):
            Building("b", Q_design=1.0, load_model="nonsense")

    def test_negative_demand_is_refused(self):
        with pytest.raises(ValueError):
            Building("b", Q_design=1.0).set_demand(-1.0)
