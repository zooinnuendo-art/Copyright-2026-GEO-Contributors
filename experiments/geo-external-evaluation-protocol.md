# GEO External Fixture Evaluation Protocol

Status: intake and scoring tool implemented; no externally authored cohort has
been supplied or scored yet.

## Purpose

`tools/evaluate_external_geo_impact.py` lets a fixture author provide source
snapshots separately from an oracle author/reviewer-provided list of affected
test files. GEO parses each Python snapshot and compares test selection with
the oracle labels, with import-use fallback disabled and enabled. The tool
never executes fixture source.

The tool does not authenticate either author, prove they are independent, or
enforce a one-time run globally. For a confirmatory holdout, the evaluator must
freeze the source/label files and GEO revision before exposing those files to
the implementation author, record the hashes, and run the cohort once. If the
results inform a code or policy change, treat that cohort as development data
and do not reuse it as confirmation.

## Fixture Input

Input is strict UTF-8 JSON with no duplicate object keys, non-finite values, or
unknown fields. Example:

```json
{
  "schema_version": "ain.geo-impact-fixtures.v1",
  "cohort_id": "review-cohort-001",
  "cases": [
    {
      "case_id": "change-001",
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

Each case has `case_id`, non-empty `changed_files`, and a non-empty mapping of
relative `.py` paths to source strings. `max_hops` is optional and defaults to
0; allowed range is 0..2. Paths must be relative POSIX paths without `..`.
Limits are 100 cases, 1,000 files per case, 2 MiB source text per case, and
8 MiB per input document.

## Oracle Labels

The oracle file is separate strict JSON. `fixtures_sha256` is the SHA-256 of
the exact fixture file bytes, including whitespace/newline. Each case must
have exactly one label row; `expected_test_files` is the adjudicated set of
tests affected by that change.

```json
{
  "schema_version": "ain.geo-impact-labels.v1",
  "cohort_id": "review-cohort-001",
  "fixtures_sha256": "<64 lowercase hex characters for exact fixture bytes>",
  "labels": [
    {
      "case_id": "change-001",
      "expected_test_files": ["tests/test_core.py"]
    }
  ]
}
```

Hash binding prevents accidentally pairing labels with a different fixture
file. It does not prove who created the labels, when they were frozen, or
whether they are correct.

## Run

### Directly in Copilot Chat

Paste the two fenced JSON blocks from
`experiments/geo-external-evaluation-workbench/CHAT_INPUT_TEMPLATE.md` into the
conversation. Leave `fixtures_sha256` set to
`ASSISTANT_WILL_COMPUTE_FROM_FIXTURE_BLOCK`. The assistant computes the exact
fixture-text SHA-256, validates the two documents, runs the evaluator in memory,
and returns the report in chat. No input/output files or user terminal command
are required. The same limits and caveats below apply.

For exploratory development, the same person or assistant may author both
fixtures and expected-test labels in the chat. This is an allowed workflow for
debugging, regression discovery, and implementation iteration; an external
evaluator is not a prerequisite. Label such output as development evidence. For
a confirmatory/generalization claim, labels should be frozen without GEO's
selection output and authored or reviewed independently of the implementation
author. This chat workflow cannot authenticate that separation.

### Local CLI (Optional)

Use a new output path; existing outputs are never overwritten:

```sh
python3 -m tools.evaluate_external_geo_impact \
  --fixtures /path/to/fixtures.json \
  --labels /path/to/labels.json \
  --output /path/to/new-report.json
```

The report includes fixture and label hashes, per-case selected/recalled/missed
and unnecessary tests, local/global uncertainty evidence, and aggregate
affected-test recall, actionable recall, precision, unnecessary-selection
rate, and expected-test abstention rate for fallback off/on. It marks
`label_producer_authenticated=false`,
`fixture_author_independence_verified=false`, and
`fixture_sources_executed=false`.

The top-level `fallback_delta` compares the two modes per case. It reports
newly recommended candidates, which are expected affected tests, which of those
are Actionable under current static evidence, which remain abstained, and which
are unnecessary. A fallback-only recommendation with a boundary remains a
run/review candidate but is not counted as Actionable coverage.

Each selected test is retained in `recommended_test_files` with
`recommendation="run_candidate"`. Evidence confidence is reported separately:
tests with a relevant boundary also appear in `review_required_test_files`,
while tests with no known boundary appear in
`no_known_boundary_test_files`. “No known boundary” is not a correctness or
safety proof. `recommended_affected_recall` measures candidate-generation
coverage; `actionable_affected_recall` and
`verified_under_static_evidence_recall` exclude selected tests with relevant
uncertainty. A boundary lowers confidence but does not silently delete the test
recommendation.

An expected test with a relevant local depth-limit boundary is counted as
abstained even when it was not selected. Global parse/read/budget incompleteness
abstains every expected test in that case. Unresolved call/attribute summaries
taint a selected test when an example's `source` function is in that test's
statically known call closure. This covers dynamic behavior inside a changed
provider that the candidate calls, without tainting a different test that calls
another function in the same file. If an example has no source symbol, the
evaluator falls back to route-file matching. A truncated summary conservatively
taints all selected paths. Test-call-depth causes remain local to their test
path. A miss without a recorded uncertainty reason remains a miss, not a
successful abstention.

Results are static-analysis/test-file-selection evidence only. They do not
prove semantic impact, code correctness, safety, or generalization from a small
cohort.