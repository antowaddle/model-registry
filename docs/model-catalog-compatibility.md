# Model Catalog v1 OpenAPI compatibility

Compatibility for the Model Catalog v1 API is checked by the generic
**openapi-compat** engine, driven by the descriptor at
[`.rhoai/contracts.yaml`](../.rhoai/contracts.yaml), alongside the separate
[HTTP/database behavior suite](../catalog/internal/server/contract/README.md).
Neither needs a cluster. This repository contributes only its descriptor
and its existing bundler (`scripts/merge_catalog_specs.sh`); the comparison
engine itself — baseline resolution, tool pinning, oasdiff invocation,
severity policy, report generation — is generic and lives in
[`red-hat-data-services/contract-tests`](https://github.com/red-hat-data-services/contract-tests),
not duplicated here.

(This replaces the earlier `scripts/check_catalog_compatibility.py` /
`scripts/test_catalog_compatibility.py` prototype, now generalized so a
second provider repository never needs to copy this logic.)

## Get the shared tooling

Clone the shared repository:

```sh
git clone https://github.com/red-hat-data-services/contract-tests.git ../contract-tests
```

Point `CONTRACT_TESTS` at that checkout for the regression tests below, and
invoke its CLI directly for a manual run.

## Install the pinned tool

From this repository's root, use the existing Go toolchain and repository yq:

```sh
GOBIN="$PWD/bin" go install github.com/oasdiff/oasdiff@v1.33.0
make bin/yq
```

The checker rejects any other oasdiff version. Go-installed builds print
`main` as their display version; it verifies their embedded Go module
version instead. `--oasdiff`/`OASDIFF` and `--yq`/`YQ` can specify
executable paths.

## Select trusted baselines explicitly

Baselines are **existing Git snapshots**, not mutable copies committed
alongside the proposed change. Supply a trusted target-branch revision and
every applicable supported-release revision. Each ref is resolved to a full
commit SHA before extracting the declared artifact
(`api/openapi/catalog-v1.yaml`).

The checker never falls back to `HEAD`, guesses a remote, fetches
automatically, or silently skips a missing baseline. Fetch the relevant
revisions first. A supported-release SHA must be confirmed by the release
owners; none is invented or hard-coded.

This checkout names its provider remote `odh`, not `origin`. For a local
target-branch check, resolve that reference once, then pass its SHA:

```sh
baseline_sha=$(git rev-parse refs/remotes/odh/main)
OASDIFF="$PWD/bin/oasdiff" YQ="$PWD/bin/yq" \
python3 ../contract-tests/cmd/contract-tests/cli.py check \
  --repo . \
  --contract-set .rhoai/contracts.yaml \
  --baseline-ref "$baseline_sha" \
  --output-dir /tmp/catalog-v1-compat-report
```

The output directory must be new. Repeat `--baseline-ref` to add release
baselines; **every** supplied baseline is compared. Add `--enforce` for a
failing compatibility gate after the pilot's report-only phase is approved
for enforcement.

## What the descriptor declares

`.rhoai/contracts.yaml` registers one contract, `model-catalog-v1`:

- **`declaredArtifact`** — the committed, bundled spec the candidate is
  diffed against and verified to match (`api/openapi/catalog-v1.yaml`).
- **`matchPath`** — scopes comparisons to `^/api/model_catalog/v1(/|$)`, so
  changes to the other bundled plugin APIs (MCP, agent, skill) are out of
  scope for this contract.
- **`policy.oasdiffVersion`** — the pinned tool version (`1.33.0`).
- **`policy.yqVersion`** — the pinned bundling-tool version (`4.45.1`).
- **`policy.severityOverrides`** — promotes `response-optional-property-removed`
  from oasdiff's default `info` to `warn`. Dashboard consumers use optional
  fields such as `sourceId`; removing a declared response field must
  therefore be reviewed, even if it is not required by the schema. This is a
  conservative Model Catalog policy, not a change to the API's required
  fields, and it covers all declared optional response fields in scope, not
  only the ones currently consumed.
- **`generator`** — points the `openapi.bundle.yq-merge` adapter at this
  repository's existing bundler and its source layout
  (`api/openapi/src/{catalog-v1.yaml,lib/*.yaml,plugins/*-v1.yaml}`).

See the central repository's
[`schemas/contract-set.schema.json`](https://github.com/red-hat-data-services/contract-tests/blob/main/schemas/contract-set.schema.json)
and
[`docs/onboarding/openapi-compat.md`](https://github.com/red-hat-data-services/contract-tests/blob/main/docs/onboarding/openapi-compat.md)
for the general descriptor reference and onboarding steps that would apply
to any other provider adopting this engine.

## What runs

1. Verify the pinned tool and materialize each baseline from its resolved SHA.
2. Regenerate the candidate using `scripts/merge_catalog_specs.sh` in a
   private temporary workspace. The developer's source and generated files
   are untouched.
3. Require an exact match with the committed bundle. Stale source/bundle
   pairs are an integrity failure even in report-only mode.
4. Validate both contracts. Invalid/unloadable contracts fail; existing
   validation warnings and informational findings are recorded, not
   suppressed.
5. Run `oasdiff breaking` against every baseline, with `allOf` flattening
   and the descriptor's `matchPath`. Breaking warnings and errors are
   reported.

The invocation uses an explicit empty oasdiff configuration, so a
candidate-local `.oasdiff.yaml` cannot hide findings. External references
are disabled. Nothing is uploaded to oasdiff's hosted service.

The output contains `candidate.yaml`, `baseline-<full-SHA>.yaml` for each
baseline, `oasdiff-policy.json`, `oasdiff-levels.txt`, and `report.json`.
The report records the explicit policy and its digests, tool version,
candidate HEAD (not a claim that an uncommitted workspace is that commit),
content SHA-256 digests, resolved baseline SHAs, validation findings,
breaking findings, mode, and errors. Archive this directory as CI evidence
when integration is added.

Exit statuses:

| Status | Meaning |
| --- | --- |
| `0` | Compatible, or breaking findings recorded in report-only mode |
| `1` | Breaking findings in enforcement mode |
| `2` | Missing baseline/tool, wrong tool version, generation/validation failure, stale bundle, malformed tool output, or another integrity failure |

Always read `report.json.status`: report-only exit `0` does not mean compatibility.

## Regression tests

`scripts/test_catalog_contract_compat.py` proves this repository's real
descriptor and bundler integrate correctly with the generic engine (it does
not re-derive the engine's own generic test cases, which live in the
shared repository). Requires `CONTRACT_TESTS` to point at a local
checkout of that repository:

```sh
CONTRACT_TESTS=../contract-tests \
  python3 -m pytest scripts/test_catalog_contract_compat.py -v
```

It seeds a removed operation, a removed optional response field, an
additive optional field, a stale bundle, an out-of-scope plugin change, a
missing baseline, and a wrong-API-version baseline, and asserts the engine's
response to each.

## CI integration

`check-model-catalog-contract.yml` calls the shared reusable workflow by the
full commit SHA pinned below. It resolves the PR base SHA locally and runs
in report-only mode; compatibility findings do not fail the PR yet.
Infrastructure and integrity failures still fail closed.

oasdiff checks declared API compatibility; it does not prove implementation
behavior, authorization at a deployed proxy boundary, or consumer migration
from v1alpha1 to v1. Keep the HTTP/database suite and consumer review for
those needs.
