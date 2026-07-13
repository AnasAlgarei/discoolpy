"""
Reusable TESPy cooling tower module for a chiller condenser-water loop.

The CoolingTower class wraps a SimpleHeatExchanger and a CycleCloser. It creates
all condenser-water connections to a chiller-like component with ports ``in2``
and ``out2``. The tower is intentionally modeled as an external heat sink; TESPy
calculates the tower heat rejection from the closed-loop energy balance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, Sequence

from tespy.components import CycleCloser, SimpleHeatExchanger
from tespy.connections import Connection


@dataclass
class CoolingTower:
    """Closed condenser-water loop with a SimpleHeatExchanger cooling tower.

    Parameters
    ----------
    label:
        Base label for the cooling-tower module.
    T_in_chiller:
        Condenser-water temperature entering the chiller in degC, i.e. tower
        outlet temperature.
    T_out_chiller:
        Condenser-water temperature leaving the chiller in degC, i.e. tower
        inlet temperature.
    p_in_chiller:
        Pressure anchor for the condenser-water loop in the network pressure
        unit, normally bar.
    fluid:
        Fluid composition dictionary for TESPy fluid propagation.
    pr:
        Optional cooling tower water-side pressure ratio. Leave this as ``None``
        when the chiller condenser already fixes the secondary pressure ratio;
        otherwise a closed loop can become overdetermined.
    ambient_temperature:
        Design ambient or wet-bulb proxy temperature in degC for the tower heat
        sink.
    approach_temperature:
        Default approach in K used to derive condenser-water inlet temperature
        from ambient temperature if a snapshot does not provide it directly.
    """

    label: str
    T_in_chiller: float = 30.0
    T_out_chiller: float = 35.0
    p_in_chiller: float = 3.0
    fluid: Optional[dict] = None
    pr: Optional[float] = None
    ambient_temperature: float = 26.0
    approach_temperature: float = 4.0
    tower: SimpleHeatExchanger = field(init=False)
    cycle_closer: CycleCloser = field(init=False)
    cond_in: Optional[Connection] = field(default=None, init=False)
    cond_out: Optional[Connection] = field(default=None, init=False)
    close_connection: Optional[Connection] = field(default=None, init=False)

    def __post_init__(self) -> None:
        self.tower = SimpleHeatExchanger(self.label)
        self.cycle_closer = CycleCloser(f"{self.label} cycle closer")
        if self.fluid is None:
            self.fluid = {"water": 1.0}

    def connect_to_chiller(
        self,
        chiller,
        condenser_in_port: str = "in2",
        condenser_out_port: str = "out2",
        cond_in_label: str = "cond_in",
        cond_out_label: str = "cond_out",
        close_label: Optional[str] = None,
    ) -> Sequence[Connection]:
        """Create the closed condenser-water loop connections to a chiller."""
        self.cond_in = Connection(
            self.cycle_closer,
            "out1",
            chiller,
            condenser_in_port,
            label=cond_in_label,
        )
        self.cond_out = Connection(
            chiller,
            condenser_out_port,
            self.tower,
            "in1",
            label=cond_out_label,
        )
        self.close_connection = Connection(
            self.tower,
            "out1",
            self.cycle_closer,
            "in1",
            label=close_label or f"{self.label.replace(' ', '_').lower()}_to_secondary_closer",
        )
        return self.cond_in, self.cond_out, self.close_connection

    @property
    def connections(self) -> Sequence[Connection]:
        """Return the currently created TESPy connections."""
        if self.cond_in is None or self.cond_out is None or self.close_connection is None:
            raise RuntimeError("Call connect_to_chiller before requesting connections.")
        return self.cond_in, self.cond_out, self.close_connection

    def set_design(self, native_offdesign: bool = False) -> None:
        """Apply condenser-water state constraints for the closed loop.

        With ``native_offdesign=True``, the design solve fixes the tower inlet
        and outlet temperatures to size the water flow and tower conductance, and
        offdesign solves hold the design water flow while using TESPy's native
        ``kA_char`` relation for the tower heat exchanger. The chiller condenser
        inlet temperature remains a snapshot boundary condition so weather can be
        imposed without an external tower equation.
        """
        if self.cond_in is None or self.cond_out is None:
            raise RuntimeError("Create cooling tower connections before setting design data.")
        if native_offdesign:
            self.cond_in.set_attr(
                fluid=self.fluid,
                p=self.p_in_chiller,
                T=self.T_in_chiller,
                design=["m"],
            )
            self.cond_out.set_attr(T=self.T_out_chiller, design=["T"])
            if self.pr is not None:
                self.tower.set_attr(
                    Tamb=self.ambient_temperature,
                    pr=self.pr,
                    design=["pr"],
                    offdesign=["zeta", "kA_char"],
                )
            else:
                self.tower.set_attr(
                    Tamb=self.ambient_temperature,
                    offdesign=["kA_char"],
                )
        else:
            self.cond_in.set_attr(fluid=self.fluid, p=self.p_in_chiller, T=self.T_in_chiller)
            self.cond_out.set_attr(T=self.T_out_chiller)
            if self.pr is not None:
                self.tower.set_attr(pr=self.pr)

    def set_offdesign_ambient(
        self,
        ambient_temperature: Optional[float] = None,
        condenser_inlet_temperature: Optional[float] = None,
    ) -> float:
        """Update tower ambient and condenser inlet conditions for a snapshot.

        Returns the condenser-water temperature entering the chiller. If the
        snapshot does not provide this temperature explicitly, it is estimated as
        ambient plus the configured approach temperature, which is a boundary
        condition rather than a replacement for TESPy's tower heat-transfer
        calculation.
        """
        if self.cond_in is None:
            raise RuntimeError("Create cooling tower connections before applying snapshot data.")
        if ambient_temperature is not None:
            self.ambient_temperature = float(ambient_temperature)
            self.tower.set_attr(Tamb=self.ambient_temperature)
        if condenser_inlet_temperature is None:
            condenser_inlet_temperature = self.ambient_temperature + self.approach_temperature
        self.T_in_chiller = float(condenser_inlet_temperature)
        self.cond_in.set_attr(T=self.T_in_chiller)
        return self.T_in_chiller

    def apply_snapshot(self, snapshot) -> float:
        """Apply ambient and condenser-water fields from a TimeSnapshot."""
        return self.set_offdesign_ambient(
            ambient_temperature=getattr(snapshot, "ambient_temperature", None),
            condenser_inlet_temperature=getattr(snapshot, "condenser_inlet_temperature", None),
        )

    def set_start(self, m: float = 35.0, pr_start: float = 0.999) -> None:
        """Set starting values for the condenser-water loop."""
        if self.cond_in is None or self.cond_out is None or self.close_connection is None:
            raise RuntimeError("Create cooling tower connections before setting start values.")
        for conn, temp, pressure in [
            (self.cond_in, self.T_in_chiller, self.p_in_chiller),
            (self.cond_out, self.T_out_chiller, self.p_in_chiller),
            (self.close_connection, self.T_in_chiller, self.p_in_chiller * pr_start),
        ]:
            conn.m.set_val0(m)
            conn.T.set_val0(temp)
            conn.p.set_val0(pressure)

    @property
    def heat_rejection(self) -> float:
        """Return positive tower heat rejection in W after a successful solve."""
        return -self.tower.Q.val


__all__ = ["CoolingTower"]
