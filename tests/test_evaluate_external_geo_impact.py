import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tools.evaluate_external_geo_impact import (
    FIXTURE_SCHEMA,
    LABEL_SCHEMA,
    create_label_template,
    evaluate_external_chat_blocks,
    evaluate_external_documents,
    evaluate_external_files,
)


def _documents():
    fixtures = {
        "schema_version": FIXTURE_SCHEMA,
        "cohort_id": "external-cohort-1",
        "cases": [
            {
                "case_id": "direct",
                "changed_files": ["pkg/core.py"],
                "sources": {
                    "pkg/core.py": "def changed():\n    return 1\n",
                    "tests/test_core.py": (
                        "from pkg.core import changed\n"
                        "def test_core():\n    return changed()\n"
                    ),
                },
            },
            {
                "case_id": "unrelated",
                "changed_files": ["pkg/core.py"],
                "sources": {
                    "pkg/core.py": "def changed():\n    return 1\n",
                    "tests/test_unrelated.py": "def test_unrelated():\n    return 2\n",
                },
            },
        ],
    }
    fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
    labels = {
        "schema_version": LABEL_SCHEMA,
        "cohort_id": "external-cohort-1",
        "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
        "labels": [
            {"case_id": "direct", "expected_test_files": ["tests/test_core.py"]},
            {"case_id": "unrelated", "expected_test_files": []},
        ],
    }
    return fixtures, fixture_bytes, labels


class ExternalGeoImpactEvaluationTests(unittest.TestCase):
    def test_chat_blocks_compute_hash_and_score_without_files(self):
        fixtures, _fixture_bytes, labels = _documents()
        labels["fixtures_sha256"] = "ASSISTANT_WILL_COMPUTE_FROM_FIXTURE_BLOCK"
        fixture_text = "```json\n" + json.dumps(fixtures, sort_keys=True, indent=2) + "\n```"
        label_text = "```json\n" + json.dumps(labels, sort_keys=True, indent=2) + "\n```"

        report = evaluate_external_chat_blocks(fixture_text, label_text)

        self.assertEqual(report["cohort_id"], "external-cohort-1")
        self.assertTrue(report["fixtures_sha256"])
        self.assertEqual(report["fallback_comparison"]["fallback_enabled"]["metrics"]["affected_test_recall"], 1.0)

    def test_chat_hash_placeholder_does_not_override_an_explicit_wrong_hash(self):
        fixtures, _fixture_bytes, labels = _documents()
        labels["fixtures_sha256"] = "0" * 64

        with self.assertRaisesRegex(ValueError, "does not bind"):
            evaluate_external_chat_blocks(json.dumps(fixtures), json.dumps(labels))

    def test_label_template_is_hash_bound_and_new_file_only(self):
        fixtures, fixture_bytes, _labels = _documents()
        with tempfile.TemporaryDirectory() as directory:
            fixture_path = Path(directory) / "fixtures.json"
            labels_path = Path(directory) / "labels.json"
            fixture_path.write_bytes(fixture_bytes)

            result = create_label_template(fixture_path, labels_path)
            labels = json.loads(labels_path.read_text(encoding="utf-8"))

            self.assertEqual(labels["schema_version"], LABEL_SCHEMA)
            self.assertEqual(labels["fixtures_sha256"], hashlib.sha256(fixture_bytes).hexdigest())
            self.assertEqual([row["case_id"] for row in labels["labels"]], [case["case_id"] for case in fixtures["cases"]])
            self.assertTrue(all(row["expected_test_files"] == [] for row in labels["labels"]))
            self.assertFalse(result["labels_filled"])
            with self.assertRaises(FileExistsError):
                create_label_template(fixture_path, labels_path)

    def test_hash_bound_documents_score_fallback_modes_without_executing_sources(self):
        fixtures, fixture_bytes, labels = _documents()
        marker = Path(tempfile.gettempdir()) / "geo-external-source-must-not-run"
        marker.unlink(missing_ok=True)
        fixtures["cases"][0]["sources"]["pkg/core.py"] = (
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
            "def changed():\n    return 1\n"
        )
        fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        labels["fixtures_sha256"] = hashlib.sha256(fixture_bytes).hexdigest()

        report = evaluate_external_documents(fixtures, fixture_bytes, labels)

        self.assertFalse(marker.exists())
        self.assertFalse(report["fixture_sources_executed"])
        self.assertFalse(report["label_producer_authenticated"])
        baseline = report["fallback_comparison"]["fallback_disabled"]["metrics"]
        enabled = report["fallback_comparison"]["fallback_enabled"]["metrics"]
        self.assertEqual(baseline["expected_test_files"], 1)
        self.assertEqual(enabled["recalled_test_files"], 1)
        self.assertEqual(enabled["unnecessary_test_files"], 0)
        self.assertEqual(enabled["affected_test_recall"], 1.0)
        self.assertEqual(report["cohort_id"], "external-cohort-1")

    def test_unresolved_dynamic_dispatch_in_changed_source_taints_selected_test(self):
        fixtures = {
            "schema_version": FIXTURE_SCHEMA,
            "cohort_id": "dynamic-source-boundary",
            "cases": [{
                "case_id": "dynamic-source",
                "changed_files": ["src/plugins.py"],
                "sources": {
                    "src/plugins.py": (
                        "def load_plugin(name):\n"
                        "    module = __import__(name)\n"
                        "    return getattr(module, 'run', None)\n"
                    ),
                    "tests/test_plugins.py": (
                        "from src.plugins import load_plugin\n"
                        "def test_load():\n    return load_plugin('x')\n"
                    ),
                },
            }],
        }
        fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        labels = {
            "schema_version": LABEL_SCHEMA,
            "cohort_id": "dynamic-source-boundary",
            "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "labels": [{"case_id": "dynamic-source", "expected_test_files": ["tests/test_plugins.py"]}],
        }

        report = evaluate_external_documents(fixtures, fixture_bytes, labels)
        for mode in report["fallback_comparison"].values():
            metrics = mode["metrics"]
            case = mode["cases"][0]
            candidate = case["candidate_uncertainty"][0]
            self.assertEqual(metrics["affected_test_recall"], 1.0)
            self.assertEqual(metrics["actionable_affected_recall"], 0.0)
            self.assertEqual(case["recommended_test_files"], ["tests/test_plugins.py"])
            self.assertEqual(case["review_required_test_files"], ["tests/test_plugins.py"])
            self.assertEqual(case["no_known_boundary_test_files"], [])
            self.assertEqual(candidate["recommendation"], "run_candidate")
            self.assertEqual(candidate["evidence_status"], "review_required_boundary_present")
            self.assertTrue(candidate["uncertain"])
            self.assertIn("unresolved_attribute_call_summary", candidate["uncertainty_reasons"])

    def test_dynamic_issue_in_one_changed_function_does_not_taint_other_function_test(self):
        fixtures = {
            "schema_version": FIXTURE_SCHEMA,
            "cohort_id": "candidate-local-source-issue",
            "cases": [{
                "case_id": "two-functions",
                "changed_files": ["src/service.py"],
                "sources": {
                    "src/service.py": (
                        "def stable():\n    return 1\n"
                        "def dynamic(name):\n    return getattr(target, name)()\n"
                    ),
                    "tests/test_stable.py": (
                        "from src.service import stable\n"
                        "def test_stable():\n    return stable()\n"
                    ),
                    "tests/test_dynamic.py": (
                        "from src.service import dynamic\n"
                        "def test_dynamic():\n    return dynamic('run')\n"
                    ),
                },
            }],
        }
        fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        labels = {
            "schema_version": LABEL_SCHEMA,
            "cohort_id": "candidate-local-source-issue",
            "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "labels": [{
                "case_id": "two-functions",
                "expected_test_files": ["tests/test_stable.py", "tests/test_dynamic.py"],
            }],
        }

        report = evaluate_external_documents(fixtures, fixture_bytes, labels)
        for mode in report["fallback_comparison"].values():
            cases = mode["cases"][0]
            uncertainty = {row["test_file"]: row for row in cases["candidate_uncertainty"]}
            self.assertFalse(uncertainty["tests/test_stable.py"]["uncertain"])
            self.assertEqual(uncertainty["tests/test_stable.py"]["recommendation"], "run_candidate")
            self.assertEqual(uncertainty["tests/test_stable.py"]["evidence_status"], "no_known_boundary")
            self.assertTrue(uncertainty["tests/test_dynamic.py"]["uncertain"])
            self.assertEqual(set(cases["actionable_recalled_test_files"]), {"tests/test_stable.py"})

        def test_dynamic_issue_reached_through_production_wrapper_is_candidate_local(self):
            fixtures = {
                "schema_version": FIXTURE_SCHEMA,
                "cohort_id": "indirect-dynamic-call-locality",
                "cases": [{
                    "case_id": "wrapper-to-dynamic-dispatch",
                    "changed_files": ["src/service.py"],
                    "sources": {
                        "src/__init__.py": "",
                        "src/service.py": (
                            "def dispatch(name):\n"
                            "    return getattr(target, name)()\n"
                            "def wrapper(name):\n"
                            "    return dispatch(name)\n"
                            "def stable():\n"
                            "    return 1\n"
                        ),
                        "tests/test_dispatch.py": (
                            "from src.service import wrapper\n"
                            "def test_dispatch():\n"
                            "    return wrapper('run')\n"
                        ),
                        "tests/test_stable.py": (
                            "from src.service import stable\n"
                            "def test_stable():\n"
                            "    return stable()\n"
                        ),
                    },
                }],
            }
            fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
            labels = {
                "schema_version": LABEL_SCHEMA,
                "cohort_id": "indirect-dynamic-call-locality",
                "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
                "labels": [{
                    "case_id": "wrapper-to-dynamic-dispatch",
                    "expected_test_files": ["tests/test_dispatch.py", "tests/test_stable.py"],
                }],
            }

            report = evaluate_external_documents(fixtures, fixture_bytes, labels)
            for mode in report["fallback_comparison"].values():
                case = mode["cases"][0]
                uncertainty = {row["test_file"]: row for row in case["candidate_uncertainty"]}
                self.assertEqual(case["recommended_test_files"], ["tests/test_dispatch.py", "tests/test_stable.py"])
                self.assertTrue(uncertainty["tests/test_dispatch.py"]["uncertain"])
                self.assertIn(
                    "unresolved_attribute_call_summary",
                    uncertainty["tests/test_dispatch.py"]["uncertainty_reasons"],
                )
                self.assertFalse(uncertainty["tests/test_stable.py"]["uncertain"])
                self.assertEqual(case["actionable_recalled_test_files"], ["tests/test_stable.py"])

    def test_fallback_recommends_deleted_symbol_test_but_keeps_boundary_and_stable_test_actionable(self):
        fixtures = {
            "schema_version": FIXTURE_SCHEMA,
            "cohort_id": "dev-deleted-symbol-recommendation",
            "cases": [{
                "case_id": "removed-symbol-and-stable-api",
                "changed_files": ["src/core/api.py"],
                "max_hops": 0,
                "sources": {
                    "src/__init__.py": "",
                    "src/core/__init__.py": "",
                    "src/core/api.py": "def stable():\n    return 1\n",
                    "tests/test_removed.py": (
                        "from src.core.api import removed\n"
                        "def test_removed():\n    return removed()\n"
                    ),
                    "tests/test_stable.py": (
                        "from src.core.api import stable\n"
                        "def test_stable():\n    return stable()\n"
                    ),
                },
            }],
        }
        fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        labels = {
            "schema_version": LABEL_SCHEMA,
            "cohort_id": "dev-deleted-symbol-recommendation",
            "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "labels": [{
                "case_id": "removed-symbol-and-stable-api",
                "expected_test_files": ["tests/test_removed.py"],
            }],
        }

        report = evaluate_external_documents(fixtures, fixture_bytes, labels)
        baseline = report["fallback_comparison"]["fallback_disabled"]
        enabled = report["fallback_comparison"]["fallback_enabled"]
        baseline_case = baseline["cases"][0]
        enabled_case = enabled["cases"][0]
        baseline_candidates = {row["test_file"]: row for row in baseline_case["candidate_uncertainty"]}
        enabled_candidates = {row["test_file"]: row for row in enabled_case["candidate_uncertainty"]}

        self.assertEqual(baseline_case["recommended_test_files"], ["tests/test_stable.py"])
        self.assertEqual(enabled_case["recommended_test_files"], ["tests/test_removed.py", "tests/test_stable.py"])
        self.assertEqual(enabled_case["review_required_test_files"], ["tests/test_removed.py"])
        self.assertEqual(enabled_case["no_known_boundary_test_files"], ["tests/test_stable.py"])
        self.assertTrue(enabled_candidates["tests/test_removed.py"]["uncertain"])
        self.assertFalse(enabled_candidates["tests/test_stable.py"]["uncertain"])
        self.assertEqual(enabled_case["actionable_recalled_test_files"], [])
        self.assertEqual(enabled["metrics"]["recommended_affected_recall"], 1.0)
        self.assertEqual(enabled["metrics"]["actionable_affected_recall"], 0.0)
        delta = report["fallback_delta"]
        self.assertEqual(delta["counts"]["added_recommended_candidates"], 1)
        self.assertEqual(delta["counts"]["added_expected_candidates"], 1)
        self.assertEqual(delta["counts"]["added_actionable_expected_candidates"], 0)
        self.assertEqual(delta["counts"]["added_abstained_expected_candidates"], 1)
        self.assertEqual(delta["counts"]["added_unnecessary_candidates"], 0)

    def test_rejects_fixture_hash_mismatch_and_case_label_mismatch(self):
        fixtures, fixture_bytes, labels = _documents()
        wrong_hash = {**labels, "fixtures_sha256": "0" * 64}
        with self.assertRaisesRegex(ValueError, "does not bind"):
            evaluate_external_documents(fixtures, fixture_bytes, wrong_hash)

        wrong_labels = {**labels, "labels": labels["labels"][:1]}
        with self.assertRaisesRegex(ValueError, "exactly one row"):
            evaluate_external_documents(fixtures, fixture_bytes, wrong_labels)

        mismatched_mapping = {**fixtures, "cohort_id": "changed-without-changing-bytes"}
        with self.assertRaisesRegex(ValueError, "does not match"):
            evaluate_external_documents(mismatched_mapping, fixture_bytes, labels)

        bad_path = json.loads(json.dumps(fixtures))
        bad_path["cases"][0]["sources"]["pkg/readme.txt"] = "not Python"
        with self.assertRaisesRegex(ValueError, "Python .py"):
            evaluate_external_documents(
                bad_path,
                (json.dumps(bad_path, sort_keys=True) + "\n").encode(),
                labels,
            )

    def test_global_parse_failure_abstains_expected_test_even_when_unselected(self):
        fixtures = {
            "schema_version": FIXTURE_SCHEMA,
            "cohort_id": "global-incomplete",
            "cases": [{
                "case_id": "broken-test",
                "changed_files": ["pkg/core.py"],
                "sources": {
                    "pkg/core.py": "def changed():\n    return 1\n",
                    "tests/test_broken.py": "def test_broken(:\n    pass\n",
                },
            }],
        }
        fixture_bytes = (json.dumps(fixtures, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode()
        labels = {
            "schema_version": LABEL_SCHEMA,
            "cohort_id": "global-incomplete",
            "fixtures_sha256": hashlib.sha256(fixture_bytes).hexdigest(),
            "labels": [{"case_id": "broken-test", "expected_test_files": ["tests/test_broken.py"]}],
        }

        report = evaluate_external_documents(fixtures, fixture_bytes, labels)
        metrics = report["fallback_comparison"]["fallback_enabled"]["metrics"]

        self.assertEqual(metrics["selected_test_files"], 0)
        self.assertEqual(metrics["expected_test_abstentions"], 1)
        self.assertEqual(metrics["expected_test_abstention_rate"], 1.0)

    def test_file_cli_reader_rejects_duplicate_keys_and_does_not_overwrite_output(self):
        fixtures, fixture_bytes, labels = _documents()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fixture_path = root / "fixtures.json"
            label_path = root / "labels.json"
            output_path = root / "report.json"
            fixture_path.write_bytes(fixture_bytes)
            label_path.write_text(json.dumps(labels), encoding="utf-8")
            output_path.write_text("existing", encoding="utf-8")
            with self.assertRaises(FileExistsError):
                evaluate_external_files(fixture_path, label_path, output_path)

            fixture_path.write_text('{"schema_version":"x","schema_version":"y"}', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                evaluate_external_files(fixture_path, label_path, root / "new-report.json")


if __name__ == "__main__":
    unittest.main()