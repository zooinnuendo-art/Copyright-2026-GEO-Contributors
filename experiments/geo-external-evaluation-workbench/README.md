# GEO External Evaluation Workbench

## Complete It in This Chat

No terminal command or manual file creation is needed. Paste two fenced JSON
blocks directly into your next message, using
[`CHAT_INPUT_TEMPLATE.md`](CHAT_INPUT_TEMPLATE.md) as the schema:

1. A fixture block containing the cohort ID, changed Python files, source
   snapshots, and cases.
2. An oracle-label block listing the affected test files for each case.

Set the same new `cohort_id` in both blocks. `case_id` values must match. Leave
`fixtures_sha256` as `ASSISTANT_WILL_COMPUTE_FROM_FIXTURE_BLOCK`; the assistant
will hash the exact pasted fixture JSON and bind the label block before scoring
it in memory. The source is parsed but never executed. The result is returned
in the chat and does not require writing a report file.

### Development Mode: No External Evaluator Required

To continue implementation work, the fixture author and label author may be
the same person, and the assistant may help draft both. Paste the cases and
expected-test labels here and ask for a development evaluation. The assistant
will score them with the current GEO, report misses and uncertainty, and use
the result to guide the next code/test iteration. Record this as exploratory
development evidence; an external evaluator is not a prerequisite for
implementation progress.

For a confirmatory claim about general effectiveness, have someone other than
the implementation author determine affected-test labels without seeing GEO's
output. The tool cannot authenticate authorship. Same-session authoring is
useful for defect discovery, but must not be presented as independent or
real-repository evidence.

Do not edit a fixture after labels have been prepared. A changed fixture needs
a new label decision and a newly computed hash. If evaluation results lead to
code or policy changes, treat that cohort as development data and do not reuse
it for confirmation.

## Local Alternative

The files in this directory are optional examples, not an already scored
cohort. For a local workflow, use the CLI documented in
`experiments/geo-external-evaluation-protocol.md`.

For the built-in development-only paired import-form probe, run
`python3 -m tools.dependency_impact_benchmark --compare-fallback-import-forms`.
It compares used/unused deleted-symbol aliases, deleted provider modules, and
relative imports, then separates fallback-added recommendations from Actionable
and abstained candidates. These hand-labeled fixtures are regression probes,
not independent or real-repository evaluation.