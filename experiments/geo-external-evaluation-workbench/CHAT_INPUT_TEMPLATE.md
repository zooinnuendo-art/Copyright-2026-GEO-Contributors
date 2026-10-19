# Submit GEO Evaluation Data in Chat

You can provide the evaluation cohort directly in the Copilot Chat message.
Paste two fenced JSON blocks: fixture source first, expected-impact labels
second. Do not include secrets or real credentials in source text.

```json
{
  "schema_version": "ain.geo-impact-fixtures.v1",
  "cohort_id": "new-cohort-name",
  "cases": [
    {
      "case_id": "case-001",
      "changed_files": ["pkg/core.py"],
      "max_hops": 0,
      "sources": {
        "pkg/core.py": "def changed():\n    return 1\n",
        "tests/test_core.py": "from pkg.core import changed\ndef test_core():\n    return changed()\n"
      }
    }
  ]
}
```

```json
{
  "schema_version": "ain.geo-impact-labels.v1",
  "cohort_id": "new-cohort-name",
  "fixtures_sha256": "ASSISTANT_WILL_COMPUTE_FROM_FIXTURE_BLOCK",
  "labels": [
    {
      "case_id": "case-001",
      "expected_test_files": ["tests/test_core.py"]
    }
  ]
}
```

The fixture `cohort_id` must match in both blocks, and each `case_id` must occur
once in each block. `changed_files`, source paths, and expected test paths must
be relative Python `.py` paths. `max_hops` is optional and must be 0..2. Keep
the fixture block frozen after labels are decided. The assistant will serialize
the fixture block, compute the exact-byte SHA-256, replace the placeholder, and
run the existing fallback-off/on evaluator without executing source code.

For an independent evaluation, a separate author should prepare the expected
test labels without seeing GEO's selected-test output. If the same person or
assistant drafts fixtures and labels, the result is exploratory synthetic
evidence, not independent validation. The tool cannot authenticate authorship
or enforce a one-time evaluation.

That same-chat workflow is still valid for development: use the result to find
misses, refine tests, and guide implementation, while keeping it labeled as
development evidence. Do not wait for an independent evaluator to continue
ordinary implementation work.