import json
import tempfile
import unittest
from pathlib import Path

from experiment_registry import complete, load_events, register


class RegistryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "journal.jsonl"
        self.spec = {
            "hypothesis": "synthetic accounting check", "data_provenance": "unit test",
            "data_sha256": "a" * 64, "code_revision": "test fixture",
            "parameters": {}, "sample_split": "synthetic only",
            "cost_model": "none for unit test", "primary_metric": "identity",
            "rejection_rules": ["identity fails"], "evidence_type": "synthetic",
        }
        self.artifact = Path(self.temp.name) / "result.json"
        self.artifact.write_text(json.dumps({"evidence_type": "synthetic"}))

    def test_cannot_record_results_before_registration(self):
        with self.assertRaises(ValueError):
            complete(self.path, "trial1", "completed", self.artifact)

    def test_rejected_trials_remain_in_journal_and_ids_cannot_be_reused(self):
        register(self.path, "trial1", self.spec)
        complete(self.path, "trial1", "rejected", self.artifact)
        self.assertEqual(len(load_events(self.path)), 2)
        with self.assertRaises(ValueError):
            register(self.path, "trial1", self.spec)
        with self.assertRaises(ValueError):
            complete(self.path, "trial1", "completed", self.artifact)

    def test_cannot_relabel_synthetic_evidence(self):
        register(self.path, "trial1", self.spec)
        self.artifact.write_text(json.dumps({"evidence_type": "forward_paper"}))
        with self.assertRaises(ValueError):
            complete(self.path, "trial1", "completed", self.artifact)

    def test_edits_to_an_existing_record_are_detected(self):
        register(self.path, "trial1", self.spec)
        self.path.write_text(self.path.read_text().replace("synthetic accounting check", "altered"))
        with self.assertRaises(ValueError):
            load_events(self.path)

    def test_missing_provenance_is_rejected(self):
        del self.spec["data_sha256"]
        with self.assertRaises(ValueError):
            register(self.path, "trial1", self.spec)


if __name__ == "__main__":
    unittest.main()
