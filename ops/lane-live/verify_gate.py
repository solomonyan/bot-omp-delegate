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


# --- verifier model family (2026-11-11): the verifier must be a different model family from the author.
KIMI, CLAUDE, CODEX = "kimi-code/k3.1-pro", "anthropic/claude-opus-4-6", "openai-codex/gpt-6.1-sol"
FAMILY = {"openai-codex": "codex", "openai": "codex", "kimi-code": "kimi", "anthropic": "claude"}


def family(model):
    return FAMILY.get((model or "").split("/", 1)[0], "unknown")


def verifier_for(author_model, kimi_ok=True):
    """Pure. Codex/unknown author -> Kimi (Claude if Kimi is off); Kimi/Claude author -> Codex. Returns (model, family)."""
    m = CODEX if family(author_model) in ("kimi", "claude") else KIMI if kimi_ok else CLAUDE
    return m, family(m)


def session_model(sid, sessions_dir, options=None):
    """Pinned options.model, else provider/model of the last assistant message in the session's newest top-level
    jsonl (a --model pin at worker start writes no model_change record), else the last model_change."""
    if (options or {}).get("model"): return options["model"]
    fs = sorted(glob.glob(f"{sessions_dir}/{sid}/*.jsonl"), key=os.path.getmtime) if sid else []
    if not fs: return ""
    r = subprocess.run(["grep", "-ho", '"provider":"[^"]*","model":"[^"]*"', fs[-1]], capture_output=True, text=True, timeout=60)
    last = r.stdout.strip().splitlines()[-1:]
    if last:
        d = json.loads("{" + last[0] + "}"); return f"{d['provider']}/{d['model']}"
    r = subprocess.run(["grep", "-h", '"type":"model_change"', fs[-1]], capture_output=True, text=True, timeout=60)
    for line in reversed(r.stdout.splitlines()):
        try: return json.loads(line).get("model") or ""
        except Exception: continue
    return ""


def author_model(branch, lanes, sessions_dir):
    """Model of the newest non-verify lane whose worktree is on `branch` ("" if none)."""
    hits = [l for l in lanes if not (l.get("name") or "").startswith("verify-") and branch and _branch(l.get("cwd")) == branch]
    for l in reversed(hits):
        m = session_model(l.get("sessionId"), sessions_dir)
        if m: return m
    return ""
