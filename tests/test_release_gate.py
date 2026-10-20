import json
import os
from pathlib import Path
import subprocess
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SHA = "a" * 40
CI = {
    "name": "CI",
    "path": ".github/workflows/ci.yml",
    "event": "push",
    "head_branch": "main",
    "head_sha": SHA,
    "status": "completed",
    "conclusion": "success",
}
OTHER = {**CI, "name": "Unrelated", "path": ".github/workflows/other.yml"}


class ReleaseGateTests(unittest.TestCase):
    def run_gate(self, ci_runs, other_runs=None, api_error=False):
        workflow = (ROOT / ".github/workflows/release.yml").read_text()
        step = workflow.split("      - name: Require the tagged commit's CI", 1)[1]
        block = step.split("        run: |\n", 1)[1].split("\n      - name:", 1)[0]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture = root / "fixture.json"
            fixture.write_text(json.dumps({
                "ci": ci_runs,
                "all": ci_runs + (other_runs or []),
                "error": api_error,
            }))
            gh = root / "gh"
            gh.write_text(textwrap.dedent('''\
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                fixture = json.loads(Path(os.environ["FIXTURE"]).read_text())
                if fixture["error"]:
                    print("fixture API failure", file=sys.stderr)
                    sys.exit(1)
                args = sys.argv[1:]
                if args[:2] == ["run", "list"]:
                    # The original gate accepts this unrelated green result.
                    print(json.dumps(fixture["all"]))
                    sys.exit(0)
                assert args[0] == "api", args
                assert "--paginate" in args and "--slurp" in args, args
                assert "head_sha=" + os.environ["RELEASE_SHA"] in args, args
                endpoint = next(arg for arg in args if arg.startswith("repos/"))
                if "/workflows/ci.yml/" in endpoint:
                    assert "branch=main" in args and "event=push" in args, args
                    runs = fixture["ci"]
                else:
                    assert endpoint.endswith("/actions/runs"), args
                    runs = fixture["all"]
                # Multiple API pages exercise aggregation, including later failures.
                print(json.dumps([{"workflow_runs": [run]} for run in runs]))
                '''))
            gh.chmod(0o755)
            script = root / "gate.sh"
            script.write_text(
                "#!/usr/bin/env bash\n"
                "sleep() { SECONDS=$((SECONDS + 1801)); }\n"
                + textwrap.dedent(block)
            )
            return subprocess.run(
                ["bash", str(script)],
                env={
                    **os.environ,
                    "PATH": str(root) + os.pathsep + os.environ["PATH"],
                    "FIXTURE": str(fixture),
                    "RELEASE_SHA": SHA,
                    "DEFAULT_BRANCH": "main",
                    "GITHUB_REPOSITORY": "StartupBros-com/harness-optimizer",
                },
                text=True, capture_output=True, timeout=10,
            )

    def test_exact_default_branch_push_ci_passes(self):
        result = self.run_gate([CI])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unrelated_success_cannot_replace_missing_ci(self):
        result = self.run_gate([], [OTHER])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ci_identity_event_branch_and_commit_must_match(self):
        for field, value in [
            ("path", ".github/workflows/other.yml"),
            ("event", "pull_request"),
            ("head_branch", "feature"),
            ("head_sha", "b" * 40),
        ]:
            with self.subTest(field=field):
                result = self.run_gate([{**CI, field: value}], [OTHER])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_unsuccessful_required_ci_never_passes(self):
        for conclusion in ["failure", "cancelled", "timed_out", "skipped", "neutral"]:
            with self.subTest(conclusion=conclusion):
                result = self.run_gate([{**CI, "conclusion": conclusion}], [OTHER])
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_pending_ci_times_out(self):
        result = self.run_gate([{**CI, "status": "in_progress", "conclusion": None}])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_missing_runs_fail(self):
        result = self.run_gate([])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_additional_workflow_failure_on_later_page_is_preserved(self):
        result = self.run_gate([CI], [{**OTHER, "conclusion": "failure"}])
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_api_failure_is_not_green(self):
        result = self.run_gate([CI], api_error=True)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("fixture API failure", result.stderr)


if __name__ == "__main__":
    unittest.main()
