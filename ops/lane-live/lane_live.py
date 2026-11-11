#!/usr/bin/env python3
"""lane_live — Mac-side executor for the print-only dryrun scripts (README step 3: execute OUTSIDE the scripts).
Runs every 3 min under launchd (com.solomon.lane-live). Per lane A/B/C: acts only if LIVE/<L>.live exists AND
the lane has no activeJob. Runs lane_dispatch.py --dry-run and executes only its WOULD-RUN lines for live lanes.
Verify: if LIVE/verify.live exists, runs lane_verify.py --dry-run and dispatches at most 1 new verify per tick,
max MAX_VERIFY concurrent verify-* jobs. Logs every action to LIVE/events.log (HKT).
2026-11-11 autoverify gate (verify_gate.py): a PR head is auto-verified only when no live omp author job is on its
branch AND active omp jobs are under the Mac cap (6; 4 at load1>=25; none at load1>=40). Skips log VERIFY-WAIT.
Dry run: LANE_LIVE_DRY_RUN=1 runs the verify decision against live state, prints events to stdout, writes and
dispatches nothing (LANE_LIVE_SIM_DONE=<job-id prefixes> treats those jobs as finished).
2026-11-11 verifier family: each verify session is pinned (session configure --model) to a different family from the
author's session model: Codex author -> kimi-code/k3.1-pro (Claude if LIVE/verifier-kimi.off exists), Kimi/Claude -> Codex."""
import json, os, re, subprocess, sys, datetime as dt, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__))); import verify_gate
DRY = os.environ.get("LANE_LIVE_DRY_RUN") == "1"
SIM_DONE = tuple(x for x in os.environ.get("LANE_LIVE_SIM_DONE", "").split(",") if x) if DRY else ()
HKT = dt.timezone(dt.timedelta(hours=8)); H = os.path.expanduser("~")
LIVE = f"{H}/lanes/omp-runs/live"; BIN = f"{LIVE}/bin"; Q = f"{H}/lanes/omp-runs/queue"
OMP = f"{H}/Workspace/bot-omp-delegate/scripts/omp-delegate"; REPO = f"{H}/Workspace/3d-ui-playground/wellness-ops"
IDS = {"A": "c986101dd2034bb29b0c42d72248f760", "B": "a4e999d856eb4ca395d7fa8fc6eb87e5", "C": "561a3d901e9141a1b7782c85b5de0c72"}
MAX_VERIFY = int(os.environ.get("MAX_VERIFY", "2"))
os.environ["PATH"] = f"{H}/Workspace/bot-omp-delegate/scripts:{H}/.local/bin:{H}/.bun/bin:/opt/homebrew/bin:" + os.environ["PATH"]
now = dt.datetime.now(HKT); NOW = now.strftime("%Y-%m-%d %H:%M:%S HKT")
def ev(msg):
    if DRY: print(f"DRYRUN {NOW} {msg}"); return
    open(f"{LIVE}/events.log", "a").write(f"{NOW} {msg}\n")
def sh(cmd, **k): return subprocess.run(cmd, capture_output=True, text=True, timeout=300, **k)
def jl(p, d):
    try: return json.load(open(p))
    except Exception: return d
st = jl(f"{LIVE}/state.json", {"idle_since": {}, "last_job": {}, "forced_done": []})
if os.path.exists(f"{H}/lanes/omp-runs/DRAIN-HOLD"):
    ev("HOLD DRAIN-HOLD present; no dispatch"); sys.exit(0)
# Apply only after the compact event writer has been deployed by Solomon.
# Retain the log-size warning and idle gzip; interrupt only below the dispatch floor.
import shutil as _sh
_MIN_GB = float(os.environ.get("LANE_MIN_FREE_GB", "25"))
_FREE = _sh.disk_usage(H).free / 1e9
_J = f"{H}/.omp-delegate/jobs"; _WARN = float(os.environ.get("JOB_LOG_WARN_GB", "5"))
os.makedirs(f"{LIVE}/logcap", exist_ok=True)
_gz_busy = subprocess.run(["pgrep", "-x", "gzip"], capture_output=True).returncode == 0
for _p in ([] if DRY else glob.glob(f"{_J}/*.jsonl")):
    try: _s = os.path.getsize(_p) / 1e9; _age = now.timestamp() - os.path.getmtime(_p)
    except OSError: continue
    _m = jl(_p[:-1], {}); _id = os.path.basename(_p)[:-6]; _stt = _m.get("status")
    if _stt in ("running", "queued", "starting"):
        if _FREE < _MIN_GB and not os.path.exists(f"{LIVE}/logcap/{_id}.cap"):
            sh([OMP, "interrupt", _id]); open(f"{LIVE}/logcap/{_id}.cap", "w").write(NOW + "\n")
            ev(f"DISK-CAP job {_id[:8]} free {_FREE:.1f}GB < {_MIN_GB:g}GB: interrupted (resume in same session)")
        elif _s > _WARN and not os.path.exists(f"{LIVE}/logcap/{_id}.warn"):
            open(f"{LIVE}/logcap/{_id}.warn", "w").write(NOW + "\n"); ev(f"LOG-WARN job {_id[:8]} log {_s:.1f}GB > {_WARN:g}GB")
    elif _stt in ("completed", "error", "aborted", "interrupted") and _age > 7200 and not _gz_busy and not os.path.exists(_p + ".gz"):
        subprocess.Popen(["gzip", "-6", _p], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        _gz_busy = True; ev(f"LOG-GZ job {_id[:8]} {_s:.2f}GB idle {_age/3600:.1f}h: gzip started")
if _FREE < _MIN_GB:
    ev(f"DISK-LOW free={_FREE:.1f}GB < {_MIN_GB:g}GB: dispatch refused (lane_live.py)")
    DRY or open(f"{LIVE}/heartbeat", "w").write(NOW + "\n"); sys.exit(0)
r = sh([OMP, "lane", "list"]); d = json.loads(r.stdout); lanes = d if isinstance(d, list) else d.get("lanes", d)
byid = {l["id"]: l for l in lanes}
ls = {"updated_at": now.isoformat(), "lanes": {}, "side_lanes": []}
for L, i in IDS.items():
    l = byid.get(i, {}); job = l.get("activeJob")
    if job: st["idle_since"].pop(L, None); st["last_job"][L] = job; state = "running"
    else:
        st["idle_since"].setdefault(L, now.isoformat()); state = "idle"
    ls["lanes"][L] = {"state": state, "lane_id": i}
for l in lanes:
    if l["id"] in IDS.values(): continue
    ls["side_lanes"].append({"name": l["name"], "lane_id": l["id"], "state": "running" if l.get("activeJob") else "idle", "job_id": l.get("activeJob") or ""})
LS = "/tmp/lane-status.dryrun.json" if DRY else f"{LIVE}/lane-status.mac.json"
json.dump(ls, open(LS, "w"), indent=1)
live = [L for L in IDS if os.path.exists(f"{LIVE}/{L}.live")]
for L in live:
    if not DRY and ls["lanes"][L]["state"] == "idle" and not os.path.exists(f"{LIVE}/{L}.switched"):
        open(f"{LIVE}/{L}.switched", "w").write(NOW); ev(f"SWITCH Lane {L} now script-owned (idle_since {st['idle_since'][L]}, last job {st['last_job'].get(L,'?')[:8]})")
# --- dispatch
if not DRY and any(ls["lanes"][L]["state"] == "idle" for L in live):
    r = sh(["python3", f"{BIN}/lane_dispatch.py", "--dry-run", "--lane-status", f"{LIVE}/lane-status.mac.json", "--flags", "/dev/null",
            "--queue-dir", Q, "--mac-queue-root", Q, "--drain-hold", "absent", "--lane-ids", ",".join(f"{k}={v}" for k, v in IDS.items())])
    out = r.stdout.splitlines()
    for n, line in enumerate(out):
        m = re.match(r"WOULD-RUN Lane ([ABC]) <- (\S+)", line)
        if not m or m.group(1) not in live: continue
        L, rel = m.groups(); cmd = out[n + 1].split(": ", 1)[1]
        res = sh(["bash", "-c", cmd]); ok = res.returncode == 0
        src = f"{Q}/{rel}"
        if ok: os.makedirs(f"{Q}/dispatched", exist_ok=True); os.rename(src, f"{Q}/dispatched/{L.lower()}-{now:%m%d%H%M}-{os.path.basename(rel)}")
        idle = st["idle_since"].get(L); gap = int((now - dt.datetime.fromisoformat(idle)).total_seconds() // 60) if idle else -1
        ev(f"DISPATCH Lane {L} {'OK' if ok else 'FAIL rc=%d' % res.returncode} prompt={rel} idle_since={idle} gap_min={gap} out={(res.stdout+res.stderr).strip()[-200:]!r}")
        if ok: st["idle_since"].pop(L, None)
# --- verify
if DRY or os.path.exists(f"{LIVE}/verify.live"):
    running = [s for s in ls["side_lanes"] if s["name"].startswith("verify-") and s["state"] == "running"]
    env = dict(os.environ, FORCE_REVERIFY=",".join(x for x in os.environ.get("FORCE_REVERIFY", "450,454").split(",") if x and x not in st["forced_done"]))
    r = sh(["python3", f"{BIN}/lane_verify.py", "--dry-run", "--lane-status", LS, "--prompt-root", f"{Q}/verify"], env=env)
    need = re.findall(r"^NEED #(\d+) (\S+)@([0-9a-f]{8}) draft=\S+ author=(.+?) verifier=(\S+) Check=", r.stdout, re.M)
    if r.returncode: ev(f"VERIFY-ERR lane_verify rc={r.returncode} {r.stderr.strip()[-200:]!r}")
    if need and len(running) >= MAX_VERIFY: need = []
    if need:  # autoverify gate on the one head this tick would take: wait for the author's run.done and a free Mac job slot
        ok, why = verify_gate.decide(need[0][1], verify_gate.load_jobs(_J, byid, SIM_DONE), os.getloadavg()[0])
        if not ok or DRY: ev(f"{why} #{need[0][0]} {need[0][1]}@{need[0][2]}")
        if not ok: need = []
    for num, branch, sha, author, vname in need[:1]:
        sh(["git", "-C", REPO, "fetch", "-q", "origin", branch])
        full = sh(["git", "-C", REPO, "rev-parse", "FETCH_HEAD"]).stdout.strip()
        if any(s["name"].startswith(vname) and s["state"] == "running" for s in ls["side_lanes"]): continue
        name = vname; lane_name = f"{name}-{sha}"
        # 2026-11-01 19:20: match the create name (name-sha), not bare vname — bare match caused 409 spam on #450
        # 2026-11-10 (Solomon 13:50 HKT via JARVIS): verify jobs run yolo, labeled omp-caller:friday, coordinator
        # jarvis-omp-events, and REUSE the PR's existing verify lane/session instead of one write-mode lane per head.
        vh = st.setdefault("verified_heads", {})
        if vh.get(num) == sha:
            ev(f"VERIFY-SKIP #{num} @{sha} already dispatched on this head"); continue
        if any(s["name"] == lane_name for s in ls["side_lanes"]) and num not in vh:
            vh[num] = sha; ev(f"VERIFY-SKIP #{num} {lane_name} already exists (seeded verified_heads)"); continue
        amodel = verify_gate.author_model(branch, lanes, f"{H}/.omp-delegate/sessions")
        vmodel, vfam = verify_gate.verifier_for(amodel, kimi_ok=not os.path.exists(f"{LIVE}/verifier-kimi.off"))
        afam = verify_gate.family(amodel)
        if DRY: ev(f"VERIFY-DRYRUN author_model={amodel or '?'} ({afam}) verifier_model={vmodel} ({vfam})")
        if DRY: ev(f"VERIFY-DRYRUN would dispatch #{num} {branch}@{sha} verifier={vname} (nothing dispatched)"); break
        prev = [s for s in ls["side_lanes"] if s["name"].startswith(f"{vname}-")]
        if prev:
            lid = prev[-1]["lane_id"]
            lj = json.loads(sh([OMP, "lane", "show", lid]).stdout or "{}"); wt = lj.get("cwd") or ""
            if lj.get("activeJob"): continue
            sh(["git", "-C", wt, "fetch", "-q", "origin", branch])
            co = sh(["git", "-C", wt, "checkout", "-q", "--detach", full])
            if co.returncode: ev(f"VERIFY-FAIL #{num} reuse {prev[-1]['name']} checkout {full[:8]} failed {co.stderr.strip()[-200:]!r}"); break
        else:
            wt = f"{H}/lanes/{lane_name}"
            if not os.path.isdir(wt): sh(["git", "-C", REPO, "worktree", "add", "--detach", wt, full])
            cr = sh([OMP, "lane", "create", "--name", lane_name, "--cwd", wt, "--approval-mode", "yolo"])
            try: lid = json.loads(cr.stdout).get("id") or json.loads(cr.stdout)["lane"]["id"]
            except Exception: ev(f"VERIFY-FAIL #{num} lane create failed {cr.stderr.strip()[-200:]!r}"); break
            lj = json.loads(sh([OMP, "lane", "show", lid]).stdout or "{}")
        sid = lj.get("sessionId")
        if sid: sh([OMP, "session", "configure", "--approval-mode", "yolo", "--model", vmodel, sid])
        opts = (json.loads(sh([OMP, "session", "show", sid]).stdout or "{}").get("options") or {}) if sid else {}
        mode = opts.get("approval_mode")
        if mode != "yolo": ev(f"VERIFY-FAIL #{num} lane={lid} session={sid} approval_mode={mode} (refusing non-yolo verify)"); break
        if opts.get("model") != vmodel or vfam == afam: ev(f"VERIFY-FAIL #{num} lane={lid} session={sid} model={opts.get('model')} author={amodel} (refusing same-family/unpinned verify)"); break
        # 2026-11-02 VISION: side lanes must notify MELCHIOR; no-op while the subscriber covers all lanes
        try:
            wr = sh([sys.executable, f"{BIN}/webhook_register.py", lane_name]).stdout.strip()
            if wr != "covered-all-lanes": ev(f"WEBHOOK-REGISTER {lane_name} {wr}")
        except Exception as e: ev(f"WEBHOOK-REGISTER {lane_name} error {e!r}")
        os.makedirs(f"{Q}/verify", exist_ok=True); p = f"{Q}/verify/{name}-{sha}-prompt.md"
        open(p, "w").write(f"""Must-have: independent verify of #{num} at {sha}; verdict PASS|FAIL bound to {sha}
You are an independent verifier (author lane: {author}).
Model family: you run on {vmodel} (family {vfam}); the author ran on {amodel or 'unknown'} (family {afam}). SIGNOFF.md must include the line "Verifier family: {vfam} ({vmodel}); author family: {afam} ({amodel or 'unknown'})". If your own model is not {vfam}, stop and report VERDICT FAIL (wrong verifier family). Worktree {wt} is detached at {full}. Do not edit product code, do not push, never force-push, never push to main, never print secrets.
Review the diff vs origin/main, then run targeted tests for the touched packages (the full suite runs as the box Check; do not run it here).
DB: No Docker on this Mac (Solomon, 10 Nov). Never run docker, open Docker Desktop, or pnpm verify:local, and do not start any real Postgres (Docker or Homebrew initdb/pg_ctl). Use PGLite only: pnpm check:static; env -u DATABASE_URL -u TEST_DATABASE_URL -u CI node --import tsx scripts/test-runner.ts (postgresql-engine tests skip by design); env -u DATABASE_URL pnpm db:check; the scripts/*-ui-smoke.ts smokes (already PGLite); pnpm build. pnpm check / pnpm test need TEST_DATABASE_URL: skip them and report that the real-PostgreSQL gate was not run locally (GitHub CI runs it). If a check truly needs real Postgres with no PGLite path, stop and report it instead.
Finish with one line: VERDICT PASS|FAIL #{num} @{sha} patch-id <git patch-id of the diff> plus evidence. Post nothing to GitHub; the Eng Lead posts it.
"""
            + sh([sys.executable, f"{BIN}/signoff_gate.py", "brief", "--pr", str(num), "--sha", full]).stdout)
        res = sh([f"{H}/bin/omp-dispatch", "--caller", "friday", lid, p, "2"], env=dict(os.environ, OMP_COORDINATOR_SUBSCRIBER_ID="jarvis-omp-events")); ok = res.returncode == 0
        if ok: vh[num] = sha
        if ok and num in ("450", "454"): st["forced_done"].append(num)
        ev(f"VERIFY #{num} {branch}@{sha} lane={lid} yolo label=omp-caller:friday {'OK' if ok else 'FAIL rc=%d' % res.returncode} running_verifies={len(running)} out={(res.stdout+res.stderr).strip()[-200:]!r}")
if not DRY:
    json.dump(st, open(f"{LIVE}/state.json", "w"), indent=1)
    open(f"{LIVE}/heartbeat", "w").write(NOW + "\n")
