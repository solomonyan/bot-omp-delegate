# lane-live (deployed copy)

This is the deployed copy of `lane_live.py` that runs at `~/lanes/omp-runs/live/bin` on Solomon's Mac (launchd `com.solomon.lane-live`, every 180 s). It is tracked here for review only. Deploy by copying `lane_live.py` and `verify_gate.py` into that directory.

- `verify_gate.py`: the auto-verify gate. A sign-list PR head is verified only when no live omp author job (`running`/`waiting_input`, not on a `verify-*` lane) is on its branch, and active omp jobs are under the Mac cap (6; 4 at 1-min load >= 25; none at load >= 40). Skips log `VERIFY-WAIT author live job=<id>` or `VERIFY-WAIT cap n/cap load=x` in `live/events.log`.
- Test: `python3 test_verify_gate.py` (or `pytest`).
- Dry run against live state: `LANE_LIVE_DRY_RUN=1 python3 lane_live.py` prints events to stdout and writes or dispatches nothing. Add `LANE_LIVE_SIM_DONE=<job-id prefix>` to treat a job as finished.
- Verifier family: before each verify, lane_live pins the verify session to a different model family from the author's session model with `omp-delegate session configure --model`. A Kimi author gets `openai-codex/gpt-6.1-sol`; every other author (Codex, Claude, Grok, unknown) gets `kimi-code/k3.1-pro`. Claude is never a verifier. It refuses an unpinned or same-family verify, and the prompt requires SIGNOFF.md to include a `Verifier family:` line.
