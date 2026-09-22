"""Structural checks on the branch pressure and energy specification.

The cases named "regression" below are the exact specifications that shipped in
the original repository and that TESPy rejected with an unreadable dump.
"""

import pytest

from discoolpy.hydraulics import (
    analyse_pressure_topology,
    resolve_pressure_specification,
    suggest_pressure_specification,
    validate_pressure_topology,
    validate_thermal_degrees_of_freedom,
)


def _pipes(**kwargs):
    return {key: value for key, value in kwargs.items()}


class TestLoopCounting:
    @pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
    def test_closed_loop_branch_has_n_plus_one_loops(self, n):
        report = analyse_pressure_topology(n, [None] * n, {}, bypass_fixed=False)
        assert report.n_loops == n + 1

    @pytest.mark.parametrize("n", [1, 2, 3, 5, 8])
    def test_required_specifications_is_twice_the_building_count(self, n):
        report = analyse_pressure_topology(n, [None] * n, {}, bypass_fixed=False)
        assert report.n_required == 2 * n

    def test_open_branch_has_one_loop_fewer(self):
        closed = analyse_pressure_topology(3, [None] * 3, {}, plant_closed_loop=True)
        opened = analyse_pressure_topology(3, [None] * 3, {}, plant_closed_loop=False)
        assert opened.n_loops == closed.n_loops - 1


class TestSuggestion:
    @pytest.mark.parametrize("n", [1, 2, 3, 4, 6])
    @pytest.mark.parametrize("bypass_fixed", [True, False])
    def test_suggestion_is_actually_consistent(self, n, bypass_fixed):
        suggestion = suggest_pressure_specification(n, bypass_fixed=bypass_fixed)
        fixed = set(suggestion["fixed"])
        pipes = {key: {"pr": 0.999} for key in fixed if key.startswith(("supply", "return"))}
        prs = [0.995 if f"building_{i}" in fixed else None for i in range(1, n + 1)]
        report = analyse_pressure_topology(n, prs, pipes, bypass_fixed=bypass_fixed)
        assert report.ok, report.message()
        assert report.n_specified == suggestion["n_required"]


class TestSpecificationDetection:
    @pytest.mark.parametrize(
        "attrs",
        [{"pr": 0.99}, {"dp": 0.1}, {"zeta": 4.0}, {"L": 100, "D": 0.3, "ks": 5e-5}],
    )
    def test_every_pressure_mechanism_counts_as_specified(self, attrs):
        report = analyse_pressure_topology(1, [None], {"supply_1": attrs}, bypass_fixed=False)
        assert report.n_specified == 1

    def test_partial_geometry_does_not_count(self):
        # L and D alone are a *thermal* specification, not a hydraulic one.
        report = analyse_pressure_topology(1, [None], {"supply_1": {"L": 100, "D": 0.3}},
                                           bypass_fixed=False)
        assert report.n_specified == 0

    def test_heat_only_attributes_do_not_count(self):
        report = analyse_pressure_topology(1, [None], {"supply_1": {"Q": 0.0}}, bypass_fixed=False)
        assert report.n_specified == 0


class TestRegressionsFromTheOriginalRepository:
    def test_length_derived_pr_original_is_over_determined(self):
        """`configs/config_length_derived_pr.yaml` as shipped: 7 fixed, 6 allowed."""
        pipes = _pipes(
            supply_2={"pr": 0.999}, supply_3={"pr": 0.999},
            return_3={"pr": 0.999}, return_2={"pr": 0.999}, return_1={"pr": 0.999},
        )
        report = analyse_pressure_topology(3, [0.995, None, None], pipes, bypass_fixed=True)
        assert not report.ok
        assert report.n_specified == 7
        assert report.n_required == 6
        assert len(report.redundant) == 1
        assert "Over-determined" in report.message()

    def test_length_pipes_original_is_both_over_and_under_determined(self):
        """`configs/config_length_pipes.yaml` as shipped."""
        geometry = {"L": 180.0, "D": 0.3, "ks": 5e-5}
        pipes = _pipes(supply_2=dict(geometry), supply_3=dict(geometry), return_3=dict(geometry))
        report = analyse_pressure_topology(3, [0.995, 0.995, 0.995], pipes, bypass_fixed=True)
        assert not report.ok
        assert report.redundant, "the last building and the bypass share both nodes"
        assert report.unanchored_nodes, "return_1 and return_2 had no specification at all"

    def test_the_specific_conflict_is_named(self):
        pipes = _pipes(supply_2={"pr": 0.999}, supply_3={"pr": 0.999}, return_3={"pr": 0.999})
        report = analyse_pressure_topology(3, [None, None, 0.995], pipes, bypass_fixed=True)
        offenders = {key for key, _ in report.redundant}
        # building_3 and the bypass both span S3 -> R3.
        assert offenders & {"building_3", "bypass"}

    def test_tutorial_specification_is_accepted(self):
        """`configs/config_tutorial.yaml` was the one that already worked."""
        pipes = _pipes(supply_2={"pr": 0.997}, return_1={"pr": 0.999})
        report = analyse_pressure_topology(2, [0.995, None], pipes, bypass_fixed=True)
        assert report.ok, report.message()

    def test_repaired_length_derived_pr_is_accepted(self):
        pipes = _pipes(
            supply_2={"pr": 0.999}, supply_3={"pr": 0.999},
            return_3={"pr": 0.999}, return_2={"pr": 0.999}, return_1={"pr": 0.999},
        )
        report = analyse_pressure_topology(3, [None, None, None], pipes, bypass_fixed=True)
        assert report.ok, report.message()


class TestValidateAndResolve:
    def test_validate_raises_with_an_actionable_message(self):
        pipes = _pipes(supply_2={"pr": 0.999}, supply_3={"pr": 0.999}, return_3={"pr": 0.999},
                       return_2={"pr": 0.999}, return_1={"pr": 0.999})
        with pytest.raises(ValueError) as excinfo:
            validate_pressure_topology(3, [0.995, None, None], pipes)
        message = str(excinfo.value)
        assert "Over-determined" in message
        assert "hydraulic loop" in message
        assert "Fix:" in message

    def test_validate_can_warn_instead_of_raising(self):
        pipes = _pipes(supply_2={"pr": 0.999}, supply_3={"pr": 0.999}, return_3={"pr": 0.999},
                       return_2={"pr": 0.999}, return_1={"pr": 0.999})
        with pytest.warns(UserWarning):
            validate_pressure_topology(3, [0.995, None, None], pipes, strict=False)

    def test_resolve_frees_the_minimum_number_of_buildings(self):
        pipes = _pipes(supply_2={"pr": 0.999}, supply_3={"pr": 0.999}, return_3={"pr": 0.999},
                       return_2={"pr": 0.999}, return_1={"pr": 0.999})
        prs, notes = resolve_pressure_specification(3, [0.995, None, None], pipes)
        assert len(notes) == 1
        assert analyse_pressure_topology(3, prs, pipes).ok

    def test_resolve_is_a_no_op_when_already_consistent(self):
        pipes = _pipes(supply_2={"pr": 0.997}, return_1={"pr": 0.999})
        prs, notes = resolve_pressure_specification(2, [0.995, None], pipes)
        assert notes == []
        assert prs == [0.995, None]

    def test_resolve_refuses_to_touch_an_under_specified_branch(self):
        # Freeing more elements can only make it worse, so it must report rather
        # than "repair".
        prs, notes = resolve_pressure_specification(3, [0.995, 0.995, 0.995], {})
        assert notes == []
        assert prs == [0.995, 0.995, 0.995]


class TestThermalDegreesOfFreedom:
    KEYS = ["supply_1", "supply_2", "return_2", "return_1"]

    def test_supply_temperature_control_needs_every_pipe_specified(self):
        validate_thermal_degrees_of_freedom(self.KEYS, self.KEYS, "supply_temperature")

    def test_supply_temperature_control_rejects_a_free_pipe(self):
        with pytest.raises(ValueError, match="under-specified"):
            validate_thermal_degrees_of_freedom(self.KEYS, self.KEYS[:-1], "supply_temperature")

    def test_evaporator_duty_control_needs_exactly_one_free_pipe(self):
        validate_thermal_degrees_of_freedom(self.KEYS, self.KEYS[:-1], "evaporator_duty")

    def test_evaporator_duty_control_rejects_zero_free_pipes(self):
        with pytest.raises(ValueError, match="exactly one pipe"):
            validate_thermal_degrees_of_freedom(self.KEYS, self.KEYS, "evaporator_duty")

    def test_evaporator_duty_control_rejects_two_free_pipes(self):
        with pytest.raises(ValueError, match="exactly one pipe"):
            validate_thermal_degrees_of_freedom(self.KEYS, self.KEYS[:-2], "evaporator_duty")

    def test_unknown_mode_is_rejected(self):
        with pytest.raises(ValueError):
            validate_thermal_degrees_of_freedom(self.KEYS, self.KEYS, "whatever")
