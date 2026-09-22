"""The exceptions DisCoolPy raises on its own account.

A scenario is written by hand, so most of what goes wrong is a typo, a missing
number or a unit mistake, and none of those deserve a traceback. They deserve a
sentence naming the file, the key and the fix. :class:`ScenarioError` carries
those three things separately so the command line can print them plainly and a
notebook can still catch them as an exception.

Everything raised deliberately inherits from :class:`DisCoolPyError`, so a study
driving many scenarios can tell "this scenario is wrong" from "this tool is
broken" without matching on message text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Sequence

__all__ = ["DisCoolPyError", "ScenarioError"]


class DisCoolPyError(Exception):
    """Base class for every error DisCoolPy raises deliberately."""


class ScenarioError(DisCoolPyError):
    """A scenario file that cannot be used as written.

    Parameters
    ----------
    problem:
        What is wrong, in one sentence.
    where:
        The path to the offending key, as a scenario file reads:
        ``buildings[0].Q_design_W``. Omit for a whole-file problem.
    hint:
        What to do about it. This is the part a user acts on, so it should name
        a key or a command rather than restate the problem.
    path:
        The scenario file, when there is one.
    extra:
        Further lines, for an error that has to list several things.
    """

    def __init__(
        self,
        problem: str,
        where: Optional[str] = None,
        hint: Optional[str] = None,
        path: Optional[Any] = None,
        extra: Optional[Sequence[str]] = None,
    ) -> None:
        self.problem = problem
        self.where = where
        self.hint = hint
        self.path = None if path is None else Path(path)
        self.extra: List[str] = list(extra or [])
        super().__init__(self.render())

    def render(self) -> str:
        """The message as the command line prints it."""
        head = "Scenario error"
        if self.path is not None:
            head += f" in {self.path.name}"
        lines = [head]
        lines.append(f"  {self.where}: {self.problem}" if self.where
                     else f"  {self.problem}")
        lines.extend(f"  {line}" for line in self.extra)
        if self.hint:
            lines.append(f"  -> {self.hint}")
        return "\n".join(lines)

    def __str__(self) -> str:  # pragma: no cover - delegation
        return self.render()
