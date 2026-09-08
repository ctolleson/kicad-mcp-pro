"""Headless execution of KiCad menu commands.

A menu command reaches one of five destinations, recorded in the binding index:

    mcp       a registered MCP tool already drives it -> name that tool
    cli       a kicad-cli subcommand drives it        -> run it here
    file      a board/schematic file edit drives it   -> explain, no tool yet
    ipc       needs a running KiCad over the IPC API  -> explain the requirement
    gui-only  KiCad exposes no headless path at all   -> say so plainly

Only ``cli`` commands are executed here. Everything else returns an explanation
rather than pretending to act, so an agent is never told a command ran when it
did not.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import get_config
from ..path_safety import assert_within
from .catalog import MenuAction, get_action

# kicad-cli commands that read a board, a schematic, or neither.
_PCB_ROOTS = ("pcb",)
_SCH_ROOTS = ("sch",)
_NO_INPUT = {("version",)}

# Long options are the only ones accepted from an agent: short flags are ambiguous
# across subcommands and offer nothing a long option does not.
_OPTION_PATTERN = re.compile(r"(--[a-z0-9][a-z0-9-]*)")


@dataclass(frozen=True)
class InvocationResult:
    """Outcome of an attempt to reach a menu command headlessly."""

    action_name: str
    label: str
    channel: str
    status: str
    executed: bool
    summary: str
    command: tuple[str, ...] = ()
    return_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    outputs: tuple[str, ...] = field(default_factory=tuple)


class MenuInvocationError(ValueError):
    """Raised when a menu command cannot be driven as requested."""


@lru_cache(maxsize=64)
def cli_options(command: tuple[str, ...]) -> frozenset[str]:
    """Return the long options a kicad-cli subcommand accepts, from its own --help.

    Asking the installed binary keeps the allow-list correct across KiCad versions
    instead of freezing one release's flags into the source.
    """
    cfg = get_config()
    if not cfg.kicad_cli.exists():
        return frozenset()
    try:
        completed = subprocess.run(  # noqa: S603 - fixed binary, fixed argv shape
            [str(cfg.kicad_cli), *command, "--help"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(_OPTION_PATTERN.findall(completed.stdout + completed.stderr))


def _input_file(command: tuple[str, ...]) -> Path | None:
    """Return the design file a kicad-cli command operates on."""
    if command in _NO_INPUT:
        return None
    cfg = get_config()
    root = command[0] if command else ""
    if root in _PCB_ROOTS:
        if cfg.pcb_file is None or not cfg.pcb_file.exists():
            raise MenuInvocationError("No PCB file is configured. Call kicad_set_project() first.")
        return cfg.pcb_file
    if root in _SCH_ROOTS:
        if cfg.sch_file is None or not cfg.sch_file.exists():
            raise MenuInvocationError(
                "No schematic file is configured. Call kicad_set_project() first."
            )
        return cfg.sch_file
    return None


def _validate_options(command: tuple[str, ...], options: dict[str, Any]) -> list[str]:
    """Turn an option mapping into argv, rejecting anything the subcommand rejects."""
    accepted = cli_options(command)
    argv: list[str] = []
    for raw_key, value in options.items():
        key = raw_key if raw_key.startswith("--") else f"--{raw_key.replace('_', '-')}"
        if accepted and key not in accepted:
            near = sorted(name for name in accepted if key.lstrip("-")[:4] in name)
            hint = f" Did you mean: {', '.join(near[:5])}?" if near else ""
            raise MenuInvocationError(
                f"`kicad-cli {' '.join(command)}` does not accept {key}.{hint}"
            )
        if value is False or value is None:
            continue
        argv.append(key)
        if value is not True:
            argv.append(str(value))
    return argv


def _describe_non_cli(action: MenuAction) -> str:
    """Explain how to reach a command this module will not execute itself."""
    if action.channel == "mcp":
        tool = action.mcp_tool or "(unnamed tool)"
        text = f"Call the `{tool}` MCP tool — it drives this menu command directly."
        if action.status == "partial":
            text += " Coverage is partial; see the notes."
        return text
    if action.channel == "file":
        return (
            "No tool drives this yet. It is reachable by editing the design file "
            "(.kicad_pcb / .kicad_sch / .kicad_pro) directly."
        )
    if action.channel == "ipc":
        return (
            "This needs a running KiCad reachable over the IPC API; kicad-cli has no "
            "verb for it in this KiCad version."
        )
    return "KiCad exposes no headless path for this command — it is GUI-only."


def invoke(
    action_name: str,
    options: dict[str, Any] | None = None,
    *,
    output: str = "",
    dry_run: bool = False,
    timeout: float = 300.0,
) -> InvocationResult:
    """Reach a KiCad menu command headlessly, running kicad-cli when that is the path."""
    action = get_action(action_name)
    if action is None:
        raise MenuInvocationError(
            f"No KiCad menu command matches '{action_name}'. "
            "Search with kicad_menu_search() to find the right name."
        )

    if not action.cli_command:
        return InvocationResult(
            action_name=action.action_name,
            label=action.label,
            channel=action.channel,
            status=action.status,
            executed=False,
            summary=_describe_non_cli(action),
        )

    command = action.cli_command
    argv: list[str] = [*command]
    argv.extend(_validate_options(command, options or {}))

    outputs: list[str] = []
    if output:
        cfg = get_config()
        base = cfg.ensure_output_dir()
        destination = Path(output).expanduser()
        # Keep agent-supplied output paths inside the workspace, the same guarantee
        # every other write path in this server makes.
        destination = base / destination if not destination.is_absolute() else destination
        assert_within(cfg.workspace, destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if "--output" in cli_options(command):
            argv.extend(["--output", str(destination)])
            outputs.append(str(destination))
        else:
            raise MenuInvocationError(f"`kicad-cli {' '.join(command)}` takes no --output option.")

    source = _input_file(command)
    if source is not None:
        argv.append(str(source))

    if dry_run:
        return InvocationResult(
            action_name=action.action_name,
            label=action.label,
            channel=action.channel,
            status=action.status,
            executed=False,
            summary="Dry run — command not executed.",
            command=("kicad-cli", *argv),
            outputs=tuple(outputs),
        )

    # Reuse the export layer's runner so timeouts, telemetry and path scrubbing match
    # the rest of the server.
    from ..tools.export_support import _run_cli

    return_code, stdout, stderr = _run_cli(*argv, timeout=timeout)
    ok = return_code == 0
    summary = (
        f"Ran `kicad-cli {' '.join(argv[: len(command)])}` for menu command "
        f"'{action.label}' — {'succeeded' if ok else f'failed (exit {return_code})'}."
    )
    return InvocationResult(
        action_name=action.action_name,
        label=action.label,
        channel=action.channel,
        status=action.status,
        executed=True,
        summary=summary,
        command=("kicad-cli", *argv),
        return_code=return_code,
        stdout=stdout,
        stderr=stderr,
        outputs=tuple(outputs),
    )


__all__ = ["InvocationResult", "MenuInvocationError", "cli_options", "invoke"]
