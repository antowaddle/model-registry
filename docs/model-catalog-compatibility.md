# Model Catalog v1 OpenAPI compatibility

The checker adopts **oasdiff v1.33.0**, alongside the separate
[HTTP/database behavior suite](../catalog/internal/server/contract/README.md).
It needs no cluster or container runtime. The Python 3 checker uses only its standard library;
oasdiff is a standalone build tool, not a service dependency.

## Install the pinned tool

From the repository root, use the existing Go toolchain and repository yq:

```sh
GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@v1.33.0
make bin/yq
```

The checker rejects other oasdiff versions. Go-installed builds print `main` as
their display version; the checker verifies their embedded Go module version.
Official release binaries with the matching display version also work.
`--oasdiff`/`OASDIFF` and `--yq`/`YQ` can specify executable paths.

## Select trusted baselines explicitly

Baselines are **existing Git snapshots**, not mutable copies committed alongside
the proposed change. Supply a trusted target-branch revision and every applicable
supported-release revision. Each ref is resolved to a full commit SHA before
extracting `api/openapi/catalog-v1.yaml`.

The checker never falls back to `HEAD`, guesses a remote, fetches automatically,
or silently skips a missing baseline. Fetch the relevant revisions first. A
supported-release SHA must be confirmed by the release owners; none is invented
or hard-coded by this implementation.

This checkout names its provider remote `odh`, not `origin`. For a local
target-branch check, resolve that reference once, then pass its SHA:

```sh
baseline_sha=$(git rev-parse refs/remotes/odh/main)
python3 scripts/check_catalog_compatibility.py \
  --baseline-ref "$baseline_sha" \
  --output-dir /tmp/catalog-v1-compat-report
```

The output directory must be new. Repeat `--baseline-ref` to add release baselines;
**every** supplied baseline is compared. Add `--enforce` for a failing compatibility
gate after the pilot's report-only phase is approved for enforcement.

## What runs

1. Verify the pinned tool and materialize each baseline from its resolved SHA.
2. Regenerate the candidate using `scripts/merge_catalog_specs.sh` in a private
   temporary workspace. The developer's source and generated files are untouched.
3. Require an exact match with the committed bundle. Stale source/bundle pairs
   are an integrity failure even in report-only mode.
4. Validate both contracts. Invalid/unloadable contracts fail; existing validation
   warnings and informational findings are recorded, not suppressed.
5. Run `oasdiff breaking` against every baseline, with `allOf` flattening and the
   path filter `^/api/model_catalog/v1(/|$)`. Breaking warnings and errors are
   reported. Compatibility comparisons exclude other catalog plugin endpoints;
   freshness and validation cover the shared generated bundle as a whole.

The invocation uses an explicit empty oasdiff configuration, so a candidate-local
`.oasdiff.yaml` cannot hide findings. External references are disabled. Nothing
is uploaded to oasdiff's hosted service.

The pilot intentionally promotes `response-optional-property-removed` to `WARN`.
oasdiff normally treats these removals as informational, but Dashboard consumers
use optional fields such as `sourceId`. Removing a declared response field must
therefore be reviewed, even if it is not required by the schema. Additive optional
response fields remain compatible. This is a conservative Model Catalog policy,
not a change to the API's required fields; it covers all declared optional response
fields in the selected endpoints, not just the ones currently consumed.

The output contains `candidate.yaml`, `baseline-<full-SHA>.yaml` for each baseline,
`oasdiff-policy.json`, `oasdiff-levels.txt`, and `report.json`. The report records
the explicit policy and its digests, tool version,
candidate HEAD (not a claim that an uncommitted workspace is that commit), content
SHA-256 digests, resolved baseline SHAs, validation findings, breaking findings,
mode, and errors. The saved baseline files are evidence copies; Git remains their
source of truth. Archive this directory as CI evidence when integration is added.

Exit statuses:

| Status | Meaning |
| --- | --- |
| `0` | Compatible, or breaking findings recorded in report-only mode |
| `1` | Breaking findings in enforcement mode |
| `2` | Missing baseline/tool, wrong tool version, generation/validation failure, stale bundle, malformed tool output, or another integrity failure |

Always read `report.json.status`: report-only exit `0` does not mean compatibility.

## Regression tests

Use pytest from the Catalog Python client's existing development dependencies
(`catalog/clients/python/pyproject.toml`; its lockfile pins pytest 8.4.2), or install
that same version in an isolated test environment. From the repository root:

```sh
python3 -m pytest scripts/test_catalog_compatibility.py -v
```

The tests use the actual Catalog source specifications, bundler, yq, oasdiff, and
temporary Git repositories. They seed removed operations/fields, changed response
types, new required inputs, additive optional fields, stale bundles, broken
references, and missing baselines. They also verify multiple-baseline handling,
scope isolation, report-only/enforcement results, configuration isolation, and
that changing source and bundle together cannot move a pinned older baseline.
Small unit tests inject tool failures to verify fail-closed handling.

## Remaining integration

No CI workflows or API specifications are changed. To turn this into a trusted
PR check, CI must obtain baseline SHAs from the protected base/release policy,
pin the runner/tool/generator and their policy from trusted configuration, run the
regression tests and checker, and archive the report. A PR-controlled argument or
edited runner is not a security boundary. Start report-only as proposed.

oasdiff checks declared API compatibility; it does not prove implementation
behavior, authorization at a deployed proxy boundary, or consumer migration from
v1alpha1 to v1. Keep the HTTP/database suite and consumer review for those needs.
