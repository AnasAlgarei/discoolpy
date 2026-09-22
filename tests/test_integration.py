"""End-to-end checks: every shipped scenario must build, solve and balance.

These are the tests that would have caught the shipped repository's broken
scenario files. They are slower than the unit tests; run them with
``pytest -m "not slow"`` excluded if you only want the fast suite.
"""

import warnings

import pytest

from discoolpy import (
    build_system,
    load_yaml_config,
    make_weather_and_load_profiles,
    run_configured_case,
)
from discoolpy.config_schema import heat_gains_enabled, plant_control_mode

warnings.filterwarnings("ignore", category=FutureWarning)

CONFIG_NAMES = [
    "config_tutorial.yaml",
    "config_length_pipes.yaml",
    "config_length_derived_pr.yaml",
    "config_riyadh_heat_gains.yaml",
    "config_precooling_flexibility.yaml",
    "config_campus_five_buildings.yaml",
]


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_every_shipped_scenario_reaches_a_converged_design_point(name, tmp_config):
    cfg = tmp_config(name)
    system = build_system(cfg)
    system.network.solve(mode="design", max_iter=300)
    assert system.network.converged, f"{name} did not converge at the design point"


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_chilled_water_loop_balances_exactly_at_design(name, tmp_config):
    """Q_evap must equal building duty + pipe gain + pump heat + store exchange.

    The pre-upgrade tool had no such check, which let a 518 W pump-power
    inconsistency hide inside a pipe duty in `config_length_pipes.yaml`.
    """
    cfg = tmp_config(name)
    system = build_system(cfg)
    system.network.solve(mode="design", max_iter=300)
    assert system.network.converged

    q_evap = system.chiller.solved_Q_evap_W
    buildings = sum(b.component.Q.val for b in system.buildings)
    pipes = system.branch.heat_gain_report()["total_heat_gain_W"]
    pump = system.branch.pump.P.val if system.branch.pump is not None else 0.0
    residual = q_evap - buildings - pipes - pump
    assert abs(residual) < 1.0, f"{name}: {residual:.3f} W unaccounted for"


@pytest.mark.parametrize("name", CONFIG_NAMES)
def test_plant_control_mode_matches_whether_heat_gains_are_active(name, tmp_config):
    cfg = tmp_config(name)
    expected = "supply_temperature" if heat_gains_enabled(cfg) else "evaporator_duty"
    assert plant_control_mode(cfg) == expected


class TestHeatGainsChangeTheAnswer:
    """Heat gains have to change the answer, not only the API surface."""

    def test_pipes_add_load_the_plant_must_carry(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        assert system.network.converged
        buildings = sum(b.component.Q.val for b in system.buildings)
        gains = system.branch.heat_gain_report()
        assert gains["total_heat_gain_W"] > 0, "buried chilled mains must gain heat"
        assert system.chiller.solved_Q_evap_W > buildings
        # A 3 km buried network in Riyadh: a few per cent, not a rounding error
        # and not a catastrophe.
        share = gains["total_heat_gain_W"] / buildings
        assert 0.01 < share < 0.15

    def test_supply_mains_gain_more_than_return_mains(self, tmp_config):
        # Supply water is colder, so its driving temperature difference to the
        # soil is larger. Any model that got this backwards would be wrong.
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        gains = system.branch.heat_gain_report()
        assert gains["supply_heat_gain_W"] > gains["return_heat_gain_W"]

    def test_heat_gains_degrade_the_distribution_delta_t(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        design_delta = (
            cfg["design"]["building_return_temperature_degC"]
            - cfg["design"]["supply_temperature_degC"]
        )
        actual_delta = (
            system.branch.connections["branch_out"].T.val
            - system.branch.connections["branch_in"].T.val
        )
        assert actual_delta > design_delta

    def test_adiabatic_scenario_reports_no_pipe_gain(self, tmp_config):
        cfg = tmp_config("config_tutorial.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        assert abs(system.branch.heat_gain_report()["total_heat_gain_W"]) < 1e-6


class TestHeatGainReportIsReadBackFromTheSolver:
    """Every figure in the report has to come off the solved component.

    The point of handing UA and Tamb to TESPy rather than computing a gain
    alongside it is that the duty is then a solved quantity. The report has to
    be able to show that, which means reading UA, the log-mean difference and
    both terminal temperatures back from the component instead of recomputing
    them from the specification.
    """

    def test_per_pipe_detail_carries_the_solved_conductance(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        detail = system.branch.heat_gain_report()["per_pipe"]
        assert set(detail) == {"supply_1", "supply_2", "supply_3",
                               "return_1", "return_2", "return_3"}
        for key, values in detail.items():
            assert values["model"] == "ua"
            assert values["UA_W_K"] > 0
            assert values["lmtd_K"] > 0
            assert values["T_in_degC"] is not None
            assert values["T_out_degC"] is not None
            assert values["side"] == key.split("_")[0]

    def test_the_duty_is_the_conductance_times_the_driving_difference(self, tmp_config):
        """Q = UA * dT_log, as TESPy's own UA_group solved it."""
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        for values in system.branch.heat_gain_report()["per_pipe"].values():
            assert values["Q_W"] == pytest.approx(
                values["UA_W_K"] * values["lmtd_K"], rel=1e-6)

    def test_supply_water_warms_along_the_run(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        detail = system.branch.heat_gain_report()["per_pipe"]
        for key in ("supply_1", "supply_2", "supply_3"):
            assert detail[key]["T_out_degC"] > detail[key]["T_in_degC"]

    def test_network_conductance_is_the_sum_of_the_pipes(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        report = system.branch.heat_gain_report()
        assert report["network_UA_W_K"] == pytest.approx(
            sum(v["UA_W_K"] for v in report["per_pipe"].values()))

    def test_an_adiabatic_pipe_reports_its_model_not_a_zero_conductance(self, tmp_config):
        cfg = tmp_config("config_tutorial.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        report = system.branch.heat_gain_report()
        assert report["network_UA_W_K"] == 0.0
        assert all(v["model"] == "adiabatic" for v in report["per_pipe"].values())

    def test_a_forking_network_namespaces_its_descendants(self, tmp_config):
        cfg = tmp_config("config_branching_grid.yaml")
        system = build_system(cfg, strict_hydraulics=False)
        system.network.solve(mode="design", max_iter=300)
        detail = system.branch.heat_gain_report()["per_pipe"]
        assert any("/" in key for key in detail), "child branches must be namespaced"
        assert any("/" not in key for key in detail), "the root keeps bare keys"
        for key, values in detail.items():
            assert values["branch"] == (key.rpartition("/")[0] or system.branch.label)

    def test_the_backward_compatible_keys_still_agree(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        report = system.branch.heat_gain_report()
        assert report["per_pipe_W"] == {
            k: v["Q_W"] for k, v in report["per_pipe"].items()
        }


class TestModularity:
    def test_five_building_scenario_builds_and_scales(self, tmp_config):
        cfg = tmp_config("config_campus_five_buildings.yaml")
        system = build_system(cfg)
        assert len(system.buildings) == 5
        assert len(list(system.branch.pipe_items())) == 10

    def test_profile_generator_handles_any_building_count(self, tmp_config):
        # The pre-upgrade generator raised unless there were exactly three.
        for name, expected in [
            ("config_tutorial.yaml", 2),
            ("config_riyadh_heat_gains.yaml", 3),
            ("config_campus_five_buildings.yaml", 5),
        ]:
            cfg = tmp_config(name)
            cfg.setdefault("profiles", {})["periods"] = 8
            frame = make_weather_and_load_profiles(cfg)
            labels = [b["label"] for b in cfg["buildings"]]
            assert len(labels) == expected
            for label in labels:
                assert f"{label}_Q_W" in frame.columns
            assert (frame[f"{labels[0]}_Q_W"] > 0).all()

    def test_profile_carries_ground_and_solar(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["profiles"]["periods"] = 48
        frame = make_weather_and_load_profiles(cfg)
        assert frame["ground_temperature_degC"].std() < 0.2, "soil must be near-constant over a day"
        assert frame["ambient_temperature_degC"].std() > 3.0, "air must swing over a day"
        assert frame["solar_irradiance_W_m2"].max() > 500
        assert frame["solar_irradiance_W_m2"].min() == 0

    def test_mixed_placement_pipes_get_different_conductances(self, tmp_config):
        from discoolpy import make_pipe_thermal

        cfg = tmp_config("config_campus_five_buildings.yaml")
        thermal = make_pipe_thermal(cfg)
        buried = thermal["supply_2"].UA_per_m_W_mK
        surface = thermal["supply_3"].UA_per_m_W_mK
        assert surface > buried
        assert thermal["supply_3"].ambient_source == "air"
        assert thermal["supply_2"].ambient_source == "ground"


class TestRejections:
    def test_evaporator_duty_control_with_heat_gains_is_refused(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["design"]["plant_control"] = "evaporator_duty"
        with pytest.raises(ValueError, match="over-determines"):
            build_system(cfg)

    def test_over_determined_hydraulics_are_refused_before_tespy_sees_them(self, tmp_config):
        cfg = tmp_config("config_length_derived_pr.yaml")
        cfg["buildings"][0]["pr"] = 0.995   # reintroduce the original bug
        with pytest.raises(ValueError, match="Over-determined"):
            build_system(cfg)

    def test_auto_relax_repairs_it_instead(self, tmp_config):
        cfg = tmp_config("config_length_derived_pr.yaml")
        cfg["buildings"][0]["pr"] = 0.995
        cfg["branch"]["auto_relax_pressure"] = True
        system = build_system(cfg)
        assert any("Released" in note for note in system.notes)
        system.network.solve(mode="design", max_iter=300)
        assert system.network.converged

    def test_missing_pipe_energy_specification_is_refused(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        # Geometry (and therefore the pressure spec) stays; only the energy
        # equation goes away, since 'adiabatic' without an explicit Q leaves the
        # pipe duty free while the chiller duty is also free.
        cfg["branch"]["pipes"]["return_1"]["heat_model"] = "adiabatic"
        with pytest.raises(ValueError, match="under-specified"):
            build_system(cfg)


@pytest.mark.slow
class TestTimeSeries:
    def test_adiabatic_legacy_scenario_runs_offdesign(self, tmp_config):
        cfg = tmp_config("config_length_derived_pr.yaml")
        cfg["profiles"]["periods"] = 12
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "no_storage", use_storage=False, progress=False)
        assert len(results) == 12
        assert results["cop"].between(1.5, 8.0).all()

    def test_heat_gain_scenario_runs_offdesign_and_balances(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["profiles"]["periods"] = 12
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "no_storage", use_storage=False, progress=False)
        assert len(results) == 12
        assert results["chw_energy_residual_W"].abs().max() < 1.0
        assert (results["pipe_heat_gain_W"] > 0).all()
        assert (results["chiller_Q_evap_W"] > results["actual_building_total_Q_W"]).all()

    def test_hydraulic_storage_forces_colder_plant_water_when_charging(self, tmp_config):
        """The physics a supervisory model cannot represent."""
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["profiles"]["periods"] = 16
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "storage", use_storage=True, progress=False)
        setpoint = cfg["design"]["supply_temperature_degC"]
        charging = results[results["storage_charge_power_W"] > 1000]
        assert len(charging) > 0, "the scenario should charge at least once overnight"

        # The plant has to make water colder than the distribution setpoint by
        # exactly the temperature drop the store imposes, Q / (m * cp). Asserting
        # the relation rather than a threshold also catches a sign error.
        cp = cfg["design"]["cp_water_J_kgK"]
        expected_drop = charging["storage_charge_power_W"] / (charging["chw_total_m_kg_s"] * cp)
        actual_drop = setpoint - charging["chw_plant_supply_T_degC"]
        assert ((actual_drop - expected_drop).abs() < 0.02 + 0.03 * expected_drop).all()

        # And at full charging rate the depression is large enough to matter.
        hard = results[results["storage_charge_power_W"] > 100_000]
        assert len(hard) > 0
        assert (setpoint - hard["chw_plant_supply_T_degC"] > 0.5).all()

        # Discharging works the other way: the plant may run warmer.
        discharging = results[results["storage_discharge_power_W"] > 1000]
        if len(discharging):
            assert (discharging["chw_plant_supply_T_degC"] > setpoint).all()

        assert results["chw_energy_residual_W"].abs().max() < 1.0

    def test_storage_ambient_gain_tracks_the_weather(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["profiles"]["periods"] = 48
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "storage", use_storage=True, progress=False)
        correlation = results["storage_ambient_heat_gain_W"].corr(
            results["ambient_temperature_degC"]
        )
        assert correlation > 0.95, "a UA-based tank loss must follow ambient temperature"

    def test_building_thermal_mass_runs_and_respects_comfort(self, tmp_config):
        cfg = tmp_config("config_precooling_flexibility.yaml")
        cfg["profiles"]["periods"] = 12
        frame = make_weather_and_load_profiles(cfg)
        # thermal-mass buildings read the profile as internal gains only
        for item in cfg["buildings"]:
            frame[f"{item['label']}_Q_W"] *= 0.25
        results = run_configured_case(cfg, frame, "tracking", use_storage=False, progress=False)
        for item in cfg["buildings"]:
            safe = item["label"].replace(" ", "_")
            temps = results[f"{safe}_T_indoor_degC"]
            assert temps.between(
                item["thermal_mass"]["min_temperature_degC"] - 1e-6,
                item["thermal_mass"]["max_temperature_degC"] + 1e-6,
            ).all()
