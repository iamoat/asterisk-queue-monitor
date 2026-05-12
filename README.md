# asterisk-queue-monitor

A terminal dashboard that displays the real-time status of an Asterisk call queue — refreshing every 2 seconds.

## Features

- **Queue summary** — active calls, strategy, holdtime, talktime, completed, abandoned, SL%, SL2%
- **Members table**
  - Who is on a call and the caller's phone number (via `core show channels verbose`)
  - Pause status, pause reason (RINGING, LUNCH, BREAK, NOT AVAILABLE, …), and how long paused
  - Calls taken and time since last call
  - **Next-call prediction** based on queue strategy:
    - `leastrecent` — agent who waited longest since their last call
    - `fewestcalls` — agent with the fewest completed calls
    - `linear` — first available agent in list order by penalty
    - `ringall` — all available agents highlighted
    - `random` / `wrandom` / `roundrobin` / `rrmemory` — eligible agents marked `?`
- **Callers in queue** — channel, caller ID (looked up from active channels), wait time, priority

## Requirements

- Python 3.10+
- [rich](https://github.com/Textualize/rich) library
- Asterisk CLI accessible as `asterisk -rx`

## Installation

```bash
pip install rich
```

## Usage

```bash
python3 queue_status.py <queue_name>

# Custom refresh interval (default: 2 seconds)
python3 queue_status.py <queue_name> --interval 5
python3 queue_status.py <queue_name> -i 3
```

**Example:**
```bash
python3 queue_status.py QUEUE_1000_65
```

Press `Ctrl+C` to quit.

## How it works

Each refresh cycle runs two Asterisk CLI commands:

1. `queue show <queue>` — member states, pause reasons, call counts
2. `core show channels verbose` — active channels with CallerID and BridgeID

Channels are matched by BridgeID to determine which caller each agent is currently speaking with. The BridgeID may be truncated by terminal width; the script handles this correctly by using column-position extraction rather than a full UUID regex.
