# lane-live

Deploy `lane_live.py` and `verify_gate.py` under `~/lanes/omp-runs/live/bin` with the local dispatcher helpers.
Keep deployment values in `~/lanes/omp-runs/live/config.json` with mode `0600`:

| Key | Value |
| --- | --- |
| `repo` | Absolute path to the repository being verified (`~/` is allowed) |
| `lane_ids` | Object mapping `A`, `B`, and `C` to their distinct lane IDs |
| `caller` | Caller name passed to `omp-dispatch` |
| `coordinator_subscriber_id` | ID of the configured verifier webhook subscriber |
| `codex_model` | Installed model ID in the `openai-codex/` or `openai/` family |
| `verify_instructions` | Optional repository-specific instructions appended to the verifier prompt |

Set `FORCE_REVERIFY` to a comma-separated list of PR numbers if needed.

- `verify_gate.py`: the auto-verify gate. A sign-list PR head is verified only when no live omp author job (`running`/`waiting_input`, not on a `verify-*` lane) is on its branch, and active omp jobs are under the Mac cap (6; 4 at 1-min load >= 25; none at load >= 40). Skips log `VERIFY-WAIT author live job=<id>` or `VERIFY-WAIT cap n/cap load=x` in `live/events.log`.
- Test: `python3 test_verify_gate.py` (or `pytest`).
- Dry run against live state: `LANE_LIVE_DRY_RUN=1 python3 lane_live.py` prints events to stdout without changing live state or dispatching. Add `LANE_LIVE_SIM_DONE=<job-id prefix>` to treat a job as finished.
- A Kimi author gets the configured Codex model; every other author gets `kimi-code/k3.1-pro`. The executor refuses an unpinned or same-family verifier, and the prompt requires SIGNOFF.md to include a `Verifier family:` line.
