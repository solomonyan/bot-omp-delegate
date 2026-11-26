"""python3 test_verify_gate.py  (or pytest)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_gate import decide, verifier_for, family

BR = "feature/author"
AUTHOR = {"id": "author-job-1", "status": "running", "lane": "author", "branch": BR}
OTHER = [{"id": "other-job-1", "status": "running", "lane": "other", "branch": "feature/other"},
         {"id": "other-job-2", "status": "waiting_input", "lane": "design", "branch": ""}]
CODEX = "openai-codex/test-model"


def test_skips_while_author_live():
    ok, why = decide(BR, [AUTHOR] + OTHER, 5.0)
    assert not ok and why.startswith("VERIFY-WAIT author live job=author-j"), why


def test_skips_while_author_waiting_input():
    ok, why = decide(BR, [dict(AUTHOR, status="waiting_input")] + OTHER, 5.0)
    assert not ok and "author live job=author-j" in why, why


def test_fires_after_author_finished():
    ok, why = decide(BR, OTHER, 5.0)  # author job completed -> load_jobs drops it
    assert ok and why == "VERIFY-GO cap 2/6 load=5.0", why


def test_verify_lane_is_not_an_author():
    ok, why = decide(BR, OTHER + [{"id": "verify-job", "status": "running", "lane": "verify-fixture", "branch": BR}], 5.0)
    assert ok, why


def test_skips_at_cap_low_load():
    jobs = OTHER + [{"id": f"j{i}", "status": "running", "lane": f"x{i}", "branch": f"b{i}"} for i in range(4)]
    ok, why = decide(BR, jobs, 10.0)
    assert not ok and why == "VERIFY-WAIT cap 6/6 load=10.0", why
    assert decide(BR, jobs[:5], 10.0)[0]


def test_skips_at_cap_high_load():
    jobs = OTHER + [{"id": f"j{i}", "status": "running", "lane": f"x{i}", "branch": f"b{i}"} for i in range(2)]
    ok, why = decide(BR, jobs, 25.0)
    assert not ok and why == "VERIFY-WAIT cap 4/4 load=25.0", why
    assert decide(BR, jobs[:3], 24.9)[0] and decide(BR, jobs[:3], 25.0)[0]


def test_never_at_load_40():
    ok, why = decide(BR, [], 40.0)
    assert not ok and why.startswith("VERIFY-WAIT cap 0/4 load=40.0"), why


def test_codex_author_gets_kimi():
    assert verifier_for(CODEX, CODEX) == ("kimi-code/k3.1-pro", "kimi")
    assert verifier_for("openai/another-model", CODEX) == ("kimi-code/k3.1-pro", "kimi")


def test_claude_is_never_a_verifier():
    for a in (CODEX, "anthropic/claude-opus-4-6", "xai/grok-4.7", "", "kimi-code/k3.1-pro"):
        assert verifier_for(a, CODEX)[1] != "claude"


def test_kimi_author_gets_codex_claude_author_gets_kimi():
    assert verifier_for("kimi-code/k3.1-pro", CODEX) == (CODEX, "codex")
    assert verifier_for("anthropic/claude-opus-4-6", CODEX) == ("kimi-code/k3.1-pro", "kimi")


def test_unknown_author_defaults_to_kimi():
    assert verifier_for("", CODEX) == ("kimi-code/k3.1-pro", "kimi") and family("") == "unknown"

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns: f(); print("ok  ", f.__name__)
    print(f"{len(fns)} passed")
