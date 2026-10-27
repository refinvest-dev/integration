"""Check the workflow wiring that binds evidence to the candidate run."""

import sys
import unittest
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
from contract_evidence import CHECKS

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"


def workflow(name):
    # BaseLoader preserves GitHub's `on` key instead of YAML 1.1 boolean coercion.
    return yaml.load((WORKFLOWS / name).read_text(encoding="utf-8"), Loader=yaml.BaseLoader)


class CandidateWorkflowTest(unittest.TestCase):
    def test_checks_are_called_with_all_five_pinned_outputs(self):
        propagation = workflow("contract-propagation.yml")
        for key in CHECKS:
            job = propagation["jobs"][key]
            callee = workflow(Path(job["uses"]).name)
            self.assertEqual(set(job["with"]), set(callee["on"]["workflow_call"]["inputs"]))
            self.assertEqual(len(job["with"]), 5)
            for name, value in job["with"].items():
                self.assertEqual(value, "${{ needs.propagate.outputs." + name + " }}")
                self.assertEqual(callee["on"]["workflow_call"]["inputs"][name]["required"], "true")
        self.assertIn("candidate-sync", propagation["jobs"]["candidate-smoke"]["needs"])

    def test_attempt_names_match_collector_and_existing_entrypoints_remain(self):
        for key, filename in (("candidate-sync", "contract-sync.yml"), ("candidate-smoke", "api-smoke.yml")):
            data = workflow(filename)
            self.assertTrue({"pull_request", "push", "workflow_dispatch", "workflow_call"} <= set(data["on"]))
            uploads = [step["with"]["name"] for job in data["jobs"].values() for step in job["steps"]
                       if step.get("uses", "").startswith("actions/upload-artifact")]
            self.assertIn(CHECKS[key][0] + "-${{ github.run_attempt }}", uploads)

    def test_observer_runs_after_completion_and_executes_only_default_branch_code(self):
        data = workflow("contract-evidence.yml")
        self.assertEqual(data["on"]["workflow_run"]["types"], ["completed"])
        self.assertEqual(data["on"]["workflow_run"]["workflows"], ["Propagate Context snapshots"])
        job = data["jobs"]["record"]
        self.assertIn("head_branch == 'main'", job["if"])
        checkout = next(step for step in job["steps"] if step.get("uses", "").startswith("actions/checkout"))
        self.assertEqual(checkout["with"]["ref"], "refs/heads/main")
        self.assertEqual(checkout["with"]["persist-credentials"], "false")
        self.assertEqual(data["permissions"]["actions"], "read")


if __name__ == "__main__":
    unittest.main()
