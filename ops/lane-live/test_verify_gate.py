"""python3 test_verify_gate.py  (or pytest)"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from verify_gate import decide, verifier_for, family

BR = "lane-b/g13-harness"
AUTHOR = {"id": "3341e818000f441e", "status": "running", "lane": "eng-m9-457-g13", "branch": BR}
OTHER = [{"id": "d90ac16e1f7e4c61", "status": "running", "lane": "eng-settings-ux", "branch": "feat/settings-ux-upgrade"},
         {"id": "31e2d3e797174e96", "status": "waiting_input", "lane": "design-launch-checkout", "branch": ""}]


def test_skips_while_author_live():
    ok, why = decide(BR, [AUTHOR] + OTHER, 5.0)
    assert not ok and why.startswith("VERIFY-WAIT author live job=3341e818"), why


def test_skips_while_author_waiting_input():
    ok, why = decide(BR, [dict(AUTHOR, status="waiting_input")] + OTHER, 5.0)
    assert not ok and "author live job=3341e818" in why, why


def test_fires_after_author_finished():
    ok, why = decide(BR, OTHER, 5.0)  # author job completed -> load_jobs drops it
    assert ok and why == "VERIFY-GO cap 2/6 load=5.0", why


def test_verify_lane_is_not_an_author():
    ok, why = decide(BR, OTHER + [{"id": "fb1cc25d", "status": "running", "lane": "verify-457-61fd49d2", "branch": BR}], 5.0)
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
    assert verifier_for("openai-codex/gpt-6.1-sol") == ("kimi-code/k3.1-pro", "kimi")
    assert verifier_for("openai-codex/gpt-6-luna") == ("kimi-code/k3.1-pro", "kimi")


def test_kimi_off_falls_back_to_claude():
    assert verifier_for("openai-codex/gpt-6.1-sol", kimi_ok=False) == ("anthropic/claude-opus-4-6", "claude")


def test_kimi_or_claude_author_gets_codex():
    assert verifier_for("kimi-code/k3.1-pro") == ("openai-codex/gpt-6.1-sol", "codex")
    assert verifier_for("anthropic/claude-opus-4-6") == ("openai-codex/gpt-6.1-sol", "codex")


def test_unknown_author_defaults_to_kimi():
    assert verifier_for("") == ("kimi-code/k3.1-pro", "kimi") and family("") == "unknown"

if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for f in fns: f(); print("ok  ", f.__name__)
    print(f"{len(fns)} passed")
