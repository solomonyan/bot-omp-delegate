import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "mac-disk" / "mac_disk_reaper.py"
SPEC = importlib.util.spec_from_file_location("mac_disk_reaper", SCRIPT)
reaper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(reaper)


class MacDiskReaperSafetyTests(unittest.TestCase):
    def test_overlap_blocks_ancestors_and_descendants(self):
        root = Path("/tmp/example")
        self.assertTrue(reaper._overlaps(root, root / "child"))
        self.assertTrue(reaper._overlaps(root / "child", root))
        self.assertFalse(reaper._overlaps(root, Path("/tmp/elsewhere")))

    def test_protected_wellness_name_is_screened_without_git_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "wellness-ops-private"
            candidate.mkdir()
            self.assertTrue(reaper._protected_path(candidate))

    def test_answer_markdown_keeps_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            (candidate / "nested").mkdir()
            (candidate / "nested" / "ANSWER-final.md").touch()
            self.assertEqual(reaper._protected_contents(candidate), "answer_markdown")

    def test_nested_git_repository_keeps_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            (candidate / "nested" / ".git").mkdir(parents=True)
            self.assertEqual(reaper._protected_contents(candidate), "nested_git_repository")

    def test_only_dead_or_reused_owner_is_orphan_when_timestamps_parse(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            (candidate / reaper.OWNER_FILE).write_text(json.dumps({
                "pid": 123, "start_time": "2026-10-12T08:30:00.500",
            }))
            with patch.object(reaper, "_pid_is_alive", return_value=False):
                self.assertTrue(reaper._owner_is_orphan(candidate))
            with patch.object(reaper, "_pid_is_alive", return_value=True), \
                 patch.object(reaper, "_process_start", return_value="Mon Oct 12 08:30:00 2026"):
                self.assertFalse(reaper._owner_is_orphan(candidate))
            with patch.object(reaper, "_pid_is_alive", return_value=True), \
                 patch.object(reaper, "_process_start", return_value="Tue Oct 13 08:30:00 2026"):
                self.assertTrue(reaper._owner_is_orphan(candidate))

    def test_unparseable_live_owner_start_time_is_not_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory)
            (candidate / reaper.OWNER_FILE).write_text(json.dumps({"pid": 123, "start_time": "unknown"}))
            with patch.object(reaper, "_pid_is_alive", return_value=True), \
                 patch.object(reaper, "_process_start", return_value="Tue Oct 13 08:30:00 2026"):
                self.assertFalse(reaper._owner_is_orphan(candidate))

    def test_lanes_verify_candidate_needs_no_owner_file(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            candidate = home / "lanes" / "verify-orphan"
            candidate.mkdir(parents=True)
            (home / ".omp" / "wt" / "unknown-owner").mkdir(parents=True)
            candidates = reaper.discover_candidates(home, home / "tmp")
            self.assertEqual(candidates, [candidate.resolve()])

    def test_active_job_paths_include_session_cwd_and_add_dirs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            jobs = root / "jobs"
            jobs.mkdir()
            (jobs / "job.json").write_text(json.dumps({"status": "waiting_input", "sessionId": "s1"}))
            state = root / "state.json"
            cwd, add_dir = root / "cwd", root / "extra"
            state.write_text(json.dumps({"sessions": {"s1": {"cwd": str(cwd), "options": {"add_dir": [str(add_dir)]}}}}))
            paths, ids = reaper._active_job_paths(jobs)
            self.assertEqual(ids, {"s1"})
            self.assertEqual(reaper._session_paths(ids, state), [cwd.resolve(), add_dir.resolve()])

    def test_unknown_job_state_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            (jobs / "job.json").write_text(json.dumps({"status": "mystery"}))
            with self.assertRaises(reaper.SafetyError):
                reaper._active_job_paths(jobs)

    def test_missing_job_directory_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(reaper.SafetyError):
                reaper._active_job_paths(Path(directory) / "missing")

    def test_old_job_log_dry_run_reports_path_and_keeps_state(self):
        with tempfile.TemporaryDirectory() as directory:
            jobs = Path(directory)
            log_file = jobs / "job-1.jsonl.gz"
            state_file = jobs / "job-1.json"
            log_file.write_bytes(b"compressed-log")
            state_file.write_text(json.dumps({"status": "completed"}))
            old = reaper.time.time() - 31 * reaper.DAY
            os.utime(log_file, (old, old))
            with patch.object(reaper, "JOBS_DIR", jobs):
                count, size, rows = reaper._cleanup_old_logs(reaper.time.time(), True)
            self.assertEqual((count, size), (1, len(b"compressed-log")))
            self.assertEqual(rows[0]["action"], "would_remove_job_log")
            self.assertEqual(rows[0]["path"], str(log_file))
            self.assertTrue(log_file.exists())
            self.assertTrue(state_file.exists())

    def test_missing_active_session_location_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / "state.json"
            state.write_text(json.dumps({"sessions": {}}))
            with self.assertRaises(reaper.SafetyError):
                reaper._session_paths({"missing"}, state)

    def test_lane_query_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "omp-delegate"
            cli.touch()
            result = type("Result", (), {"returncode": 1, "stdout": "", "stderr": ""})()
            with patch.object(reaper, "_run", return_value=result):
                with self.assertRaises(reaper.SafetyError):
                    reaper._lane_paths(cli)

    def test_lane_paths_include_only_active_lane_cwds(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "omp-delegate"
            cli.touch()
            active, idle = Path(directory) / "active", Path(directory) / "idle"
            payload = json.dumps({"lanes": [
                {"activeJob": "job-1", "cwd": str(active)},
                {"activeJob": None, "cwd": str(idle)},
            ]})
            result = type("Result", (), {"returncode": 0, "stdout": payload, "stderr": ""})()
            with patch.object(reaper, "_run", return_value=result):
                self.assertEqual(reaper._lane_paths(cli), [active.resolve()])

    def test_missing_lane_activity_key_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            cli = Path(directory) / "omp-delegate"
            cli.touch()
            result = type("Result", (), {
                "returncode": 0,
                "stdout": json.dumps({"lanes": [{"cwd": str(Path(directory) / "cwd")}]}),
                "stderr": "",
            })()
            with patch.object(reaper, "_run", return_value=result):
                with self.assertRaises(reaper.SafetyError):
                    reaper._lane_paths(cli)

    def test_git_safety_and_worktree_removal_against_real_repository(self):
        import contextlib
        import io
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, remote, candidate = root / "main", root / "remote.git", root / "verify-fixture"
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                     "-c", "user.email=fixture@example.invalid", *map(str, args)],
                    check=True, capture_output=True, text=True, env=env,
                )

            git("init", "--initial-branch=main", main)
            git("init", "--bare", remote)
            (main / "tracked").write_text("initial")
            git("-C", main, "add", "tracked")
            git("-C", main, "commit", "-m", "initial")
            self.assertFalse(reaper._git_is_safe(main))  # No upstream: ownership is unproven.
            git("-C", main, "remote", "add", "origin", remote)
            git("-C", main, "push", "-u", "origin", "main")
            git("-C", main, "worktree", "add", "--track", "-b", "verify", candidate, "origin/main")
            self.assertTrue(reaper._git_is_safe(candidate))
            extra = candidate / "untracked"
            extra.write_text("retain me")
            self.assertFalse(reaper._git_is_safe(candidate))
            rows = []
            with patch.object(reaper, "discover_candidates", return_value=[candidate]), \
                 patch.object(reaper, "_guard_paths", return_value=[]), \
                 patch.object(reaper, "_cleanup_old_logs", return_value=(0, 0, [])), \
                 patch.object(reaper, "_chrome_processes", return_value=[]), \
                 patch.object(reaper, "_snapshot_report", return_value=0), \
                 patch.object(reaper, "_record", side_effect=rows.append), \
                 contextlib.redirect_stdout(io.StringIO()):
                reaper._run_once(True)
            self.assertTrue(extra.exists())
            self.assertTrue(any(row.get("reason") == "git_dirty_untracked_or_unpushed_or_unknown"
                                for row in rows))
            extra.unlink()
            (candidate / "tracked").write_text("unpushed")
            self.assertFalse(reaper._git_is_safe(candidate))
            git("-C", candidate, "add", "tracked")
            git("-C", candidate, "commit", "-m", "unpushed")
            self.assertFalse(reaper._git_is_safe(candidate))
            git("-C", candidate, "push", "origin", "HEAD:main")
            self.assertTrue(reaper._git_is_safe(candidate))
            git("-C", main, "checkout", "-b", "saved-work")
            (main / "tracked").write_text("other branch")
            git("-C", main, "add", "tracked")
            git("-C", main, "commit", "-m", "unpublished other branch")
            self.assertFalse(reaper._git_is_safe(candidate))
            git("-C", main, "push", "origin", "saved-work")
            # Update this worktree's shared remote refs; never fetch a real remote.
            self.assertTrue(reaper._git_is_safe(candidate))
            with patch.object(reaper, "_guard_paths", return_value=[]):
                self.assertEqual(reaper._delete_candidate(candidate, "worktree", False), "removed")
            self.assertFalse(candidate.exists())
            self.assertEqual((main / "tracked").read_text(), "other branch")

    def test_unpublished_stash_tag_and_arbitrary_refs_are_kept(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, remote = root / "source", root / "remote.git"
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                     "-c", "user.email=fixture@example.invalid", *map(str, args)],
                    check=True, capture_output=True, text=True, env=env,
                )

            git("init", "--initial-branch=main", source)
            git("init", "--bare", remote)
            (source / "tracked").write_text("published")
            git("-C", source, "add", "tracked")
            git("-C", source, "commit", "-m", "published")
            git("-C", source, "remote", "add", "origin", remote)
            git("-C", source, "push", "-u", "origin", "main")
            git("--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main")

            # A stash whose base is remote-reachable is still user work.
            clone = root / "stash-clone"
            git("clone", remote, clone)
            unpublished_stash = root / "unpublished-stash"
            git("clone", remote, unpublished_stash)
            (unpublished_stash / "local").write_text("unpublished base")
            git("-C", unpublished_stash, "add", "local")
            git("-C", unpublished_stash, "commit", "-m", "unpublished stash base")
            (unpublished_stash / "tracked").write_text("stashed change")
            git("-C", unpublished_stash, "stash", "push", "-m", "unpublished-base stash")
            self.assertEqual(reaper._git_unsafe_reason(unpublished_stash), "git_stash_present")
            (clone / "tracked").write_text("stashed change")
            git("-C", clone, "stash", "push", "-m", "published-base stash")
            self.assertEqual(reaper._git_unsafe_reason(clone), "git_stash_present")

            # A tag can be the only local ref retaining a detached unpublished commit.
            tag_clone = root / "tag-clone"
            git("clone", remote, tag_clone)
            git("-C", tag_clone, "checkout", "--detach")
            (tag_clone / "tagged").write_text("tag only")
            git("-C", tag_clone, "add", "tagged")
            git("-C", tag_clone, "commit", "-m", "tag-only unpublished")
            git("-C", tag_clone, "tag", "local-only")
            git("-C", tag_clone, "checkout", "main")
            self.assertEqual(reaper._git_unsafe_reason(tag_clone), "git_unpublished_refs")

            # Non-standard refs count too, not just branches and tags.
            ref_clone = root / "ref-clone"
            git("clone", remote, ref_clone)
            git("-C", ref_clone, "checkout", "--detach")
            (ref_clone / "ref-only").write_text("ref only")
            git("-C", ref_clone, "add", "ref-only")
            git("-C", ref_clone, "commit", "-m", "ref-only unpublished")
            commit = git("-C", ref_clone, "rev-parse", "HEAD").stdout.strip()
            git("-C", ref_clone, "update-ref", "refs/saved/only", commit)
            git("-C", ref_clone, "checkout", "main")
            self.assertEqual(reaper._git_unsafe_reason(ref_clone), "git_unpublished_refs")

    def test_sibling_worktree_observes_common_stash_and_tag_refs(self):
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, remote, sibling = root / "main", root / "remote.git", root / "sibling"
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                     "-c", "user.email=fixture@example.invalid", *map(str, args)],
                    check=True, capture_output=True, text=True, env=env,
                )

            git("init", "--initial-branch=main", main)
            git("init", "--bare", remote)
            (main / "tracked").write_text("published")
            git("-C", main, "add", "tracked")
            git("-C", main, "commit", "-m", "published")
            git("-C", main, "remote", "add", "origin", remote)
            git("-C", main, "push", "-u", "origin", "main")
            git("-C", main, "worktree", "add", "--track", "-b", "sibling", sibling, "origin/main")
            self.assertTrue(reaper._git_is_safe(sibling))

            (main / "tracked").write_text("stash shared")
            git("-C", main, "stash", "push", "-m", "common stash")
            self.assertEqual(reaper._git_unsafe_reason(sibling), "git_stash_present")
            git("-C", main, "stash", "clear")

            git("-C", main, "checkout", "--detach")
            (main / "tagged").write_text("tag shared")
            git("-C", main, "add", "tagged")
            git("-C", main, "commit", "-m", "common tag commit")
            git("-C", main, "tag", "shared-unpublished")
            git("-C", main, "checkout", "main")
            self.assertEqual(reaper._git_unsafe_reason(sibling), "git_unpublished_refs")

    def test_new_active_overlap_at_deletion_boundary_prevents_all_removals(self):
        import contextlib
        import io
        import subprocess

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            main, remote, candidate, copy = (
                root / "main", root / "remote.git", root / "candidate", root / "copy",
            )
            jobs, cli = root / "jobs", root / "omp-delegate"
            jobs.mkdir()
            cli.write_text("#!/bin/sh\nprintf '%s\\n' '{\"lanes\":[]}'\n")
            cli.chmod(0o755)
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1")

            def git(*args):
                return subprocess.run(
                    ["git", "-c", "core.hooksPath=/dev/null", "-c", "user.name=Fixture",
                     "-c", "user.email=fixture@example.invalid", *map(str, args)],
                    check=True, capture_output=True, text=True, env=env,
                )

            git("init", "--initial-branch=main", main)
            git("init", "--bare", remote)
            (main / "tracked").write_text("published")
            git("-C", main, "add", "tracked")
            git("-C", main, "commit", "-m", "published")
            git("-C", main, "remote", "add", "origin", remote)
            git("-C", main, "push", "-u", "origin", "main")
            git("--git-dir", remote, "symbolic-ref", "HEAD", "refs/heads/main")
            git("-C", main, "worktree", "add", "--track", "-b", "candidate", candidate, "origin/main")
            git("clone", remote, copy)
            rows = []
            real_guard_paths = reaper._guard_paths
            real_candidate_size = reaper._candidate_size

            def size_with_new_active_job(path):
                (jobs / "active.json").write_text(json.dumps({"status": "running", "cwd": str(path)}))
                return real_candidate_size(path)

            with patch.object(
                reaper, "_guard_paths",
                side_effect=lambda: real_guard_paths(jobs_dir=jobs, cli=cli),
            ), patch.object(reaper, "_candidate_size", side_effect=size_with_new_active_job), \
                 patch.object(reaper, "discover_candidates", return_value=[candidate.resolve(), copy.resolve()]), \
                 patch.object(reaper, "_cleanup_old_logs", return_value=(0, 0, [])), \
                 patch.object(reaper, "_chrome_processes", return_value=[]), \
                 patch.object(reaper, "_snapshot_report", return_value=0), \
                 patch.object(reaper, "_record", side_effect=rows.append), \
                 contextlib.redirect_stdout(io.StringIO()):
                reaper._run_once(False)
            self.assertTrue(candidate.is_dir())
            self.assertTrue(copy.is_dir())
            self.assertEqual(
                sum(row.get("reason") == "active_job_or_lane_overlap" for row in rows), 2,
            )

    def test_dry_run_never_removes_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            candidate = Path(directory) / "candidate"
            candidate.mkdir()
            self.assertEqual(reaper._delete_candidate(candidate, None, True), "would_remove")
            self.assertTrue(candidate.is_dir())


if __name__ == "__main__":
    unittest.main()
