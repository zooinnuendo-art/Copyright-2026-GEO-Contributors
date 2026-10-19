# GEO

Experimental Python analyzer that surfaces review hotspots and candidate tests
by comparing static dependencies with Git co-change history.

GEO is a review compass, not a correctness prover. It surfaces known dependency
paths, candidate tests, and files that often change together despite being
statically distant. A result means "inspect this evidence," not "this change is
safe" or "these tests are sufficient." GEO does not automatically edit analyzed
code or execute it.

**Status: experimental, local-first research tooling.** Synthetic regression
results are not evidence of general effectiveness on real projects. Dynamic
Python behavior is only partially visible; unresolved relations and extraction
limits are reported rather than filled in.

## Core Tools

| Tool | Purpose |
| --- | --- |
| [Dependency graph](tools/code_dependency_graph.py) | Bounded AST extraction of imports, calls, types, and global reads/writes; candidate test selection. |
| [Git temporal history](tools/git_temporal_history.py) | Read-only commit snapshots, graph/AST deltas, and heuristic file-rename tracking. |
| [History tension](tools/geo_history_tension_probe.py) | Rank disagreement between historical co-change and static distance, with explicit exclusions and uncertainty. |
| [Geodesic probe](tools/geo_geodesic_probe.py) | Weighted shortest-path and bidirectional-meeting evidence over known static relations; not semantic distance. |
| [External evaluation](tools/evaluate_external_geo_impact.py) | Evaluate supplied source fixtures against separate, hash-bound expected-test labels. |

## Install

After downloading or cloning this repository, open a terminal in its root:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

On Windows, activate with `.venv\Scripts\activate` instead. Python 3.10+ is
required; Git must be available on PATH for history analysis and the demo.
There are **no third-party runtime dependencies**. Installation uses setuptools
as a build dependency, which pip may download. No PyPI publication is implied:
`geo-code-analysis` is this project's local distribution name.

The installed commands work outside the checkout while this environment is
active:

| Command | Input and output |
| --- | --- |
| `geo-demo` | Self-contained temporary Git demo; JSON output. |
| `geo-graph ROOT --changed PATH` | Python snapshot and changed relative path; graph/test-candidate JSON. |
| `geo-history REPOSITORY REVISION ...` | Contiguous Git commits, oldest first; history JSON. |
| `geo-tension --repository ROOT --revisions REVISION ...` | Same-snapshot history tension; text by default, JSON with `--format json`. |

Existing `python -m tools...` commands remain supported. A non-editable
`python -m pip install .` is also supported and includes the demo's toy sources.
The historical `tools` and `examples` import package names are retained; use a
dedicated virtual environment to avoid collisions with other packages using
those generic names.

## Quick Start

After installation, from any directory:

```sh
geo-demo
```

Without installation, run `python3 -m examples.run_demo` from the project root.

The demo reads the small [shop project](examples/shop/), then creates a
temporary Git repository with a co-change and a module move. It demonstrates:

- `shop/pricing.py` selects `tests/test_checkout.py` as a candidate test.
- `shop/pricing.py` -> `shop/rates.py` is detected as a content-identical rename.
- `shop/policy.py` and `shop/rates.py` have tension `1.0` in the toy history:
  they co-changed but have no known static path.

The last item is deliberately a review prompt, not proof of a hidden
dependency. The demo prints JSON, makes commits **only inside its temporary
repository**, and removes that repository afterward. It leaves this project
and the sample source unchanged and requires no network access.

To inspect the sample's static graph directly:

```sh
python3 -m tools.code_dependency_graph examples/shop --changed shop/pricing.py
```

To run the sample's own unit test (separate from GEO's tests):

```sh
cd examples/shop
python3 -m unittest discover -s tests
```

Run the remaining commands in this README from the GEO root.

### Analyze Your Own Project

For a snapshot (no Git history needed):

```sh
geo-graph /path/to/project --changed pkg/core.py
```

The JSON contains `nodes`, `edges`, `status`, `issues`, and
`scope.linked_test_files`. These are candidate tests, not generated tests or
guaranteed impact coverage.

For history analysis, choose two or more consecutive first-parent commits
(a single commit is also accepted and compared with its parent):

```sh
geo-tension --repository /path/to/project \
  --revisions COMMIT_A COMMIT_B --top 10
geo-tension --repository /path/to/project \
  --revisions COMMIT_A COMMIT_B --format json > tension.json
```

Replace the commit placeholders with actual IDs, oldest first. Text shows pair
paths, tension, co-change counts, known-path evidence, and graph uncertainty.
JSON includes `pairs`, `files`, `noise_filter`, `graph_uncertainty`, and `source`.
For the toy demo, one pair has these values (an excerpt, not the complete schema):

```json
{
  "left_path": "shop/policy.py",
  "right_path": "shop/rates.py",
  "cochange_commits": 1,
  "structural_evidence": "static_path_not_found",
  "tension": 1.0,
  "hotspot": true
}
```

Hotspots do not cause a failed exit. Input errors and budget failures do.
The tool recommends review candidates; it does not generate tests or certify
that a change is correct.

## Development Workspace

Open [GEO.code-workspace](GEO.code-workspace) in its own VS Code window.
This directory contains real, independent files: no symlinks, shared application
source, or runtime imports from the AIN workspace. Changes here do not change AIN,
and changes in AIN are not automatically synchronized here.

## Requirements

- Python 3.10 or newer; analysis and tests use only the standard library.
- Git for temporal-history extraction and its integration tests.

Direct source execution needs no third-party packages. To run development
tests and research probes, use the project root:

```sh
python3 -m unittest discover -s tests
python3 -m tools.dependency_impact_benchmark
python3 -m tools.dependency_impact_benchmark --compare-fallback-import-forms
```

Analyze an explicitly supplied Python snapshot:

```sh
python3 -m tools.code_dependency_graph /path/to/project --changed pkg/core.py
```

Extract a contiguous Git revision series, oldest first:

```sh
python3 -m tools.git_temporal_history /path/to/repository COMMIT_A COMMIT_B
```

Commands do not implicitly analyze AIN. External repositories are supplied
explicitly as inputs; synthetic benchmarks use temporary fixtures.

## Git Rename Tracking

The extractor records Python `file_changes` using Git's NUL-delimited rename
detection at 50% similarity. Edited-rename detection is bounded by `max_files`;
unmatched moves remain additions/deletions. The result is heuristic identity,
not proof that two files have the same semantics.

Existing graph and AST deltas retain their path-based representation.
For detected renames, `delta.renamed_nodes` maps old/new node IDs with matching
symbol names and kinds, and `ast_delta.rename_aware` compares symbols after
projecting parent paths onto destination paths. A pure move has no rename-aware
AST changes; a move with edits can still report changed, added, or removed
symbols. Symbol renames themselves are not inferred.

The history tension probe prefers `file_changes` when available, counting actual
file edits instead of dependency-edge endpoints. It follows consecutive renames
to the final history revision, excludes content-identical moves, and keeps a
recreated old path separate from the moved file. Deleted file identities are
excluded because the comparison targets the final snapshot. Supply that
snapshot's graph and records in oldest-first order. Legacy history without
`file_changes` remains path-based and cannot reliably follow renames. Mixed
legacy/Git-change histories and noncontiguous Git-change records are rejected.
Output `path_identity` identifies which mode was used.

## History Tension Noise Controls

Use explicit exclusions when ranking history/static-distance disagreements:

```python
from tools.geo_history_tension_probe import analyze_history_structural_tension

result = analyze_history_structural_tension(
    graph,
    history,
    minimum_cochange_commits=2,
    excluded_path_patterns=["pkg/settings.py", "generated/*"],
)
```

No files are excluded by default, including utilities and constants. Patterns
use case-sensitive Python `fnmatch` on the whole repository-relative path:
`*` matches `/` as well as ordinary characters, `?` matches one character,
and `[abc]` matches a character set. These are not Git-ignore patterns:
`*.py` matches nested files too, `generated/*` does not match
`pkg/generated/file.py`, and a trailing slash does not implicitly match a
directory's contents. For Git histories, patterns match the final projected
path after rename tracking, not an old name.

Exclusions remove files from candidate pairs but preserve static graph paths
through those files. They do not remove revisions from the frequency denominator
or alter remaining pairs' Jaccard proximity, tension, hotspot thresholds, or
sorting. Existing record/path budgets still apply before filtering; the pair
budget applies to retained candidates.

`noise_filter` reports the configured patterns, number of distinct excluded
paths, and total excluded file/revision occurrences. Its counts and the `files`
table cover only edits in comparable revisions (not root or incomplete records,
deleted Git identities, or pure Git moves). Each `files` row includes matched
patterns, `changed_commits`, `change_frequency` (changed commits divided by all
comparable revisions), and `static_degree` (distinct neighboring files in the
undirected static graph). Pair rows repeat frequency and degree for each side.
Parallel edges and multiple symbols do not inflate file degree; excluded
neighbors still count. Files appearing only in excluded records are not listed.

These metrics explain rankings without automatically suppressing high-frequency
or highly connected files. A high score is not proof of a hidden dependency;
an exclusion is not proof that a file is safe. Legacy path-based histories can
still count dependency-edge endpoints as changes, so prefer Git `file_changes`
for edit-frequency interpretation.

## Static Evidence And Uncertainty

Each tension pair has `structural_evidence`: `known_static_path` when a path is
found, otherwise `static_path_not_found`. Neither is a correctness verdict.
In particular, a missing static path does not establish absence of a runtime
dependency. Distances, scores, hotspot flags, and ranking are unchanged.

`graph_uncertainty` preserves the supplied graph status, issues, and incomplete
causes. `incomplete_reported` is true if the graph reports `incomplete/review`
or any incomplete causes; false means no incompleteness was reported, not that
analysis is complete. Missing status is null, missing issue/cause lists stay
null, and their `*_reported` flags are false. Metadata describes the supplied
static graph, not every historical revision.

`unresolved_calls.reported_count` sums reported simple/attribute-call summary
counts. It is null when issue metadata is unavailable, and zero when an issue
list is present but reports no unresolved-call sites. This is neither a
call-resolution rate nor coverage of metaclasses, importlib, framework DI, or
all other dynamic behavior.

Summary examples are retained with `examples_truncated` when the count exceeds
the examples provided. They are not a complete per-file census:
`per_file_counts_available` is false. All uncertainty is explicitly
`whole_graph_not_pair_attributed`, including examples from excluded or unrelated
files. GEO does not invent per-pair confidence or conclude that a file absent
from the examples has no unresolved calls. Invalid supplied metadata raises
`ValueError` rather than silently becoming a clean result.

## Local History Tension CLI

The CLI reads Git commits directly or consumes the existing extractors' JSON
output. It does not execute analyzed source, change a repository, post PR
comments, or install Git hooks.

### Recommended: Aligned Git Snapshots In One Command

```sh
python3 -m tools.geo_history_tension_probe \
  --repository /path/to/repository --revisions COMMIT_A COMMIT_B \
  --exclude 'generated/*' --minimum-cochange-commits 2 --top 20
```

Supply contiguous first-parent commits, oldest first. The extractor reads Git
objects and passes its final graph directly to tension analysis, without a
second extraction or working-tree scan. HEAD may be newer and the working tree
may be dirty; staged, unstaged, and untracked files are ignored and left alone.
No checkout, worktree, or commit is created. `--format json` produces the complete
report. Both formats include `source` provenance: resolved revision IDs,
baseline, final commit/tree IDs, `git-tree://` graph root,
`snapshot_alignment: same_extraction`, and `working_tree_used: false`.

Git extraction limits are `--max-revisions` (20 by default), `--max-files`,
`--max-total-bytes`, `--max-metadata-bytes`, `--max-nodes`, `--max-edges`,
`--max-ast-symbols-per-file`, `--max-symbol-ast-nodes-per-file`, and
`--timeout-seconds` (per Git command). These apply only to repository mode.
Incomplete graphs remain explicit review evidence, not safety approvals.

For Python callers, `extract_git_temporal_history(...,
include_final_snapshot=True)` adds `final_snapshot` to its usual history result;
default API and history-extractor CLI output remain unchanged.

### Existing JSON Inputs

Alternatively, use a source snapshot matching the last commit:

```sh
python3 -m tools.git_temporal_history /path/to/repository COMMIT_A COMMIT_B > history.json
python3 -m tools.code_dependency_graph /path/to/final-commit-snapshot > graph.json
python3 -m tools.geo_history_tension_probe \
  --graph graph.json --history history.json \
  --exclude 'pkg/settings.py' --exclude 'generated/*' \
  --minimum-cochange-commits 2 --top 20
```

In JSON mode, the snapshot must match the final commit, not a newer or dirty working tree.
The CLI cannot verify this relationship for arbitrary JSON inputs; the caller
is responsible for snapshot alignment. Use new output paths when redirecting
extractor output: shell `>` overwrites existing files.

Text is the default. It shows ranking evidence, edit frequencies, static degrees,
exclusions, and graph-wide issues/incomplete causes (including supplied unresolved
call examples). `--top` limits only the number of displayed pairs, not analysis
or audit metadata. For the complete API-shaped report:

```sh
python3 -m tools.geo_history_tension_probe \
  --graph graph.json --history history.json --format json > tension.json
```

JSON is emitted alone on stdout and always includes all retained pairs.
`--exclude` is repeatable; quote patterns so the shell does not expand them.
`--hotspot-threshold` sets the existing hotspot threshold without changing scores.
Record/path/pair budgets are available as `--max-history-records`,
`--max-paths-per-revision`, `--max-unique-paths`, and `--max-pairs`.
`--max-input-bytes` limits each input document (default 64 MiB). Inputs must be
UTF-8 JSON objects without duplicate keys or non-standard NaN/Infinity constants.
The byte limit applies only to JSON mode. `--graph`/`--history` cannot be
combined with `--repository`/`--revisions`.

Successful analysis exits 0 even with hotspots, incomplete graph metadata, or
no candidates. Invalid inputs, file-read failures, or exceeded budgets produce
an error on stderr and exit 2, without a success report on stdout. A successful
exit is not approval of a change. For now this is a manually invoked local
workflow; automatic PR or hook integration remains out of scope.

## Research Provenance

GEO was extracted from AIN into this independent workspace. Application-specific
reference logic and real-application snapshot measurements are not included in
this public source tree. Existing `ain.geo-impact-*.v1` JSON schema identifiers
are retained for compatibility with existing fixture and label documents.

The [experiments](experiments/) directory contains an evaluation protocol and
minimal synthetic fixture/label templates, not private evaluation cohorts.
The [examples](examples/) directory contains a fictional checkout project.

## Separate Development

Use this directory as the source of truth for future GEO work. Open the AIN
workspace in a separate window for AIN changes. The previous AIN-contained GEO
view is a legacy view, not a synchronized copy of this project.

## Publication And License

### Continuous Integration

[CI](.github/workflows/ci.yml) runs on pushes, pull requests, and manual dispatch.
Its 15-job matrix covers Python 3.10 through 3.14 on Ubuntu, macOS, and Windows.
Each job installs the editable project, runs GEO's regression suite and the toy
project test, builds a wheel, replaces the editable installation with that
wheel, and checks installed commands from outside the checkout.

The [installed-package check](scripts/check_installed.py) verifies CLI help,
actual demo/graph/tension JSON, bundled demo sources, license metadata, and the
absence of runtime dependencies. It also rejects imports from the source tree.
The newline-in-filename integration test is skipped on Windows, whose filesystem
does not support that filename; the other rename tests still run there.
The workflow has read-only repository permissions, does not persist checkout
credentials, and contains no publishing steps or secrets. GitHub-hosted results
are available only after the workflow has been pushed and run; local checks do
not establish that every OS/Python matrix combination passes.

Local Git initialization is separate from publishing. No remote repository or
initial commit is required to run GEO. Before publishing, review all files
(including historical experiment notes and provenance) for permission to share.

The owner has selected **Apache License 2.0** and confirmed that they hold the
rights needed to publish and license the AIN-derived code and materials.
See [LICENSE](LICENSE) for the unmodified official license text.

**Copyright 2026 GEO Contributors**

See [NOTICE](NOTICE) for the project attribution; both LICENSE and NOTICE are
included in distribution packages. The example notice in the license appendix
remains the unmodified standard application template. Preserve any applicable
upstream notices. This local preparation is not a legal opinion or independent
verification of ownership.

Application-specific reference code, its tests, and real-snapshot diagnostics
have been removed from the public tree and retained separately by the owner.
Do not put private evaluation inputs or outputs inside this checkout; pass
external paths to the evaluator. Removing or anonymizing content does not by
itself resolve ownership or confidentiality obligations.

Legacy `ain.geo-impact-*.v1` schema strings are compatibility identifiers, not
network endpoints or application imports; they are intentionally retained.
Toy Git identities use reserved `example.invalid` addresses.

### Publish Files, Not This Workspace's Git Metadata

The working repository may contain local agent checkpoint refs even before a
user-created commit exists. Do not assume that its Git object database is empty
or sanitized. For an initial public release, copy only reviewed source files
into a separate new directory, excluding `.git`, caches, build outputs, virtual
environments, and private material. Initialize a new repository there and
review the exact initial commit before adding a remote or pushing. Do not copy
or mirror this workspace's refs, reflogs, or object database. No Git history is
rewritten by the source-tree sanitization.

A local text-pattern review found no literal home-directory paths, network
endpoints, private-key headers, or apparent credential assignments in the
reviewed Python, JSON, and Markdown files before the license was added. Generic
`Token` test classes and Python's `tokenize` import are not credentials. This
limited check does not establish the absence of all secrets, proprietary
knowledge, or third-party obligations. Review new files and the exact staged
diff again before any commit or push. No remote or public release is configured.
