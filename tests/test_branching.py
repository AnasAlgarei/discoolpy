"""Branching networks: parsing, degrees of freedom, assembly, and solving.

A branch that forks is not a cosmetic change. It puts two paths in parallel
between one pair of hydraulic nodes, which changes how many pressure drops may
be specified; it makes "which building is on which street, and in what order" a
question the scenario has to answer; and with a satellite plant it puts a second
evaporator in the chilled-water loop, which changes what the energy balance
closes on. These tests cover all three.
"""

import warnings

import pytest

from discoolpy import (
    Branch,
    BranchTopology,
    EndBypass,
    SatellitePlant,
    SubBranch,
    TerminalTopology,
    analyse_network_topology,
    build_system,
    load_yaml_config,
    make_branch_specs,
    make_weather_and_load_profiles,
    network_design_mass_flows,
    network_topology,
    resolve_network_pressure_specification,
    run_configured_case,
)
from discoolpy.config_schema import heat_gains_enabled

warnings.filterwarnings("ignore", category=FutureWarning)

BRANCHING = "config_branching_grid.yaml"


# Topology arithmetic, with no TESPy and no scenario file

def _ladder(label, n, pipes_fixed=True, terminals=None):
    pipes = {}
    if pipes_fixed:
        for i in range(1, (n or 1) + 1):
            pipes[f"supply_{i}"] = {"pr": 0.999}
            pipes[f"return_{i}"] = {"pr": 0.999}
    return BranchTopology(
        label=label,
        building_pr=[None] * n,
        pipe_attrs=pipes,
        terminals=terminals or [TerminalTopology(key="bypass", specified=False, spec="free")],
    )


class TestForkDegreesOfFreedom:
    def test_each_extra_terminal_at_a_fork_adds_one_loop(self):
        """Terminals at a fork sit in parallel between one pair of nodes."""
        loops = []
        for count in (1, 2, 3, 4):
            root = _ladder("root", 2, terminals=[
                TerminalTopology(key=f"bypass_{i}", specified=False, spec="free")
                for i in range(count)
            ])
            report = analyse_network_topology(root)
            loops.append(report.n_loops)
            assert report.n_forks == (1 if count > 1 else 0)
        assert loops == [loops[0] + i for i in range(4)]

    def test_pipes_that_anchor_both_ends_leave_no_room_for_a_pinned_terminal(self):
        """The rule a forking network keeps running into.

        Once each parallel path is reachable through its own supply and
        return pipes, both of its end nodes are anchored already. A terminal
        spanning them closes a loop, so pinning its drop is one specification
        too many.
        """
        free = _ladder("root", 1, terminals=[
            TerminalTopology(key="north", kind="branch", specified=False, child=_ladder(
                "north", 1,
                terminals=[TerminalTopology(key="north bypass", specified=False, spec="free")],
            )),
            TerminalTopology(key="east", kind="branch", specified=False, child=_ladder(
                "east", 1,
                terminals=[TerminalTopology(key="east bypass", specified=False, spec="free")],
            )),
        ])
        assert analyse_network_topology(free).ok

    def test_pinning_the_bypasses_of_a_fork_is_over_determined(self):
        """The exact failure a working single street hits the moment it forks."""
        forked = _ladder("root", 1, terminals=[
            TerminalTopology(key="north", kind="branch", specified=False, child=_ladder(
                "north", 1,
                terminals=[TerminalTopology(key="north bypass", specified=True, spec="dp=0")],
            )),
            TerminalTopology(key="east", kind="branch", specified=False, child=_ladder(
                "east", 1,
                terminals=[TerminalTopology(key="east bypass", specified=True, spec="dp=0")],
            )),
        ])
        report = analyse_network_topology(forked)
        assert not report.ok
        assert len(report.redundant) == 2
        assert report.n_forks == 1
        assert "parallel" in report.message()
        assert "dp: null" in report.message()

    def test_the_resolver_frees_bypasses_when_no_building_leg_can_help(self):
        forked = _ladder("root", 1, terminals=[
            TerminalTopology(key="north", kind="branch", specified=False, child=_ladder(
                "north", 1,
                terminals=[TerminalTopology(key="north bypass", specified=True, spec="dp=0")],
            )),
            TerminalTopology(key="east", kind="branch", specified=False, child=_ladder(
                "east", 1,
                terminals=[TerminalTopology(key="east bypass", specified=True, spec="dp=0")],
            )),
        ])
        actions, notes = resolve_network_pressure_specification(forked)
        assert [a["target"] for a in actions] == ["terminal", "terminal"]
        assert all("balancing valve" in note for note in notes)
        assert analyse_network_topology(forked).ok

    def test_a_building_leg_is_freed_before_a_bypass(self):
        """Relaxing a balancing valve at a building costs less than at a dead end."""
        forked = _ladder("root", 1, terminals=[
            TerminalTopology(key="north", kind="branch", specified=False, child=_ladder(
                "north", 1,
                terminals=[TerminalTopology(key="north bypass", specified=False, spec="free")],
            )),
        ])
        forked.terminals[0].child.building_pr = [0.995]
        actions, _ = resolve_network_pressure_specification(forked)
        assert [a["target"] for a in actions] == ["building"]
        assert analyse_network_topology(forked).ok

    def test_a_trunk_link_with_no_buildings_still_carries_a_pipe_pair(self):
        link = BranchTopology(label="link", building_pr=[], pipe_attrs={}, terminals=[
            TerminalTopology(key="bypass", specified=False, spec="free")
        ])
        assert link.n_pipes_per_side == 1
        keys = {e.key for e in analyse_network_topology(link).edges}
        assert {"supply_1", "return_1"} <= keys


# Scenario parsing

class TestBranchSpecParsing:
    def test_the_tree_is_read_in_terminal_order(self, tmp_config):
        root = make_branch_specs(tmp_config(BRANCHING))
        assert [b.label for b in root.walk()] == [
            "downtown trunk", "north spur", "east spur", "south leg"
        ]

    def test_buildings_keep_the_order_the_branch_lists_them_in(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        labels = [b["label"] for b in cfg["buildings"]]
        root = make_branch_specs(cfg)
        by_branch = {
            b.label: [labels[i] for i in b.building_indices] for b in root.walk()
        }
        assert by_branch["downtown trunk"] == ["city hall", "conference centre"]
        assert by_branch["north spur"] == ["north tower", "teaching block"]
        assert by_branch["south leg"] == ["exhibition hall"]

    def test_reordering_a_branch_list_reorders_the_taps(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["buildings"] = ["conference centre", "city hall"]
        system = build_system(cfg)
        trunk = system.branches[0]
        assert [b.label for b in trunk.buildings] == ["conference centre", "city hall"]

    def test_a_legacy_scenario_still_means_one_street_and_one_bypass(self, tmp_config):
        root = make_branch_specs(tmp_config("config_campus_five_buildings.yaml"))
        assert len(list(root.walk())) == 1
        assert root.n_buildings == 5
        assert [t.kind for t in root.terminals] == ["bypass"]

    def test_an_unplaced_building_is_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["buildings"] = ["city hall"]
        with pytest.raises(ValueError, match="placed on no branch"):
            make_branch_specs(cfg)

    def test_a_building_on_two_branches_is_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["buildings"] = ["city hall", "conference centre", "north tower"]
        with pytest.raises(ValueError, match="listed on both branch"):
            make_branch_specs(cfg)

    def test_a_pipe_that_cannot_exist_is_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["pipes"]["supply_9"] = {"pr": 0.999}
        with pytest.raises(ValueError, match="do not exist"):
            make_branch_specs(cfg)

    def test_duplicate_branch_labels_are_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["terminals"][0]["label"] = "downtown trunk"
        with pytest.raises(ValueError, match="Duplicate branch label"):
            make_branch_specs(cfg)

    def test_a_satellite_without_a_slipstream_is_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        plant = cfg["branch"]["terminals"][1]["terminals"][1]
        plant.pop("m_kg_s")
        with pytest.raises(ValueError, match="explicit 'm_kg_s'"):
            make_branch_specs(cfg)

    def test_a_satellite_without_a_duty_is_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        plant = cfg["branch"]["terminals"][1]["terminals"][1]
        plant.pop("Q_evap_kW")
        with pytest.raises(ValueError, match="needs a duty"):
            build_system(cfg)

    def test_an_empty_terminal_list_is_refused(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["terminals"] = []
        with pytest.raises(ValueError, match="must return"):
            make_branch_specs(cfg)

    def test_children_inherit_the_parents_pipe_and_heat_models(self, tmp_config):
        root = make_branch_specs(tmp_config(BRANCHING))
        for branch in root.walk():
            assert branch.setting("pipe_model") == "darcy"
            assert branch.setting("heat_model") == "ua"

    def test_a_satellite_plant_makes_the_plant_duty_a_solved_output(self, tmp_config):
        # A second evaporator in the loop means the loop balance, not an
        # asserted number, has to determine the main plant's duty.
        cfg = tmp_config(BRANCHING)
        assert heat_gains_enabled(cfg)


# Mass flows

class TestDesignMassFlows:
    def test_a_branch_carries_its_own_subtree(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        flows = network_design_mass_flows(cfg)
        trunk, north = flows["downtown trunk"], flows["north spur"]
        east, south = flows["east spur"], flows["south leg"]
        assert trunk["inlet"] == pytest.approx(
            sum(trunk["buildings"]) + north["inlet"] + east["inlet"]
        )
        assert east["inlet"] == pytest.approx(
            sum(east["buildings"]) + south["inlet"] + east["terminals"]["airport satellite"]
        )

    def test_the_first_pipe_carries_everything_downstream_of_it(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        flows = network_design_mass_flows(cfg)
        assert flows["downtown trunk"]["pipe"]["supply_1"] == pytest.approx(
            flows["downtown trunk"]["inlet"]
        )

    def test_supply_and_return_carry_the_same_flow(self, tmp_config):
        flows = network_design_mass_flows(tmp_config(BRANCHING))
        for entry in flows.values():
            for key, value in entry["pipe"].items():
                if key.startswith("supply"):
                    assert entry["pipe"][key.replace("supply", "return")] == value


# Assembly and solving

class TestBranchingSystem:
    def test_it_builds_the_tree_it_was_asked_for(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        assert [b.label for b in system.branches] == [
            "downtown trunk", "north spur", "east spur", "south leg"
        ]
        assert len(system.buildings) == 6
        assert len(system.satellite_plants) == 1
        assert len(system.chillers) == 2
        # Two forks: the trunk into north/east, and the east spur into
        # south leg / satellite.
        forks = [b for b in system.branches if len(b.terminals) > 1]
        assert [b.label for b in forks] == ["downtown trunk", "east spur"]

    def test_terminals_are_the_three_kinds(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        kinds = {
            type(t) for b in system.branches for t in b.terminals
        }
        assert kinds == {SubBranch, EndBypass, SatellitePlant}

    def test_every_pipe_in_the_tree_is_reported(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        keys = [k for k, _ in system.branch.tree_pipe_items()]
        # 2 + 2 + 1 + 1 taps, supply and return, root keys bare and the rest
        # namespaced by branch.
        assert len(keys) == 12
        assert "supply_1" in keys
        assert "north spur/supply_2" in keys
        assert "south leg/return_1" in keys

    def test_it_reaches_a_converged_design_point(self, tmp_config):
        system = build_system(tmp_config(BRANCHING))
        system.network.solve(mode="design", max_iter=400)
        assert system.network.converged
        assert system.branch.pressure_feasibility()["feasible"]

    def test_the_chilled_water_loop_balances_with_a_satellite_in_it(self, tmp_config):
        """Q_evap + satellite duty must equal loads + pipe gain + pump heat."""
        system = build_system(tmp_config(BRANCHING))
        system.network.solve(mode="design", max_iter=400)
        assert system.network.converged
        satellites = system.branch.satellite_report()
        residual = (
            system.chiller.solved_Q_evap_W
            + satellites["total_Q_evap_W"]
            - satellites["total_storage_Q_W"]
            - sum(b.component.Q.val for b in system.buildings)
            - system.branch.heat_gain_report()["total_heat_gain_W"]
            - system.branch.pump.P.val
        )
        assert abs(residual) < 1.0, f"{residual:.3f} W unaccounted for"

    def test_a_satellite_takes_load_off_the_main_plant(self, tmp_config):
        """The whole point of a distributed plant, stated as a number."""
        with_plant = build_system(tmp_config(BRANCHING))
        with_plant.network.solve(mode="design", max_iter=400)
        assert with_plant.network.converged
        duty_with = with_plant.chiller.solved_Q_evap_W

        cfg = tmp_config(BRANCHING)
        # Swap the satellite for a plain bypass carrying the same slipstream.
        east = cfg["branch"]["terminals"][1]
        east["terminals"][1] = {
            "type": "bypass", "label": "airport dead end", "m_kg_s": 14.0, "dp": None,
        }
        without = build_system(cfg)
        without.network.solve(mode="design", max_iter=400)
        assert without.network.converged

        assert duty_with < without.chiller.solved_Q_evap_W
        assert without.chiller.solved_Q_evap_W - duty_with == pytest.approx(
            180_000.0, rel=0.02
        )

    def test_pinning_both_end_bypasses_is_refused_with_an_actionable_message(
        self, tmp_config
    ):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["terminals"][0]["terminals"][0]["dp"] = 0.0
        cfg["branch"]["terminals"][1]["terminals"][0]["terminals"][0]["dp"] = 0.0
        with pytest.raises(ValueError) as excinfo:
            build_system(cfg)
        message = str(excinfo.value)
        assert "Over-determined" in message
        assert "parallel" in message

    def test_auto_relax_repairs_it_instead(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["branch"]["terminals"][0]["terminals"][0]["dp"] = 0.0
        cfg["branch"]["terminals"][1]["terminals"][0]["terminals"][0]["dp"] = 0.0
        cfg["branch"]["auto_relax_pressure"] = True
        system = build_system(cfg)
        assert any("Released" in note for note in system.notes)
        system.network.solve(mode="design", max_iter=400)
        assert system.network.converged

    def test_the_surface_spur_gains_more_per_metre_than_the_buried_trunk(
        self, tmp_config
    ):
        """Mixed placements survive the trip down the tree."""
        from discoolpy import make_network_pipe_thermal

        from discoolpy import buried_pipe_UA_per_m

        thermal = make_network_pipe_thermal(tmp_config(BRANCHING))
        buried = thermal["downtown trunk"]["supply_2"]
        exposed = thermal["south leg"]["supply_1"]
        assert exposed.ambient_source == "air"
        assert buried.ambient_source == "ground"
        # Compare like with like: the exposed spur is a much smaller pipe than
        # the trunk, so the honest comparison is against the same pipe buried.
        same_pipe_buried = buried_pipe_UA_per_m(
            inner_diameter_m=0.13,
            insulation_thickness_m=0.06,
            insulation_conductivity="pur",
            burial_depth_m=1.4,
            ground="dry soil",
            pipe_wall_thickness_m=0.007,
            twin_spacing_m=0.8,
        )
        assert exposed.UA_per_m_W_mK > same_pipe_buried


@pytest.mark.slow
class TestBranchingTimeSeries:
    def test_it_runs_offdesign_and_closes_its_energy_balance(self, tmp_config):
        cfg = tmp_config(BRANCHING)
        cfg["profiles"]["periods"] = 12
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "grid", use_storage=False, progress=False)
        assert len(results) == 12
        assert results["chw_energy_residual_W"].abs().max() < 1.0
        assert (results["pipe_heat_gain_W"] > 0).all()
        assert (results["satellite_Q_evap_W"] > 0).all()
        # The fleet COP counts both machines' electricity against both machines'
        # cooling, which is the number an operator is billed for.
        assert results["fleet_cop"].between(1.5, 8.0).all()

    def test_a_proportionally_dispatched_satellite_follows_the_district(
        self, tmp_config
    ):
        cfg = tmp_config(BRANCHING)
        cfg["profiles"]["periods"] = 48
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "grid", use_storage=False, progress=False)
        delivered = results["satellite_delivered_Q_W"]
        load = results["actual_building_total_Q_W"]
        assert delivered.corr(load) > 0.95
        assert delivered.max() > delivered.min() * 1.3

    def test_the_satellite_store_levels_its_own_chiller(self, tmp_config):
        """A store at a satellite exists so the machine can run flat."""
        cfg = tmp_config(BRANCHING)
        cfg["profiles"]["periods"] = 48
        frame = make_weather_and_load_profiles(cfg)
        results = run_configured_case(cfg, frame, "grid", use_storage=False, progress=False)
        delivered = results["satellite_delivered_Q_W"]
        produced = results["satellite_Q_evap_W"]
        assert produced.std() < delivered.std(), (
            "the store must flatten the chiller relative to what it delivers"
        )
        soc = results["satellite_airport_satellite_storage_soc"]
        assert soc.max() > soc.min(), "the store has to actually cycle"
        assert soc.between(0.0, 1.0).all()


# Shapes no shipped scenario happens to have

class TestUncommonShapes:
    """A trunk link with no buildings, and a fork wider than two ways.

    Neither appears in a shipped scenario, and both are easy to break: the first
    has no splitters or merges at all, and the second is the only thing that
    exercises a TESPy Splitter with more than two outlets.
    """

    @staticmethod
    def _with_trunk_link(cfg):
        """Put 400 m of buildingless main between the plant and the old root."""
        import copy

        old_root = copy.deepcopy(cfg["branch"])
        old_root.update(type="branch", pump_placement=None, pump_label=None)
        for key in ("pump_pressure_ratio", "fix_pump_power", "origin_m"):
            old_root.pop(key, None)
        cfg["branch"] = {
            "label": "plant main",
            "buildings": [],
            "pump_placement": "supply_inlet",
            "pump_label": "main pump",
            "pipe_model": "darcy",
            "fix_pump_power": False,
            "pump_pressure_ratio": 1.30,
            "heat_model": "ua",
            "thermal_defaults": old_root["thermal_defaults"],
            "origin_m": [0.0, 0.0],
            "heading_deg": 0,
            "pipes": {
                "supply_1": {"L": 400.0, "D": 0.30, "ks": 5e-5, "heading_deg": 0},
                "return_1": {"L": 400.0, "D": 0.30, "ks": 5e-5},
            },
            "terminals": [old_root],
        }
        return cfg

    @staticmethod
    def _with_third_spur(cfg):
        cfg["buildings"].append(
            {"label": "civic centre", "Q_design_W": 150000.0, "pr": None,
             "archetype": "office"}
        )
        cfg["branch"]["terminals"].append({
            "type": "branch",
            "label": "south spur",
            "buildings": ["civic centre"],
            "heading_deg": -90,
            "pipes": {
                "supply_1": {"L": 240.0, "D": 0.10, "ks": 5e-5, "heading_deg": -90},
                "return_1": {"L": 240.0, "D": 0.10, "ks": 5e-5},
            },
            "terminals": [{"type": "bypass", "label": "south spur dead end",
                           "m_kg_s": 0.05, "dp": None, "heading_deg": -90}],
        })
        return cfg

    def test_a_buildingless_trunk_link_carries_its_whole_subtree(self, tmp_config):
        cfg = self._with_trunk_link(tmp_config(BRANCHING))
        root = make_branch_specs(cfg)
        assert root.label == "plant main"
        assert root.n_buildings == 0
        assert root.pipe_keys == ["supply_1", "return_1"]
        flows = network_design_mass_flows(cfg, root)
        # A link has nothing of its own, so its pipe carries exactly its child's inlet.
        assert flows["plant main"]["pipe"]["supply_1"] == pytest.approx(
            flows["downtown trunk"]["inlet"]
        )
        assert analyse_network_topology(network_topology(cfg)).ok

    def test_a_buildingless_trunk_link_solves(self, tmp_config):
        cfg = self._with_trunk_link(tmp_config(BRANCHING))
        system = build_system(cfg)
        link = system.branches[0]
        assert link.is_link and not link.splitters and not link.merges
        system.network.solve(mode="design", max_iter=400)
        assert system.network.converged
        assert system.branch.pressure_feasibility()["feasible"]

    def test_a_three_way_fork_solves_and_balances(self, tmp_config):
        cfg = self._with_third_spur(tmp_config(BRANCHING))
        report = analyse_network_topology(network_topology(cfg))
        assert report.ok and report.n_branches == 5

        system = build_system(cfg)
        trunk = system.branches[0]
        assert len(trunk.terminals) == 3
        assert trunk.end_splitter.outlets() == ["out1", "out2", "out3"]

        system.network.solve(mode="design", max_iter=400)
        assert system.network.converged

        satellites = system.branch.satellite_report()
        residual = (
            system.chiller.solved_Q_evap_W
            + satellites["total_Q_evap_W"]
            - satellites["total_storage_Q_W"]
            - sum(b.component.Q.val for b in system.buildings)
            - system.branch.heat_gain_report()["total_heat_gain_W"]
            - system.branch.pump.P.val
        )
        assert abs(residual) < 1.0

    def test_a_third_spur_is_drawn_on_its_own_bearing(self, tmp_config):
        from discoolpy import compute_layout

        cfg = self._with_third_spur(tmp_config(BRANCHING))
        layout = compute_layout(build_system(cfg))
        civic = next(n for n in layout.nodes_of("building") if n.label == "civic centre")
        north = next(n for n in layout.nodes_of("building") if n.label == "north tower")
        # The trunk forks at x = 860; one spur goes north, the new one south.
        assert civic.y < 0 < north.y
