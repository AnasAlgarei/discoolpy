"""Heat-transfer physics: closed-form checks that do not need a solver."""

import math

import pytest

from discoolpy.thermal import (
    EnvelopeThermal,
    PipeThermal,
    StorageThermal,
    buried_pipe_UA_per_m,
    external_film_coefficient,
    lmtd_to_ambient,
    soil_temperature_degC,
    surface_pipe_UA_per_m,
    tank_UA_W_K,
    tank_surface_area_m2,
    twin_pipe_correction,
)


class TestLMTD:
    def test_sign_is_positive_when_ambient_is_warmer(self):
        # The whole point of the sign convention: a chilled line in warm ground
        # must produce a positive (gain) heat flow.
        assert lmtd_to_ambient(T_in=6.0, T_out=7.0, T_ambient=30.0) > 0

    def test_sign_is_negative_when_ambient_is_colder(self):
        assert lmtd_to_ambient(T_in=60.0, T_out=55.0, T_ambient=10.0) < 0

    def test_reduces_to_arithmetic_mean_for_equal_terminals(self):
        assert lmtd_to_ambient(7.0, 7.0, 30.0) == pytest.approx(23.0)

    def test_lies_between_the_two_terminal_differences(self):
        value = lmtd_to_ambient(6.0, 10.0, 30.0)
        assert 20.0 < value < 24.0

    def test_finite_when_terminals_straddle_ambient(self):
        # Would divide by log of a negative number without the guard.
        assert math.isfinite(lmtd_to_ambient(25.0, 35.0, 30.0))


class TestBuriedPipe:
    def test_thicker_insulation_lowers_conductance(self):
        thin = buried_pipe_UA_per_m(0.3, 0.02)
        thick = buried_pipe_UA_per_m(0.3, 0.08)
        assert thick < thin

    def test_no_insulation_is_dominated_by_soil(self):
        bare = buried_pipe_UA_per_m(0.3, 0.0, burial_depth_m=1.2, ground="moist soil")
        # 2*pi*k / arccosh(2z/D), with the small internal-film term.
        expected = 2 * math.pi * 2.2 / math.acosh(2 * 1.2 / 0.3)
        assert bare == pytest.approx(expected, rel=0.02)

    def test_deeper_burial_lowers_conductance(self):
        assert buried_pipe_UA_per_m(0.3, 0.04, burial_depth_m=2.5) < buried_pipe_UA_per_m(
            0.3, 0.04, burial_depth_m=0.8
        )

    def test_conductive_ground_raises_conductance(self):
        assert buried_pipe_UA_per_m(0.3, 0.04, ground="dry soil") < buried_pipe_UA_per_m(
            0.3, 0.04, ground="moist soil"
        )

    def test_named_and_numeric_insulation_agree(self):
        assert buried_pipe_UA_per_m(0.3, 0.05, "pur") == pytest.approx(
            buried_pipe_UA_per_m(0.3, 0.05, 0.027)
        )

    def test_typical_value_is_in_the_engineering_range(self):
        # A DN300 PUR-50 buried main should land near 0.4-0.7 W/(m.K).
        ua = buried_pipe_UA_per_m(0.30, 0.05, "pur", 1.2, "moist soil", pipe_wall_thickness_m=0.006)
        assert 0.35 < ua < 0.75

    def test_twin_trench_reduces_net_conductance(self):
        alone = buried_pipe_UA_per_m(0.3, 0.05)
        paired = buried_pipe_UA_per_m(0.3, 0.05, twin_spacing_m=0.7)
        assert paired < alone

    def test_rejects_bad_geometry(self):
        with pytest.raises(ValueError):
            buried_pipe_UA_per_m(0.0, 0.05)
        with pytest.raises(ValueError):
            buried_pipe_UA_per_m(0.3, 0.05, insulation_conductivity="unobtainium")


class TestSurfacePipe:
    def test_exposed_pipe_gains_more_than_buried_equivalent(self):
        # Same pipe, same insulation: air offers far less resistance than soil,
        # which is why mixing placements in one network changes the answer.
        buried = buried_pipe_UA_per_m(0.3, 0.05, "pur", 1.2, "moist soil")
        surface = surface_pipe_UA_per_m(0.3, 0.05, "pur", wind_velocity_m_s=1.0)
        assert surface > buried

    def test_wind_raises_conductance(self):
        assert surface_pipe_UA_per_m(0.3, 0.02, wind_velocity_m_s=8.0) > surface_pipe_UA_per_m(
            0.3, 0.02, wind_velocity_m_s=0.2
        )

    def test_film_coefficient_has_radiation_component(self):
        with_rad = external_film_coefficient(1.0, include_radiation=True)
        without = external_film_coefficient(1.0, include_radiation=False)
        assert with_rad > without
        assert 4.0 < with_rad - without < 8.0  # linearised sigma*T^3 at room temperature


class TestTank:
    def test_area_matches_cylinder_geometry(self):
        area = tank_surface_area_m2(volume_m3=1000.0, height_to_diameter=1.0)
        diameter = (4 * 1000.0 / math.pi) ** (1 / 3)
        expected = math.pi * diameter**2 + 2 * math.pi * diameter**2 / 4
        assert area == pytest.approx(expected)

    def test_bigger_tank_has_more_area_but_less_area_per_m3(self):
        small = tank_surface_area_m2(100.0)
        big = tank_surface_area_m2(1000.0)
        assert big > small
        assert big / 1000.0 < small / 100.0  # surface-to-volume falls with scale

    def test_insulation_dominates_a_well_built_tank(self):
        ua = tank_UA_W_K(400.0, 0.10, "pur")
        area = tank_surface_area_m2(400.0)
        assert ua == pytest.approx(area / (1 / 300 + 0.10 / 0.027 + 1 / 12), rel=1e-6)

    def test_rejects_nonpositive_volume(self):
        with pytest.raises(ValueError):
            tank_UA_W_K(0.0, 0.1)


class TestSoilTemperature:
    def test_damping_increases_with_depth(self):
        surface_swing = max(soil_temperature_degC(d, 25, 12, 0.05) for d in range(1, 366)) - min(
            soil_temperature_degC(d, 25, 12, 0.05) for d in range(1, 366)
        )
        deep_swing = max(soil_temperature_degC(d, 25, 12, 6.0) for d in range(1, 366)) - min(
            soil_temperature_degC(d, 25, 12, 6.0) for d in range(1, 366)
        )
        assert deep_swing < 0.35 * surface_swing

    def test_annual_mean_is_preserved(self):
        values = [soil_temperature_degC(d, 25.0, 12.0, 1.5) for d in range(1, 366)]
        assert sum(values) / len(values) == pytest.approx(25.0, abs=0.15)

    def test_summer_is_warmer_than_winter_in_the_northern_hemisphere(self):
        july = soil_temperature_degC(200, 25, 12, 1.2)
        january = soil_temperature_degC(15, 25, 12, 1.2)
        assert july > january


class TestEnvelope:
    def test_components_sum_to_total(self):
        env = EnvelopeThermal(UA_W_K=5000, solar_aperture_m2=100, infiltration_kg_s=2.0,
                              indoor_setpoint_degC=24.0, internal_gain_W=20000)
        parts = env.gain_W(ambient_temperature_degC=40.0, solar_irradiance_W_m2=800.0)
        assert parts["total_gain_W"] == pytest.approx(
            parts["conduction_W"] + parts["infiltration_W"] + parts["solar_W"] + parts["internal_W"]
        )

    def test_conduction_follows_the_driving_temperature_difference(self):
        env = EnvelopeThermal(UA_W_K=5000, indoor_setpoint_degC=24.0)
        assert env.gain_W(40.0)["conduction_W"] == pytest.approx(5000 * 16)
        assert env.gain_W(24.0)["conduction_W"] == pytest.approx(0.0)
        assert env.gain_W(18.0)["conduction_W"] < 0  # free cooling at night

    def test_solar_scales_with_aperture(self):
        env = EnvelopeThermal(solar_aperture_m2=50.0)
        assert env.gain_W(24.0, solar_irradiance_W_m2=900.0)["solar_W"] == pytest.approx(45000.0)


class TestStorageThermal:
    def test_gain_grows_with_ambient(self):
        st = StorageThermal(UA_W_K=80.0, storage_temperature_degC=0.0)
        assert st.heat_gain_W(45.0, 0.5) == pytest.approx(80.0 * 45.0)
        assert st.heat_gain_W(15.0, 0.5) == pytest.approx(80.0 * 15.0)

    def test_hot_afternoon_costs_roughly_three_times_a_cool_night(self):
        # The specific failing of a constant standby-loss fraction.
        st = StorageThermal(UA_W_K=80.0, storage_temperature_degC=0.0)
        assert st.heat_gain_W(45.0, 0.5) / st.heat_gain_W(15.0, 0.5) == pytest.approx(3.0)

    def test_ice_temperature_is_independent_of_soc(self):
        st = StorageThermal(UA_W_K=80.0, storage_temperature_degC=0.0)
        assert st.medium_temperature_degC(0.1) == st.medium_temperature_degC(0.9)

    def test_sensible_store_warms_as_it_discharges(self):
        st = StorageThermal(
            UA_W_K=200.0, storage_temperature_degC=5.0,
            discharged_temperature_degC=13.0, temperature_varies_with_soc=True,
        )
        assert st.medium_temperature_degC(1.0) == pytest.approx(5.0)
        assert st.medium_temperature_degC(0.0) == pytest.approx(13.0)
        assert st.medium_temperature_degC(0.5) == pytest.approx(9.0)
        # ...and therefore gains less heat when nearly empty.
        assert st.heat_gain_W(40.0, 0.0) < st.heat_gain_W(40.0, 1.0)

    def test_no_ua_means_no_gain(self):
        assert StorageThermal(UA_W_K=0.0).heat_gain_W(45.0, 0.5) == 0.0

    def test_missing_ambient_means_no_gain(self):
        assert StorageThermal(UA_W_K=80.0).heat_gain_W(None, 0.5) == 0.0


class TestPipeThermal:
    def test_adiabatic_reports_nothing(self):
        assert PipeThermal(model="adiabatic").heat_gain_W(7.0, 8.0, 30.0) == 0.0

    def test_fixed_reports_its_constant(self):
        assert PipeThermal(model="fixed", Q_W=1234.0).heat_gain_W(7.0, 8.0, 30.0) == 1234.0

    def test_ua_resolves_from_length_and_per_metre_value(self):
        spec = PipeThermal(model="ua", UA_per_m_W_mK=0.5, length_m=800.0)
        assert spec.resolved_UA() == pytest.approx(400.0)

    def test_explicit_ua_wins_over_geometry(self):
        spec = PipeThermal(model="ua", UA_W_K=99.0, UA_per_m_W_mK=0.5, length_m=800.0)
        assert spec.resolved_UA() == 99.0

    def test_ua_gain_matches_ua_times_lmtd(self):
        spec = PipeThermal(model="ua", UA_W_K=400.0, ambient_temperature_degC=31.0)
        assert spec.heat_gain_W(7.0, 8.0) == pytest.approx(400.0 * lmtd_to_ambient(7.0, 8.0, 31.0))


class TestNativeVocabularyGuards:
    """Configuration-time checks on what TESPy's own pipe groups will accept.

    Each of these otherwise fails from inside the solver's residual evaluation,
    where the message names neither the pipe nor the offending value.
    """

    def _config(self, **pipe):
        base = {
            "design": {"ground_temperature_degC": 29.4, "ambient_temperature_degC": 43.0},
            "branch": {
                "heat_model": "tespy_buried",
                "thermal_defaults": {"insulation": "pur", "insulation_thickness_m": 0.05,
                                     "burial_depth_m": 1.2, "ground": "moist soil",
                                     "pipe_wall_thickness_m": 0.006},
                "pipes": {"supply_1": {"L": 800.0, "D": 0.2, **pipe}},
            },
        }
        return base

    def test_accepts_a_material_tespy_knows(self):
        from discoolpy.config_schema import make_pipe_thermal

        specs = make_pipe_thermal(self._config())
        assert specs["supply_1"].native_attrs["environment_media"] == "moist soil"

    def test_names_the_ground_tespy_does_not_know(self):
        from discoolpy.config_schema import make_pipe_thermal

        config = self._config()
        config["branch"]["thermal_defaults"]["ground"] = "sand"
        with pytest.raises(ValueError, match="buried-pipe group knows only"):
            make_pipe_thermal(config)

    def test_points_at_the_ua_model_for_a_ground_it_does_know(self):
        from discoolpy.config_schema import make_pipe_thermal

        config = self._config()
        config["branch"]["thermal_defaults"]["ground"] = "clay"
        with pytest.raises(ValueError, match="heat_model='ua' will model this ground"):
            make_pipe_thermal(config)

    def test_refuses_a_bare_pipe_on_a_native_group(self):
        from discoolpy.config_schema import make_pipe_thermal

        config = self._config()
        config["branch"]["thermal_defaults"]["insulation_thickness_m"] = 0.0
        with pytest.raises(ValueError, match="insulation thickness below"):
            make_pipe_thermal(config)

    def test_refuses_still_air_on_the_surface_group(self):
        from discoolpy.config_schema import make_pipe_thermal

        config = self._config()
        config["branch"]["heat_model"] = "tespy_surface"
        config["branch"]["thermal_defaults"]["wind_velocity_m_s"] = 0.0
        with pytest.raises(ValueError, match="cannot take wind_velocity"):
            make_pipe_thermal(config)

    def test_requires_geometry_for_a_native_group(self):
        from discoolpy.config_schema import make_pipe_thermal

        config = self._config()
        del config["branch"]["pipes"]["supply_1"]["D"]
        with pytest.raises(ValueError, match="needs the real geometry"):
            make_pipe_thermal(config)

    def test_the_closed_form_takes_every_ground_in_the_table(self):
        from discoolpy.thermal import GROUND_CONDUCTIVITY_W_mK

        for ground in GROUND_CONDUCTIVITY_W_mK:
            assert buried_pipe_UA_per_m(0.2, 0.05, "pur", 1.2, ground) > 0
