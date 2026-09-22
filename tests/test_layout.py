"""The network layout plotter: geometry first, drawing second.

The layout is worth testing separately from the drawing because it is the part
that can be *wrong*. A picture that looks plausible while placing a spur on the
wrong side of the street is worse than no picture, so these tests check
coordinates in metres, not pixels.
"""

import warnings

import matplotlib
import pytest

matplotlib.use("Agg")

from discoolpy import build_system, compute_layout, plot_network
from discoolpy.layout import NODE_MARKERS

warnings.filterwarnings("ignore", category=FutureWarning)

BRANCHING = "config_branching_grid.yaml"


class TestMarkerConvention:
    def test_every_component_kind_has_its_agreed_marker(self):
        assert NODE_MARKERS["chiller"] == "s"          # square
        assert NODE_MARKERS["cooling_tower"] == "8"    # octagon
        assert NODE_MARKERS["storage"] == "o"          # circle
        assert NODE_MARKERS["building"] == "^"         # triangle
        assert NODE_MARKERS["junction"] == "."         # point


class TestGeometryFromTheScenario:
    def test_it_follows_the_headings_and_lengths_it_was_given(self, tmp_config):
        layout = compute_layout(build_system(tmp_config(BRANCHING)))
        assert layout.geometry_is_complete
        nodes = {n.key: n for n in layout.nodes}
        # The trunk runs 520 m then 340 m due east from the origin.
        assert nodes["downtown trunk/tap_1"].x == pytest.approx(520.0)
        assert nodes["downtown trunk/tap_1"].y == pytest.approx(0.0)
        assert nodes["downtown trunk/tap_2"].x == pytest.approx(860.0)
        # The north spur turns north at the fork and runs 300 m.
        assert nodes["north spur/tap_1"].x == pytest.approx(860.0)
        assert nodes["north spur/tap_1"].y == pytest.approx(300.0)
        # The south leg turns south from the east spur's end at x = 1270.
        assert nodes["south leg/tap_1"].x == pytest.approx(1270.0)
        assert nodes["south leg/tap_1"].y == pytest.approx(-280.0)

    def test_a_fork_is_drawn_as_a_point(self, tmp_config):
        layout = compute_layout(build_system(tmp_config(BRANCHING)))
        forks = [n for n in layout.nodes if n.meta.get("role") == "split/merge"]
        assert {n.label for n in forks} == {"downtown trunk fork", "east spur fork"}
        assert all(n.kind == "junction" for n in forks)
        assert {n.meta["ways"] for n in forks} == {2}

    def test_every_building_is_drawn_once_as_a_triangle(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        layout = compute_layout(system)
        drawn = layout.nodes_of("building")
        assert {n.label for n in drawn} == {b.label for b in system.buildings}
        assert len(drawn) == len(system.buildings)

    def test_buildings_are_drawn_in_branch_order(self, tmp_config):
        layout = compute_layout(build_system(tmp_config(BRANCHING)))
        north = [n for n in layout.nodes_of("building") if n.branch == "north spur"]
        assert [n.label for n in north] == ["north tower", "teaching block"]
        assert [n.meta["order"] for n in north] == [1, 2]

    def test_both_plants_are_drawn_with_their_towers(self, tmp_config):
        layout = compute_layout(build_system(tmp_config(BRANCHING)))
        assert len(layout.nodes_of("chiller")) == 2
        assert len(layout.nodes_of("cooling_tower")) == 2
        # Only the satellite has a store in this scenario.
        assert [n.label for n in layout.nodes_of("storage")] == ["satellite_store"]

    def test_a_supervisory_store_is_not_drawn_as_a_pipe_fitting(self, tmp_config):
        """It is an accounting device; only a hydraulic store sits in the network."""
        cfg = tmp_config("config_riyadh_heat_gains.yaml")
        cfg["storage"]["coupling"] = "supervisory"
        layout = compute_layout(build_system(cfg))
        assert layout.nodes_of("storage") == []

    def test_pipes_are_drawn_as_a_supply_and_return_pair(self, tmp_config):
        layout = compute_layout(build_system(tmp_config(BRANCHING)))
        supply = [e for e in layout.edges if e.kind == "supply"]
        assert len(supply) == 6                       # 2 + 2 + 1 + 1
        assert len([e for e in layout.edges if e.kind == "return"]) == len(supply)
        lengths = sorted(round(e.meta["length_m"]) for e in supply)
        assert lengths == [260, 280, 300, 340, 410, 520]
        # Drawn length must equal the specified length, offsets notwithstanding.
        for edge in supply:
            assert edge.length_m == pytest.approx(edge.meta["length_m"], rel=1e-9)


class TestWithoutGeometry:
    def test_a_scenario_with_no_lengths_still_draws(self, tmp_config):
        """The tutorial has no L and no bearings at all."""
        layout = compute_layout(build_system(tmp_config("config_tutorial.yaml")))
        assert not layout.geometry_is_complete
        assert layout.assumed_lengths
        assert len(layout.nodes_of("building")) == 2
        # A nominal segment, so the drawing is schematic but still to scale
        # with itself.
        assert all(e.length_m > 0 for e in layout.edges if e.kind == "supply")

    def test_terminals_with_no_bearing_fan_out(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        for terminal in cfg["branch"]["terminals"]:
            terminal.pop("heading_deg", None)
            for pipe in terminal.get("pipes", {}).values():
                pipe.pop("heading_deg", None)
        layout = compute_layout(build_system(cfg))
        north = next(n for n in layout.nodes_of("building") if n.label == "north tower")
        hotel = next(n for n in layout.nodes_of("building") if n.label == "airport hotel")
        # One child goes left of the trunk, the other right; they must not be
        # drawn on top of each other.
        assert (north.y > 0) != (hotel.y > 0)

    def test_the_default_segment_length_is_configurable(self, tmp_config):
        system = build_system(tmp_config("config_tutorial.yaml"))
        short = compute_layout(system, default_segment_m=50.0)
        long = compute_layout(system, default_segment_m=500.0)
        assert long.extent_m > short.extent_m * 5


class TestDrawing:
    @pytest.mark.parametrize(
        "name",
        [
            "config_tutorial.yaml",
            "config_riyadh_heat_gains.yaml",
            "config_campus_five_buildings.yaml",
            BRANCHING,
        ],
    )
    def test_every_shipped_scenario_can_be_drawn(self, name, tmp_config, tmp_path):
        """The plotter has to work on every shipped scenario, forks or no forks."""
        system = build_system(tmp_config(name))
        target = tmp_path / f"{name}.png"
        ax = plot_network(system, save_path=str(target), annotate_pipes=True)
        assert target.exists() and target.stat().st_size > 0
        assert ax.get_title()
        matplotlib.pyplot.close(ax.figure)

    def test_it_draws_an_unsolved_network(self, tmp_config):
        """The layout is a property of the scenario, not of a solution."""
        system = build_system(tmp_config(BRANCHING))
        assert not getattr(system.network, "converged", False)
        ax = plot_network(system)
        assert len(ax.collections) >= 4       # one scatter per node kind drawn
        matplotlib.pyplot.close(ax.figure)

    def test_it_accepts_a_bare_branch(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        layout = compute_layout(system.branch)
        # No system means no central plant, but the distribution tree and the
        # satellite that hangs off it are still there.
        assert layout.nodes_of("building")
        assert [n.label for n in layout.nodes_of("chiller")] == ["airport satellite"]

    def test_pumps_can_be_left_out(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        ax = plot_network(system, show_pumps=False)
        labels = [t.get_text() for t in ax.texts]
        assert "main pump" not in labels
        matplotlib.pyplot.close(ax.figure)

    def test_records_round_trip_to_plain_data(self, tmp_config):
        layout = compute_layout(build_system(tmp_config(BRANCHING)))
        records = layout.to_records()
        assert len(records["nodes"]) == len(layout.nodes)
        assert len(records["edges"]) == len(layout.edges)
        assert {"key", "kind", "x_m", "y_m"} <= set(records["nodes"][0])
        assert {"x0_m", "y1_m", "length_m"} <= set(records["edges"][0])
