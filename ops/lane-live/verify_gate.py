"""verify_gate — decide whether lane_live may auto-dispatch a verify on a PR head (2026-11-11, JARVIS via FRIDAY).

Two conditions, both required:
  (a) no live omp AUTHOR job on the PR's head branch (verify-* lanes are never authors);
  (b) active omp jobs across ALL callers under the Mac cap: 6, or 4 while load1 >= 25; never at load1 >= 40.
`decide` is pure; `load_jobs` reads ~/.omp-delegate/jobs and resolves each live job's branch from its lane cwd."""
import glob, json, os, subprocess

AUTHOR_LIVE = ("running", "waiting_input")
ACTIVE = ("running", "waiting_input", "queued", "starting")
LOAD_TIGHT, LOAD_STOP = 25.0, 40.0


def cap_for(load1):
    return 4 if load1 >= LOAD_TIGHT else 6


def decide(branch, jobs, load1):
    """jobs: list of {"id", "status", "branch", "lane"}. Returns (ok, reason)."""
    for j in jobs:
        if j["status"] in AUTHOR_LIVE and branch and j.get("branch") == branch and not (j.get("lane") or "").startswith("verify-"):
            return False, f"VERIFY-WAIT author live job={j['id'][:8]} lane={j.get('lane') or '?'}"
    n, cap = sum(j["status"] in ACTIVE for j in jobs), cap_for(load1)
    if load1 >= LOAD_STOP:
        return False, f"VERIFY-WAIT cap {n}/{cap} load={load1:.1f} (>= {LOAD_STOP:g}: no dispatch)"
    if n >= cap:
        return False, f"VERIFY-WAIT cap {n}/{cap} load={load1:.1f}"
    return True, f"VERIFY-GO cap {n}/{cap} load={load1:.1f}"


def _branch(cwd):
    if not cwd or not os.path.isdir(cwd): return ""
    r = subprocess.run(["git", "-C", cwd, "branch", "--show-current"], capture_output=True, text=True,
                       env=dict(os.environ, GIT_OPTIONAL_LOCKS="0"), timeout=30)
    return r.stdout.strip() if r.returncode == 0 else ""


def load_jobs(jobs_dir, lanes_by_id, sim_done=()):
    """Active jobs with lane name + branch. sim_done: job-id prefixes to treat as finished (dry run only)."""
    out = []
    for p in glob.glob(f"{jobs_dir}/*.json"):
        try: d = json.load(open(p))
        except Exception: continue
        jid, status = d.get("id") or os.path.basename(p)[:-5], d.get("status")
        if any(jid.startswith(s) for s in sim_done if s): status = "completed"
        if status not in ACTIVE: continue
        lane = lanes_by_id.get(d.get("laneId") or "", {})
        name = lane.get("name") or ""
        br = "" if name.startswith("verify-") or status not in AUTHOR_LIVE else _branch(lane.get("cwd"))
        out.append({"id": jid, "status": status, "lane": name, "branch": br})
    return out
