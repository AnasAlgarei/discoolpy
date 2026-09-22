"""Scenario validation: catching a typo while it is still a typo.

YAML has no schema, so a misspelled key is not an error, it is a key nobody
reads. These tests pin the two halves of the rule: a key close to a real one
stops the run, because the scenario it produces is not the one that was
written; a key close to nothing is a warning, because scenarios carry notes and
private annotations and refusing those would make the check a nuisance.
"""

from __future__ import annotations

import pytest

from discoolpy import ScenarioError, load_scenario, validate_scenario
from discoolpy.config_schema import building_design_load_W


def minimal(**overrides):
    config = {
        "branch": {"autofill": True},
        "buildings": [{"label": "b1", "Q_design_W": 150000.0}],
    }
    config.update(overrides)
    return config


class TestTypos:
    def test_a_near_miss_top_level_section_is_refused(self):
        with pytest.raises(ScenarioError) as excinfo:
            validate_scenario({"buidlings": [], "branch": {}})
        assert "did you mean 'buildings'" in str(excinfo.value)

    def test_a_near_miss_inside_a_section_is_refused(self):
        config = minimal()
        config["design"] = {"supply_temperature_degc": 7.0}
        with pytest.raises(ScenarioError, match="supply_temperature_degC"):
            validate_scenario(config)

    def test_a_near_miss_inside_a_building_is_refused(self):
        config = minimal(buildings=[{"label": "b1", "Q_desgin_W": 150000.0}])
        with pytest.raises(ScenarioError, match="Q_design_W"):
            validate_scenario(config)

    def test_a_near_miss_inside_a_pipe_is_refused(self):
        config = minimal()
        config["branch"] = {"pipes": {"supply_1": {"L": 400.0, "DD": 0.2}}}
        with pytest.raises(ScenarioError, match="did you mean 'D'"):
            validate_scenario(config)

    def test_a_near_miss_in_thermal_defaults_is_refused(self):
        config = minimal()
        config["branch"] = {"thermal_defaults": {"insulation_thicknes_m": 0.05}}
        with pytest.raises(ScenarioError, match="insulation_thickness_m"):
            validate_scenario(config)

    def test_the_error_names_the_offending_path(self):
        config = minimal(buildings=[{"label": "b1", "Q_design_W": 1e5},
                                    {"label": "b2", "Q_desgin_W": 1e5}])
        with pytest.raises(ScenarioError) as excinfo:
            validate_scenario(config)
        assert "buildings[1]" in str(excinfo.value)


class TestUnknownButNotATypo:
    def test_a_key_close_to_nothing_is_only_a_warning(self):
        config = minimal()
        config["design"] = {"my_own_annotation": 1}
        warnings = validate_scenario(config)
        assert len(warnings) == 1
        assert "my_own_annotation" in warnings[0]

    def test_an_unknown_top_level_section_is_only_a_warning(self):
        warnings = validate_scenario(minimal(scratchpad={"note": "hello"}))
        assert any("scratchpad" in w for w in warnings)

    def test_strict_turns_warnings_into_an_error(self):
        config = minimal(scratchpad={"note": "hello"})
        with pytest.raises(ScenarioError, match="strict checking is on"):
            validate_scenario(config, strict=True)


class TestShape:
    def test_buildings_must_be_a_list(self):
        with pytest.raises(ScenarioError, match="expected a list"):
            validate_scenario({"buildings": {"label": "b1"}, "branch": {}})

    def test_a_section_must_be_a_mapping(self):
        with pytest.raises(ScenarioError, match="expected a mapping"):
            validate_scenario(minimal(design=7.0))

    def test_a_building_must_be_a_mapping(self):
        with pytest.raises(ScenarioError, match="expected a mapping"):
            validate_scenario(minimal(buildings=["building_1"]))

    def test_a_scalar_tariff_is_allowed(self):
        """A flat price is a number; an hourly one is a mapping."""
        assert validate_scenario(minimal(economics={"tariff": 0.11})) == []
        assert validate_scenario(minimal(economics={"tariff": {0: 0.05}})) == []


class TestRequirements:
    def test_a_scenario_needs_buildings(self):
        with pytest.raises(ScenarioError, match="no buildings"):
            validate_scenario({"branch": {"autofill": True}})

    def test_a_building_needs_a_label(self):
        with pytest.raises(ScenarioError, match="no label"):
            validate_scenario(minimal(buildings=[{"Q_design_W": 1e5}]))

    def test_labels_must_be_unique(self):
        config = minimal(buildings=[{"label": "b", "Q_design_W": 1e5},
                                    {"label": "b", "Q_design_W": 1e5}])
        with pytest.raises(ScenarioError, match="duplicate label"):
            validate_scenario(config)

    def test_a_building_needs_a_load(self):
        with pytest.raises(ScenarioError, match="no design load"):
            validate_scenario(minimal(buildings=[{"label": "b1"}]))

    def test_a_negative_load_is_refused(self):
        config = minimal(buildings=[{"label": "b1", "Q_design_W": -1.0}])
        with pytest.raises(ScenarioError, match="must be positive"):
            validate_scenario(config)


class TestDesignLoadUnits:
    def test_kilowatts_are_accepted(self):
        assert building_design_load_W({"Q_design_kW": 150}) == 150_000.0

    def test_watts_are_accepted(self):
        assert building_design_load_W({"Q_design_W": 150_000.0}) == 150_000.0

    def test_both_at_once_is_refused(self):
        config = minimal(buildings=[
            {"label": "b1", "Q_design_W": 1e5, "Q_design_kW": 100}])
        with pytest.raises(ScenarioError, match="not both"):
            validate_scenario(config)

    def test_a_kilowatt_number_in_the_watt_key_is_warned_about(self):
        """150 W is not a district substation; 150 kW is."""
        config = minimal(buildings=[{"label": "b1", "Q_design_W": 150.0}])
        warnings = validate_scenario(config)
        assert any("Q_design_kW" in w for w in warnings)

    def test_the_two_units_build_the_same_network(self, tmp_path):
        from discoolpy import build_system

        watts = build_system(minimal(
            buildings=[{"label": "b1", "Q_design_W": 150000.0}]))
        kilowatts = build_system(minimal(
            buildings=[{"label": "b1", "Q_design_kW": 150.0}]))
        assert watts.buildings[0].Q_design == kilowatts.buildings[0].Q_design


class TestShippedScenariosValidate:
    """The key map is only useful if it stays true, so pin it to the configs.

    Every shipped scenario and template must validate with no warnings at all.
    A real key added to the code and forgotten here shows up as a warning on
    the scenario that first uses it, and turns this red.
    """

    def test_every_shipped_scenario_is_clean(self, all_configs):
        dirty = {}
        for path in all_configs:
            warnings = validate_scenario(load_scenario(path))
            if warnings:
                dirty[path.name] = warnings
        assert dirty == {}

    def test_every_template_is_clean(self):
        from pathlib import Path

        import discoolpy

        templates = Path(discoolpy.__file__).parent / "templates"
        paths = sorted(templates.glob("*.yaml"))
        assert paths, "the templates should ship with the package"
        for path in paths:
            assert validate_scenario(load_scenario(path)) == [], path.name


class TestLoadScenario:
    def test_warnings_ride_along_on_the_config(self):
        config = load_scenario(minimal(scratchpad={"note": "hello"}))
        assert any("scratchpad" in w for w in config["_warnings"])

    def test_validation_can_be_turned_off(self):
        config = load_scenario({"buidlings": []}, validate=False)
        assert "_warnings" not in config

    def test_the_internal_keys_do_not_trip_the_check(self):
        """Loading twice must not warn about the keys the first load added."""
        once = load_scenario(minimal())
        assert validate_scenario(once) == []
