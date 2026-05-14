#!/usr/bin/env python3
"""
Asterisk Queue Status Monitor
Usage: python3 queue_status.py <queue_name>
       python3 queue_status.py <queue_name> --interval 2
"""

import subprocess
import sys
import os
import re
import time
import select
import termios
import tty
import argparse
from datetime import datetime

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.text import Text
from rich.live import Live
from rich import box

console = Console()


def run_asterisk(command: str) -> str:
    try:
        result = subprocess.run(
            ["asterisk", "-rx", command],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout + result.stderr
    except FileNotFoundError:
        return "ERROR: 'asterisk' command not found. Make sure Asterisk is installed and in PATH."
    except subprocess.TimeoutExpired:
        return "ERROR: Command timed out."
    except Exception as e:
        return f"ERROR: {e}"


def parse_core_channels(raw: str) -> tuple[dict, dict]:
    """
    Parse 'core show channels verbose' output.

    Returns (agent_caller_map, channel_callerid_map):
      agent_caller_map    — member interface prefix -> caller ID on the other side of the bridge
                            e.g. {"PJSIP/10001605": "0820246914"}
      channel_callerid_map — full channel name -> that channel's own CallerID
                            e.g. {"PJSIP/CISCO_CUBE-00000407": "0661234567"}
                            Used to look up queued callers by their channel name.
    """
    if not raw.strip():
        return {}, {}

    lines = raw.splitlines()

    # Locate header line (contains both "Channel" and "CallerID")
    header_line = ""
    header_pos = -1
    for i, line in enumerate(lines):
        if line.lstrip().startswith("Channel") and "CallerID" in line:
            header_line = line
            header_pos = i
            break

    if header_pos < 0:
        return {}, {}

    callerid_col = header_line.find("CallerID")
    # Column name is "BridgeID" in newer Asterisk; find("Bridge") matches both
    bridge_col = header_line.find("BridgeID")
    if bridge_col < 0:
        bridge_col = header_line.find("Bridge")

    if callerid_col < 0 or bridge_col < 0:
        return {}, {}

    # Parse each channel data line
    channel_info = {}  # full_channel_name -> {"callerid": str, "bridge": str}

    for line in lines[header_pos + 1:]:
        stripped = line.strip()
        if not stripped or re.match(r"\d+ active", stripped):
            break
        parts = stripped.split()
        if not parts:
            continue
        channel_name = parts[0]

        # CallerID — fixed column position; grab first whitespace-delimited token
        callerid = ""
        if len(line) > callerid_col:
            field = line[callerid_col:callerid_col + 25].strip()
            callerid = field.split()[0] if field else ""

        # BridgeID — extract by column position (may be truncated by terminal width,
        # but Asterisk truncates consistently so channels in the same bridge share
        # the same truncated string — do NOT use a full-UUID regex here)
        bridge = ""
        if len(line) > bridge_col:
            bridge = line[bridge_col:].strip()

        channel_info[channel_name] = {"callerid": callerid, "bridge": bridge}

    # Group channels by bridge ID
    bridges: dict[str, list[str]] = {}
    for ch, info in channel_info.items():
        br = info["bridge"]
        if br:
            bridges.setdefault(br, []).append(ch)

    # channel_callerid_map: full channel name -> its own CallerID
    # Used by the callers-in-queue table to resolve caller IDs by channel name.
    channel_callerid_map: dict[str, str] = {
        ch: info["callerid"] for ch, info in channel_info.items() if info["callerid"]
    }

    # agent_caller_map: member interface prefix -> caller ID on the other side of bridge
    # Channel names look like PJSIP/10001605-00000123; strip the hex suffix to get the prefix.
    agent_caller_map: dict[str, str] = {}
    for ch, info in channel_info.items():
        br = info["bridge"]
        if not br:
            continue
        bridge_channels = bridges.get(br, [])
        if len(bridge_channels) < 2:
            continue
        interface = re.sub(r"-[0-9a-f]+$", "", ch)  # PJSIP/10001605
        for other_ch in bridge_channels:
            if other_ch == ch:
                continue
            other_callerid = channel_info[other_ch]["callerid"]
            if other_callerid:
                agent_caller_map[interface] = other_callerid
                break

    return agent_caller_map, channel_callerid_map


class RawTTY:
    """Context manager that puts stdin in cbreak mode for single-keypress reads.

    Falls through (active=False) when stdin is not a tty, so callers can degrade
    gracefully without special-casing piped/non-interactive input.
    """

    def __init__(self):
        self.fd = None
        self.old = None

    def __enter__(self):
        if not sys.stdin.isatty():
            return self
        try:
            self.fd = sys.stdin.fileno()
            self.old = termios.tcgetattr(self.fd)
            tty.setcbreak(self.fd)
        except (termios.error, OSError):
            self.fd = None
            self.old = None
        return self

    def __exit__(self, *exc):
        if self.fd is not None and self.old is not None:
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)

    @property
    def active(self) -> bool:
        return self.fd is not None

    def read_key(self, timeout: float | None = None) -> str | None:
        """Read one key. Returns 'UP'/'DOWN'/'LEFT'/'RIGHT'/'ENTER'/'ESC', a
        single literal character, or None on timeout/inactive.

        Reads directly from the file descriptor with os.read — sys.stdin is
        buffered, and an arrow-key sequence (\\x1b[A) arrives as one 3-byte
        burst. A buffered single-byte read would consume the ESC and leave
        '[A' in Python's buffer where select() can't see it, causing the
        sequence to be misinterpreted as a bare ESC.
        """
        if not self.active:
            return None
        if timeout is not None:
            r, _, _ = select.select([self.fd], [], [], timeout)
            if not r:
                return None
        try:
            data = os.read(self.fd, 32)
        except (OSError, BlockingIOError):
            return None
        if not data:
            return None
        if data in (b"\r", b"\n"):
            return "ENTER"
        if data == b"\x1b":
            return "ESC"
        if data.startswith(b"\x1b[") and len(data) >= 3:
            return {b"A": "UP", b"B": "DOWN", b"C": "RIGHT", b"D": "LEFT"}.get(
                data[2:3], "ESC"
            )
        try:
            return data.decode("utf-8", errors="replace")[:1]
        except Exception:
            return None


def list_queues() -> list[str]:
    """Return all queue names from 'queue show'."""
    raw = run_asterisk("queue show")
    if raw.startswith("ERROR:"):
        return []
    names = []
    for line in raw.splitlines():
        m = re.match(r"^(\S+)\s+has\s+\d+\s+calls?\s+\(max", line)
        if m:
            names.append(m.group(1))
    return names


def _select_queue_text(queues: list[str]) -> str | None:
    """Fallback numbered prompt for non-tty stdin."""
    console.print("[bold]Available queues:[/bold]")
    for i, name in enumerate(queues, 1):
        console.print(f"  [cyan]{i:>3}.[/cyan] {name}")
    while True:
        try:
            choice = console.input(
                f"\n[bold]Select queue (1-{len(queues)}) or 'q' to quit:[/bold] "
            ).strip()
        except (EOFError, KeyboardInterrupt):
            return None
        if choice.lower() in ("q", "quit", "exit"):
            return None
        try:
            idx = int(choice)
            if 1 <= idx <= len(queues):
                return queues[idx - 1]
        except ValueError:
            pass
        console.print(f"[red]Enter a number between 1 and {len(queues)}.[/red]")


def _select_queue_grid(queues: list[str], raw: "RawTTY") -> str | None:
    """Grid picker with arrow-key navigation."""
    width = console.size.width or 80
    cell_w = max(10, max(len(q) for q in queues) + 4)
    cols = max(1, min(len(queues), width // cell_w))
    rows = (len(queues) + cols - 1) // cols
    selected = 0

    from rich.console import Group

    def render() -> Panel:
        table = Table(box=box.SIMPLE, show_header=False, padding=(0, 1), expand=False)
        for _ in range(cols):
            table.add_column(justify="left")
        for r in range(rows):
            cells = []
            for c in range(cols):
                idx = r * cols + c
                if idx >= len(queues):
                    cells.append(Text(""))
                elif idx == selected:
                    cells.append(Text(f" {queues[idx]} ", style="bold black on cyan"))
                else:
                    cells.append(Text(f" {queues[idx]} "))
            table.add_row(*cells)
        content = Group(
            Text(""),
            Text("Select a queue:", style="bold white"),
            Text(""),
            table,
        )
        return Panel(
            content,
            title="[bold cyan]Asterisk Queue Monitor[/bold cyan]",
            subtitle="[dim]↑↓←→ to move • Enter to select • q or Esc to quit[/dim]",
            border_style="bright_blue",
            box=box.DOUBLE_EDGE,
        )

    with Live(render(), console=console, screen=True, auto_refresh=False) as live:
        while True:
            key = raw.read_key()
            if key in ("q", "Q", "ESC"):
                return None
            if key == "ENTER":
                return queues[selected]
            new_idx = selected
            row, col = divmod(selected, cols)
            if key == "UP" and row > 0:
                new_idx = (row - 1) * cols + col
            elif key == "DOWN" and row < rows - 1:
                cand = (row + 1) * cols + col
                if cand < len(queues):
                    new_idx = cand
            elif key == "LEFT" and selected > 0:
                new_idx = selected - 1
            elif key == "RIGHT" and selected < len(queues) - 1:
                new_idx = selected + 1
            if new_idx != selected:
                selected = new_idx
                live.update(render(), refresh=True)


def select_queue() -> str | None:
    """Prompt the user to pick a queue from the available list."""
    queues = list_queues()
    if not queues:
        console.print("[red]No queues found or unable to contact Asterisk.[/red]")
        return None
    if len(queues) == 1:
        return queues[0]
    with RawTTY() as raw:
        if raw.active:
            return _select_queue_grid(queues, raw)
        return _select_queue_text(queues)


def parse_queue_header(line: str) -> dict:
    """Parse the first line of queue show output."""
    info = {}

    m = re.match(r"^(\S+)\s+has\s+(\d+)\s+calls?\s+\(max\s+(\S+)\)", line)
    if m:
        info["name"] = m.group(1)
        info["calls"] = int(m.group(2))
        info["max"] = m.group(3)

    m = re.search(r"in\s+'([^']+)'\s+strategy", line)
    if m:
        info["strategy"] = m.group(1)

    m = re.search(r"(\d+)s\s+holdtime", line)
    if m:
        info["holdtime"] = int(m.group(1))

    m = re.search(r"(\d+)s\s+talktime", line)
    if m:
        info["talktime"] = int(m.group(1))

    m = re.search(r"W:(\d+)", line)
    if m:
        info["weight"] = int(m.group(1))

    m = re.search(r"C:(\d+)", line)
    if m:
        info["completed"] = int(m.group(1))

    m = re.search(r"A:(\d+)", line)
    if m:
        info["abandoned"] = int(m.group(1))

    m = re.search(r"SL:([\d.]+)%", line)
    if m:
        info["sl"] = float(m.group(1))

    m = re.search(r"SL2:([\d.]+)%", line)
    if m:
        info["sl2"] = float(m.group(1))

    m = re.search(r"within\s+(\d+)s", line)
    if m:
        info["sl_threshold"] = int(m.group(1))

    return info


def parse_member(line: str) -> dict:
    """Parse a member line from queue show output."""
    member = {}

    # Interface / name
    m = re.match(r"^\s+(\S+)\s+with\s+penalty\s+(\d+)", line)
    if not m:
        return {}
    member["interface"] = m.group(1)
    member["penalty"] = int(m.group(2))

    # Short name (last part after /)
    parts = member["interface"].split("/")
    member["name"] = parts[-1] if len(parts) > 1 else member["interface"]

    # Paused flag
    member["paused"] = bool(re.search(r"\bpaused\b", line, re.IGNORECASE))

    # In call flag — explicit "(in call)" token before the state parenthesis
    member["in_call"] = bool(re.search(r"\(in call\)", line, re.IGNORECASE))

    # Device state — used for eligibility; not displayed
    state_m = re.search(
        r"\((Not in use|In use|Busy|Unavailable|Ringing|On Hold|Unknown)[^)]*\)",
        line, re.IGNORECASE,
    )
    member["state"] = state_m.group(1).lower() if state_m else "not in use"

    # Explicit unavailable flag — independent of the state-alternation regex
    # above. Agents marked (Unavailable) by Asterisk (e.g. device offline,
    # logged out) must never be eligible to receive the next call, even if
    # some future Asterisk version reorders state tokens or adds qualifiers
    # that the state regex doesn't anticipate.
    member["unavailable"] = bool(re.search(r"\(Unavailable\b", line, re.IGNORECASE))

    # ringinuse
    member["ringinuse"] = not bool(re.search(r"ringinuse disabled", line, re.IGNORECASE))

    # Calls taken — handles "has taken 5 calls" and "has taken no calls yet"
    m = re.search(r"has taken\s+(\d+)\s+calls?", line)
    member["calls_taken"] = int(m.group(1)) if m else 0

    # Last call
    m = re.search(r"last was\s+(\d+)\s+secs?\s+ago", line)
    member["last_call_secs"] = int(m.group(1)) if m else None

    # Login time
    m = re.search(r"login was\s+(\d+)\s+secs?\s+ago", line)
    member["login_secs"] = int(m.group(1)) if m else None

    # Generic pause reason — e.g. "paused:last ringing was 27 secs ago"
    #                                "paused:last lunch was 136 secs ago"
    #                                "paused:last not available was 1526 secs ago"
    m = re.search(r"paused[:;]last\s+(.+?)\s+was\s+(\d+)\s+secs?\s+ago", line, re.IGNORECASE)
    if m:
        member["pause_reason"] = m.group(1).strip()
        member["pause_secs"] = int(m.group(2))
    else:
        member["pause_reason"] = None
        member["pause_secs"] = None

    return member


def parse_caller(line: str) -> dict:
    """Parse a caller/channel line.

    Handles both formats:
      1. PJSIP/CISCO_CUBE-00000407 (wait: 0:04, prio: 0)
      1. SIP/1234 (callerid) [6 secs]
    """
    caller = {}

    # Format: N. CHANNEL (wait: M:SS, prio: N)
    m = re.match(r"^\s+(\d+)\.\s+(\S+)\s+\(wait:\s*(\d+):(\d+),\s*prio:\s*(\d+)\)", line)
    if m:
        caller["position"] = int(m.group(1))
        caller["channel"] = m.group(2)
        caller["callerid"] = ""
        caller["wait_secs"] = int(m.group(3)) * 60 + int(m.group(4))
        caller["priority"] = int(m.group(5))
        return caller

    # Fallback format: N. CHANNEL (caller id) [seconds wait]
    m = re.match(r"^\s+(\d+)\.\s+(\S+)\s+\(([^)]*)\)\s+\[(\d+)\s+secs?\]", line)
    if m:
        caller["position"] = int(m.group(1))
        caller["channel"] = m.group(2)
        caller["callerid"] = m.group(3)
        caller["wait_secs"] = int(m.group(4))
        caller["priority"] = 0
    return caller


def secs_to_human(secs: int | None) -> str:
    if secs is None:
        return "-"
    if secs < 60:
        return f"{secs}s"
    elif secs < 3600:
        return f"{secs // 60}m{secs % 60:02d}s"
    else:
        h = secs // 3600
        m = (secs % 3600) // 60
        return f"{h}h{m:02d}m"


def sl_color(sl: float) -> str:
    if sl >= 80:
        return "green"
    elif sl >= 60:
        return "yellow"
    return "red"


_STATE_DISPLAY = {
    "not in use": ("Not in use", "green"),
    "in use":     ("In use",     "yellow"),
    "busy":       ("Busy",       "yellow"),
    "ringing":    ("Ringing",    "yellow"),
    "on hold":    ("On Hold",    "yellow"),
    "unavailable":("Unavailable","red"),
    "unknown":    ("Unknown",    "bright_black"),
}


def state_style(state: str) -> tuple[str, str]:
    """Return (display label, rich style) for a device state."""
    return _STATE_DISPLAY.get((state or "").lower(), (state or "?", "bright_black"))


def parse_output(raw: str) -> dict:
    """Parse full asterisk queue show output into structured data."""
    data = {"header": {}, "members": [], "callers": [], "error": None}

    if raw.startswith("ERROR:"):
        data["error"] = raw
        return data

    lines = raw.splitlines()
    section = None

    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue

        # Header line (queue name line)
        if re.match(r"^\S+\s+has\s+\d+\s+calls?", line):
            data["header"] = parse_queue_header(line)
            continue

        if stripped.lower() == "members:":
            section = "members"
            continue

        if stripped.lower() in ("callers:", "no callers."):
            section = "callers"
            if stripped.lower() == "no callers.":
                data["callers"] = []
            continue

        if section == "members" and re.match(r"^\s+\S+\s+with\s+penalty", line):
            m = parse_member(line)
            if m:
                data["members"].append(m)
            continue

        if section == "callers":
            c = parse_caller(line)
            if c:
                data["callers"].append(c)

    return data


_BUSY_STATES = {"in use", "busy", "ringing", "on hold", "unavailable"}


def determine_next_agents(members: list, strategy: str) -> tuple[set, bool]:
    """
    Predict which agent(s) will receive the next call.

    Returns (next_set, is_predictable):
      next_set       — interface names of the predicted next agent(s)
      is_predictable — False for strategies where we cannot predict (random, etc.)

    Eligible = not paused, not in a call, not (Unavailable), device state is idle.
    """
    strategy = (strategy or "").lower().strip()

    eligible = [
        m for m in members
        if not m.get("paused")
        and not m.get("in_call")
        and not m.get("unavailable")
        and m.get("state", "not in use") not in _BUSY_STATES
    ]

    predictable_strategies = {"ringall", "leastrecent", "fewestcalls", "linear"}
    is_predictable = strategy in predictable_strategies

    if not eligible or not is_predictable:
        # Return the eligible set so callers can show "?" for unpredictable strategies
        return set(), is_predictable

    if strategy == "ringall":
        return {m["interface"] for m in eligible}, True

    if strategy == "leastrecent":
        # Agent who went longest without a call goes first.
        # None (never taken a call) is treated as infinity — highest priority.
        winner = max(
            eligible,
            key=lambda m: m["last_call_secs"] if m["last_call_secs"] is not None else float("inf"),
        )
        return {winner["interface"]}, True

    if strategy == "fewestcalls":
        winner = min(eligible, key=lambda m: m.get("calls_taken", 0))
        return {winner["interface"]}, True

    if strategy == "linear":
        # Members are already ordered by penalty in queue show output;
        # the first eligible one (lowest penalty, earliest in list) wins.
        return {eligible[0]["interface"]}, True

    return set(), True


def build_display(
    data: dict,
    queue_name: str,
    refresh_count: int,
    caller_map: dict | None = None,
    channel_callerid_map: dict | None = None,
) -> Table:
    """Build a rich renderable from parsed queue data."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if data.get("error"):
        return Panel(
            Text(data["error"], style="bold red"),
            title=f"[bold]Queue: {queue_name}[/bold]",
            subtitle=f"Last update: {now}",
        )

    h = data["header"]
    queue_display_name = h.get("name", queue_name)

    # ── Header stats ──────────────────────────────────────────────
    sl = h.get("sl")
    sl_str = f"[{sl_color(sl)}]{sl:.1f}%[/]" if sl is not None else "-"
    sl2 = h.get("sl2")
    sl2_str = f"[{sl_color(sl2)}]{sl2:.1f}%[/]" if sl2 is not None else "-"
    sl_thr = h.get("sl_threshold", "?")

    header_text = Text.assemble(
        ("Queue: ", "bold white"),
        (queue_display_name, "bold cyan"),
        "  |  ",
        ("Calls in queue: ", ""),
        (str(h.get("calls", 0)), "bold yellow"),
        f"/{h.get('max', '?')}",
        "  |  ",
        ("Strategy: ", ""),
        (h.get("strategy", "?"), "italic"),
        "  |  ",
        ("Holdtime: ", ""),
        (secs_to_human(h.get("holdtime")), ""),
        "  |  ",
        ("Talktime: ", ""),
        (secs_to_human(h.get("talktime")), ""),
        "\n",
        ("Completed: ", ""),
        (str(h.get("completed", 0)), "green"),
        "  |  ",
        ("Abandoned: ", ""),
        (str(h.get("abandoned", 0)), "red"),
        "  |  ",
        (f"SL (within {sl_thr}s): ", ""),
        sl_str,
        "  |  ",
        ("SL2: ", ""),
        sl2_str,
    )

    # ── Next-agent prediction ─────────────────────────────────────
    strategy = h.get("strategy", "")
    next_set, is_predictable = determine_next_agents(data["members"], strategy)

    # Eligible agents for "?" marking in unpredictable strategies
    eligible_interfaces = {
        m["interface"] for m in data["members"]
        if not m.get("paused") and not m.get("in_call")
        and not m.get("unavailable")
        and m.get("state", "not in use") not in _BUSY_STATES
    }

    # ── Members table ─────────────────────────────────────────────
    members_table = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold magenta",
        expand=True,
        title="[bold]Members[/bold]",
        title_style="bold white",
    )
    members_table.add_column("Agent", style="cyan", no_wrap=True)
    members_table.add_column("Penalty", justify="center")
    members_table.add_column("State", justify="left", no_wrap=True)
    members_table.add_column("Next", justify="center")
    members_table.add_column("In Call", justify="center")
    members_table.add_column("Talking To", style="magenta", no_wrap=True)
    members_table.add_column("Paused", justify="center")
    members_table.add_column("Pause Reason", justify="left")
    members_table.add_column("Pause Since", justify="right")
    members_table.add_column("Calls Taken", justify="right")
    members_table.add_column("Last Call", justify="right")
    members_table.add_column("Login", justify="right")

    if data["members"]:
        for m in sorted(data["members"], key=lambda x: (x.get("penalty", 0), x.get("name", ""))):
            paused = m.get("paused", False)
            in_call = m.get("in_call", False)
            interface = m.get("interface", "")

            in_call_txt = Text("Yes", style="bold red") if in_call else Text("No", style="bright_black")

            # Look up caller ID from the channel bridge map
            talking_to = ""
            if caller_map and in_call:
                talking_to = caller_map.get(interface, "")
            talking_to_txt = Text(talking_to, style="bold magenta") if talking_to else Text("-", style="bright_black")

            pause_reason = (m.get("pause_reason") or "").upper()
            pause_since = secs_to_human(m.get("pause_secs")) if m.get("pause_secs") is not None else "-"

            # Next column
            if interface in next_set:
                next_txt = Text("★ Next", style="bold green")
            elif not is_predictable and interface in eligible_interfaces:
                next_txt = Text("?", style="yellow")
            else:
                next_txt = Text("-", style="bright_black")

            state_label, state_color = state_style(m.get("state", "not in use"))
            state_txt = Text(state_label, style=state_color)

            members_table.add_row(
                m.get("name", m.get("interface", "?")),
                str(m.get("penalty", 0)),
                state_txt,
                next_txt,
                in_call_txt,
                talking_to_txt,
                "[yellow]Yes[/yellow]" if paused else "[green]No[/green]",
                Text(pause_reason, style="yellow") if pause_reason else Text("-", style="bright_black"),
                pause_since,
                str(m.get("calls_taken", 0)),
                secs_to_human(m.get("last_call_secs")),
                secs_to_human(m.get("login_secs")),
            )
    else:
        members_table.add_row("[dim]No members[/dim]", "", "", "", "", "", "", "", "", "", "", "")

    # ── Callers table ─────────────────────────────────────────────
    callers_table = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="bold magenta",
        expand=True,
        title="[bold]Callers in Queue[/bold]",
        title_style="bold white",
    )
    callers_table.add_column("#", justify="center", width=4)
    callers_table.add_column("Channel", style="cyan")
    callers_table.add_column("Caller ID")
    callers_table.add_column("Priority", justify="center")
    callers_table.add_column("Wait Time", justify="right")

    if data["callers"]:
        for c in data["callers"]:
            wait = c.get("wait_secs", 0)
            wait_color = "green" if wait < 60 else ("yellow" if wait < 180 else "red")
            # CallerID: prefer what was parsed inline; fall back to channel lookup
            callerid = c.get("callerid", "") or ""
            if not callerid and channel_callerid_map:
                callerid = channel_callerid_map.get(c.get("channel", ""), "")
            callers_table.add_row(
                str(c.get("position", "?")),
                c.get("channel", "?"),
                callerid or "-",
                str(c.get("priority", 0)),
                Text(secs_to_human(wait), style=wait_color),
            )
    else:
        callers_table.add_row("", "[dim]No callers[/dim]", "", "", "")

    from rich.columns import Columns
    from rich.console import Group

    content = Group(
        Panel(header_text, box=box.ROUNDED, border_style="blue"),
        members_table,
        callers_table,
    )

    return Panel(
        content,
        title=f"[bold cyan]Asterisk Queue Monitor[/bold cyan]",
        subtitle=f"[dim]Last update: {now}  |  Refresh #{refresh_count}  |  'b' = back to queue list  •  'q' = quit[/dim]",
        border_style="bright_blue",
        box=box.DOUBLE_EDGE,
    )


def run_monitor(queue_name: str, interval: float) -> str:
    """Run the live status loop. Returns 'QUIT' or 'BACK'."""
    refresh_count = 0
    with RawTTY() as raw, Live(console=console, screen=True, refresh_per_second=1) as live:
        while True:
            raw_queue = run_asterisk(f"queue show {queue_name}")
            raw_channels = run_asterisk("core show channels verbose")
            data = parse_output(raw_queue)
            agent_caller_map, channel_callerid_map = parse_core_channels(raw_channels)
            refresh_count += 1
            renderable = build_display(data, queue_name, refresh_count, agent_caller_map, channel_callerid_map)
            live.update(renderable)

            # Sleep up to `interval` seconds, but wake on 'q' (quit) or 'b' (back).
            end = time.monotonic() + interval
            while True:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                if raw.active:
                    key = raw.read_key(timeout=min(0.25, remaining))
                    if key in ("q", "Q"):
                        return "QUIT"
                    if key in ("b", "B"):
                        return "BACK"
                else:
                    time.sleep(remaining)
                    break


def main():
    parser = argparse.ArgumentParser(description="Asterisk Queue Status Monitor")
    parser.add_argument(
        "queue", nargs="?", help="Queue name (e.g. sales, support, 1000). If omitted, pick from a list."
    )
    parser.add_argument(
        "--interval", "-i", type=float, default=2.0, help="Refresh interval in seconds (default: 2)"
    )
    args = parser.parse_args()

    queue_name = args.queue
    interval = max(0.5, args.interval)

    while True:
        if not queue_name:
            queue_name = select_queue()
            if not queue_name:
                return
        if run_monitor(queue_name, interval) != "BACK":
            return
        queue_name = None


if __name__ == "__main__":
    main()
