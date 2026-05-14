# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Single-file Python terminal dashboard ([queue_status.py](queue_status.py)) that polls an Asterisk PBX over its CLI and renders live queue state with [rich](https://github.com/Textualize/rich).

## Running

```bash
pip install rich
python3 queue_status.py <queue_name>            # default 2s refresh
python3 queue_status.py <queue_name> -i 5       # custom interval
```

Requires the `asterisk` binary on PATH (the script shells out to `asterisk -rx ...`). There is no build, test, or lint setup — it is a single script.

## Architecture

Each refresh cycle in `main()` runs two Asterisk CLI commands and cross-references their output:

1. `queue show <queue>` → parsed by `parse_output` → header stats, members, callers in queue.
2. `core show channels verbose` → parsed by `parse_core_channels` → two maps:
   - `agent_caller_map`: member interface prefix (`PJSIP/10001605`) → caller ID on the other end of the bridge. Built by grouping channels by BridgeID, then for each member-side channel looking up the *other* channel in the same bridge and taking its CallerID.
   - `channel_callerid_map`: full channel name → its own CallerID. Used by the callers-in-queue table when the inline CallerID is missing.

`build_display` is a pure function from parsed data to a `rich` renderable; `Live` redraws it each tick.

### Parsing gotchas (don't regress these)

- **BridgeID column is extracted by character position, not regex.** Asterisk truncates the BridgeID to fit terminal width, but truncates *consistently*, so channels sharing a bridge still share the same truncated string. A full-UUID regex would fail to match truncated IDs — see the comment at [queue_status.py:98-100](queue_status.py#L98-L100).
- **Channel name → interface prefix** is done by stripping the trailing `-[0-9a-f]+` hex suffix (e.g. `PJSIP/10001605-00000123` → `PJSIP/10001605`).
- **Caller line has two formats** (`parse_caller`): `(wait: M:SS, prio: N)` and the legacy `(callerid) [N secs]`. Keep both.
- **Pause reason regex** matches `paused[:;]last <reason> was N secs ago` — the reason is free-text (RINGING, LUNCH, NOT AVAILABLE, …), so don't enumerate it.

### Next-call prediction

`determine_next_agents` returns `(next_set, is_predictable)`. Eligibility = not paused, not in call, device state not in `_BUSY_STATES`. Predictable strategies (`ringall`, `leastrecent`, `fewestcalls`, `linear`) get a `★ Next` marker; unpredictable ones (`random`, `wrandom`, `roundrobin`, `rrmemory`) get `?` on every eligible agent. `linear` relies on the fact that `queue show` already orders members by penalty.
