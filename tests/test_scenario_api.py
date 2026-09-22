"""The short way in: scenario defaults, autofill, run_scenario, and the CLI.

Everything here is about how little a user has to write. The physics is
covered elsewhere; these tests pin the promise that a short scenario file
still describes a well-posed network, and that the one-call entry points
produce the same numbers as driving the pieces by hand.
"""

import warnings

import pytest
import yaml

from discoolpy import (
    DESIGN_DEFAULTS,
    autofill_branch_pressure,
    autofill_network_energy,
    build_system,
    check_scenario,
    default_pump_power_W,
    load_scenario,
    make_branch_specs,
    run_scenario,
)
from discoolpy.cli import main as cli_main
from discoolpy.config_schema import plant_control_mode

warnings.filterwarnings("ignore", category=FutureWarning)


MINIMAL = {
    "branch": {"autofill": True},
    "buildings": [
        {"label": "b1", "Q_design_W": 150_000.0},
        {"label": "b2", "Q_design_W": 150_000.0},
    ],
}


def scenario(tmp_path, extra=None, name="scenario.yaml"):
    """Write a config to disk so the path-based entry points can be exercised."""
    cfg = {k: (dict(v) if isinstance(v, dict) else list(v)) for k, v in MINIMAL.items()}
    if extra:
        for key, value in extra.items():
            if isinstance(value, dict) and isinstance(cfg.get(key), dict):
                cfg[key].update(value)
            else:
                cfg[key] = value
    cfg.setdefault("outputs", {})["output_dir"] = str(tmp_path / "out")
    path = tmp_path / name
    path.write_text(yaml.safe_dump(cfg), encoding="utf-8")
    return path


class TestDesignDefaults:
    def test_a_scenario_with_no_design_section_still_builds(self, tmp_path):
        system = build_system(load_scenario(scenario(tmp_path)))
        assert system.network is not None

    def test_defaults_are_only_a_fallback(self, tmp_path):
        cfg = load_scenario(scenario(tmp_path, {"design": {"supply_temperature_degC": 5.5}}))
        system = build_system(cfg)
        system.network.solve(mode="design", max_iter=300)
        assert system.branch.connections["branch_in"].T.val == pytest.approx(5.5, abs=1e-6)

    def test_every_default_is_a_number(self):
        assert all(isinstance(v, float) for v in DESIGN_DEFAULTS.values())


class TestPumpPower:
    def test_an_unstated_pump_power_is_sized_rather_than_zeroed(self, tmp_path):
        # Zero is not usable: TESPy divides by the enthalpy rise to get the
        # pump's isentropic efficiency, so a pump doing no work blows up.
        cfg = load_scenario(scenario(tmp_path))
        assert default_pump_power_W(cfg) > 0.0

    def test_a_stated_pump_power_is_used_verbatim(self, tmp_path):
        cfg = load_scenario(scenario(tmp_path, {"design": {"pump_power_W": 1234.0}}))
        assert default_pump_power_W(cfg) == pytest.approx(1234.0)

    def test_the_derived_power_follows_the_flow(self, tmp_path):
        small = load_scenario(scenario(tmp_path, name="a.yaml"))
        big = load_scenario(scenario(tmp_path, name="b.yaml", extra={
            "buildings": [
                {"label": "b1", "Q_design_W": 1_500_000.0},
                {"label": "b2", "Q_design_W": 1_500_000.0},
            ]
        }))
        assert default_pump_power_W(big) == pytest.approx(10 * default_pump_power_W(small), rel=1e-6)


class TestAutofill:
    def test_autofill_makes_a_bare_scenario_well_posed(self, tmp_path):
        result = check_scenario(scenario(tmp_path), verbose=False)
        assert result.ok

    def test_without_autofill_the_same_scenario_is_under_specified(self, tmp_path):
        result = check_scenario(
            scenario(tmp_path, {"branch": {"autofill": False}}), verbose=False
        )
        assert not result.ok
        assert "Under-determined" in "\n".join(result.lines)

    def test_autofill_leaves_a_scenario_that_states_its_own_pressure_alone(self):
        cfg = {
            "branch": {"pipes": {"supply_2": {"pr": 0.99}}},
            "buildings": MINIMAL["buildings"],
        }
        root = make_branch_specs(cfg)
        assert autofill_branch_pressure(root) is False
        assert root.pipes["supply_2"]["pr"] == 0.99

    def test_autofill_declines_to_guess_at_a_fork(self):
        cfg = {
            "branch": {
                "buildings": ["b1"],
                "terminals": [
                    {"type": "bypass", "label": "north"},
                    {"type": "branch", "label": "east", "buildings": ["b2"]},
                ],
            },
            "buildings": MINIMAL["buildings"],
        }
        root = make_branch_specs(cfg)
        # A fork puts its terminals in parallel, and which path carries the
        # free element is a modelling decision, not an arithmetic one.
        assert autofill_branch_pressure(root) is False

    def test_energy_autofill_leaves_one_slack_pipe_under_asserted_duty(self):
        cfg = {"branch": {}, "buildings": MINIMAL["buildings"]}
        root = make_branch_specs(cfg)
        assert plant_control_mode(cfg) == "evaporator_duty"
        filled = autofill_network_energy([root], "evaporator_duty")
        assert set(filled) == {"supply_1", "supply_2", "return_1"}
        assert "Q" not in (root.pipes.get("return_2") or {})

    def test_energy_autofill_specifies_every_pipe_under_a_free_duty(self):
        cfg = {"branch": {}, "buildings": MINIMAL["buildings"]}
        root = make_branch_specs(cfg)
        filled = autofill_network_energy([root], "supply_temperature")
        assert set(filled) == set(root.pipe_keys)

    def test_energy_autofill_does_not_touch_a_pipe_with_a_heat_model(self):
        cfg = {
            "branch": {"heat_model": "ua", "thermal_defaults": {"insulation_thickness_m": 0.05},
                       "pipes": {k: {"L": 100.0, "D": 0.15} for k in
                                 ("supply_1", "supply_2", "return_1", "return_2")}},
            "buildings": MINIMAL["buildings"],
        }
        root = make_branch_specs(cfg)
        assert autofill_network_energy([root], "supply_temperature") == []


class TestRunScenario:
    def test_a_single_run_writes_its_results(self, tmp_path):
        result = run_scenario(
            scenario(tmp_path, {"profiles": {"periods": 4, "freq": "1h"}}),
            progress=False,
        )
        assert len(result.results) == 4
        assert result.report is None
        assert not result.paired
        assert result.files["results"].exists()
        assert result.files["profile"].exists()
        assert "peak compressor power" in result.summary()

    def test_periods_overrides_the_scenario(self, tmp_path):
        result = run_scenario(
            scenario(tmp_path, {"profiles": {"periods": 48, "freq": "1h"}}),
            periods=3,
            progress=False,
        )
        assert len(result.results) == 3

    def test_a_stored_scenario_pairs_itself_by_default(self, tmp_config):
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["profiles"]["periods"] = 4
        result = run_scenario(cfg, progress=False, plot=False)
        assert result.paired
        assert result.report is not None
        # The store moves duty in time; it does not create cooling.
        assert result.cases["with_storage"]["compressor_power_W"].max() > 0

    def test_comparing_a_scenario_with_no_store_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="no storage enabled"):
            run_scenario(scenario(tmp_path), compare_storage=True, progress=False)

    def test_run_scenario_matches_the_hand_driven_pipeline(self, tmp_path):
        from discoolpy import (
            make_weather_and_load_profiles,
            run_configured_case,
        )

        path = scenario(tmp_path, {"profiles": {"periods": 4, "freq": "1h"}})
        by_hand = run_configured_case(
            load_scenario(path),
            make_weather_and_load_profiles(load_scenario(path)),
            "base",
            use_storage=False,
            progress=False,
        )
        by_helper = run_scenario(path, progress=False).results
        assert by_helper["compressor_power_W"].tolist() == pytest.approx(
            by_hand["compressor_power_W"].tolist()
        )


class TestCheckScenario:
    def test_the_report_can_be_collected_without_printing(self, tmp_path, capsys):
        result = check_scenario(scenario(tmp_path), verbose=False)
        assert capsys.readouterr().out == ""
        assert "Plant energy balance" in str(result)

    def test_a_check_result_is_truthy_when_the_scenario_is_usable(self, tmp_path):
        assert check_scenario(scenario(tmp_path), verbose=False)

    def test_the_layout_is_drawn_on_request(self, tmp_path):
        target = tmp_path / "layout.png"
        result = check_scenario(scenario(tmp_path), layout_path=target, verbose=False)
        assert result.layout_path == target
        assert target.exists()

    @pytest.mark.slow
    @pytest.mark.parametrize("name", [
        "config_tutorial.yaml",
        "config_riyadh_heat_gains.yaml",
        "config_branching_grid.yaml",
        "config_campus_five_buildings.yaml",
    ])
    def test_every_shipped_scenario_passes_its_own_check(self, name, config_dir, capsys):
        assert check_scenario(config_dir / name, verbose=False).ok


class TestCli:
    def test_new_writes_a_template_that_runs(self, tmp_path, capsys):
        target = tmp_path / "fresh.yaml"
        assert cli_main(["new", str(target), "--minimal"]) == 0
        assert target.exists()
        capsys.readouterr()
        assert check_scenario(target, verbose=False).ok

    def test_new_refuses_to_clobber_without_force(self, tmp_path, capsys):
        target = tmp_path / "fresh.yaml"
        cli_main(["new", str(target)])
        assert cli_main(["new", str(target)]) == 1
        assert cli_main(["new", str(target), "--force"]) == 0

    def test_the_full_template_is_valid_yaml_and_well_posed(self, tmp_path, capsys):
        target = tmp_path / "full.yaml"
        cli_main(["new", str(target)])
        capsys.readouterr()
        cfg = yaml.safe_load(target.read_text(encoding="utf-8"))
        assert cfg["metadata"]["name"] == "my_scenario"
        cfg["outputs"]["output_dir"] = str(tmp_path / "out")
        assert check_scenario(cfg, verbose=False).ok

    def test_check_returns_zero_on_a_good_scenario(self, tmp_path, capsys):
        assert cli_main(["check", str(scenario(tmp_path))]) == 0

    def test_check_returns_nonzero_on_a_broken_one(self, tmp_path, capsys):
        path = scenario(tmp_path, {"branch": {"autofill": False}})
        assert cli_main(["check", str(path)]) == 1

    def test_plot_writes_a_figure_without_solving(self, tmp_path, capsys):
        target = tmp_path / "plan.png"
        assert cli_main(["plot", str(scenario(tmp_path)), "-o", str(target)]) == 0
        assert target.exists()

    def test_run_writes_results(self, tmp_path, capsys):
        path = scenario(tmp_path, {"profiles": {"periods": 3, "freq": "1h"}})
        assert cli_main(["run", str(path), "--quiet", "-n", "2"]) == 0
        assert (tmp_path / "out" / "results.csv").exists()

    def test_list_names_the_shipped_scenarios(self, capsys):
        assert cli_main(["list"]) == 0
        assert "config_tutorial.yaml" in capsys.readouterr().out

    def test_a_missing_scenario_is_reported_not_traced(self, tmp_path):
        with pytest.raises(SystemExit, match="not found"):
            cli_main(["check", str(tmp_path / "nope.yaml")])


class TestShippedBlankExample:
    """The fill-in example in examples/ has to stay runnable."""

    @pytest.fixture
    def blank(self, request):
        path = request.config.rootpath / "examples" / "blank_scenario.yaml"
        if not path.exists():
            pytest.skip("examples/ is not part of this install")
        return path

    def test_it_is_well_posed_as_shipped(self, blank, tmp_path):
        cfg = load_scenario(blank)
        cfg["outputs"]["output_dir"] = str(tmp_path / "out")
        assert check_scenario(cfg, verbose=False).ok

    def test_it_runs_a_short_time_series(self, blank, tmp_path):
        cfg = load_scenario(blank)
        cfg["outputs"]["output_dir"] = str(tmp_path / "out")
        result = run_scenario(cfg, periods=3, progress=False)
        assert len(result.results) == 3


class TestTheFourStepFlow:
    """Define, check, run, report: each step has to hand off to the next.

    The steps are only a workflow if the artefacts line up: the check has to
    read what `new` wrote, the run has to keep what the report needs, and the
    report has to work without solving anything a second time.
    """

    def test_check_names_the_run_command(self, tmp_path):
        result = check_scenario(scenario(tmp_path), verbose=False)
        assert result.ok
        assert any("discoolpy run" in line for line in result.lines)
        assert any("discoolpy report" in line for line in result.lines)

    def test_check_suggests_a_short_run_for_a_long_scenario(self, tmp_path):
        path = scenario(tmp_path, {"profiles": {"periods": 336, "freq": "30min"}})
        lines = check_scenario(path, verbose=False).lines
        assert any("-n 48" in line for line in lines)

    def test_a_failed_check_says_so_rather_than_naming_the_next_step(self, tmp_path):
        path = scenario(tmp_path, {"design": {"pump_power_W": 1.0}})
        result = check_scenario(path, verbose=False)
        if not result.ok:
            assert any("not usable yet" in line for line in result.lines)
            assert not any("Next:" in line for line in result.lines)

    def test_the_run_keeps_the_system_that_produced_it(self, tmp_path):
        result = run_scenario(scenario(tmp_path), periods=3, progress=False)
        assert result.system is not None
        assert result.system.branch is not None
        # The same object the frame came from, not a rebuild.
        assert result.system.network.converged

    def test_the_result_reports_itself_without_a_second_solve(self, tmp_path, capsys):
        result = run_scenario(scenario(tmp_path), periods=3, progress=False)
        report = result.print_report()
        assert "Plant energy balance" in capsys.readouterr().out
        assert report.sections

    def test_the_result_plots_itself_next_to_its_other_output(self, tmp_path):
        result = run_scenario(scenario(tmp_path), periods=3, progress=False)
        paths = result.plot_all()
        assert paths["system"].parent.name == "figures"
        assert paths["system"].parent.parent == result.output_dir
        assert all(p.exists() for p in paths.values())
        # A system was available, so the plan view is in there too.
        assert "layout" in paths

    def test_run_can_do_the_report_step_itself(self, tmp_path):
        result = run_scenario(scenario(tmp_path), periods=3, progress=False,
                              report=True)
        assert any(k.startswith("figure_") for k in result.files)

    def test_a_structural_warning_reaches_the_caller(self, tmp_path):
        path = scenario(tmp_path, {"scratchpad": {"note": "hello"}})
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            result = run_scenario(path, periods=3, progress=False)
        assert result.warnings
        assert any("scratchpad" in str(w.message) for w in caught)


class TestScenarioErrorsAreMessagesNotTracebacks:
    def test_a_misspelled_section_fails_the_check_cleanly(self, tmp_path):
        path = tmp_path / "typo.yaml"
        path.write_text(
            yaml.safe_dump({"branch": {"autofill": True},
                            "buidlings": [{"label": "b1", "Q_design_W": 1e5}]}),
            encoding="utf-8",
        )
        result = check_scenario(path, verbose=False)
        assert not result.ok
        assert result.error is not None
        assert "did you mean 'buildings'" in str(result.error)

    def test_the_cli_exits_one_without_a_traceback(self, tmp_path, capsys):
        path = tmp_path / "typo.yaml"
        path.write_text(
            yaml.safe_dump({"branch": {"autofill": True},
                            "buidlings": [{"label": "b1", "Q_design_W": 1e5}]}),
            encoding="utf-8",
        )
        assert cli_main(["check", str(path)]) == 1
        captured = capsys.readouterr()
        assert "Traceback" not in captured.out + captured.err
        assert "did you mean" in captured.out + captured.err

    def test_strict_checking_fails_on_a_warning(self, tmp_path):
        path = scenario(tmp_path, {"scratchpad": {"note": "hello"}})
        assert check_scenario(path, verbose=False).ok
        assert not check_scenario(path, verbose=False, strict=True).ok
