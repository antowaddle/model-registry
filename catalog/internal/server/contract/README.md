# Model Catalog v1 consumer contract tests

For schema evolution checks against Git baselines, see the companion
[oasdiff compatibility suite](../../../../docs/model-catalog-compatibility.md).

Run from the repository root with a Docker-compatible runtime available:

```sh
go test ./catalog/internal/server/contract -run TestModelCatalogV1Contract -count=1 -v
```

For Colima on macOS, use its host socket and the VM-local socket path for the
Testcontainers cleanup container (substitute your home directory below):

```sh
colima start
DOCKER_HOST="unix://$HOME/.colima/default/docker.sock" \
TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE=/var/run/docker.sock \
go test ./catalog/internal/server/contract -run TestModelCatalogV1Contract -count=1 -v
```

The existing PostgreSQL Testcontainers helper creates and tears down a disposable
`postgres:16` database. No Kubernetes cluster or separately deployed catalog is
needed. The first run may download Go dependencies and the database image.

The suite serves the real v1 router, controllers, services, and database-backed
model provider over a local HTTP connection. Fixtures are stored through the real
repositories. Neither provider queries nor HTTP responses are mocked.

Hand-written consumer projections freeze expected JSON field names and types;
they do not use the provider's generated response models. Generated types are
used only to configure source fixtures. Missing/null consumer-required fields,
changed types, routing/status regressions, lost or repeated page items, and
incorrect filtering fail the tests. Additional response fields remain compatible.
Some optional schema fields are required only in scenarios whose fixtures supply
them; this does not make them universally required in the API.

Coverage includes source/label discovery, model detail and source isolation,
slash/colon names, model/artifact page traversal, equal-sort-key cursor behavior,
source/label/property filters, case-insensitive search, ordering, empty arrays,
artifact discriminators and URIs, performance artifact selection, filter options,
and JSON 400/404 errors. `size` is the current page count, not the total count.

These are prospective **v1** expectations, not evidence that the dashboard's
current v1alpha1 client is v1-compatible. Consumer owners should review the
expectations before treating them as a release gate. This suite complements the
existing BFF mocks and provider unit tests; it does not replace them.

Not included: auth/proxy boundaries, deployed infrastructure, source loading,
preview/status mutations, performance recommendation calculations, or OpenAPI
backward-compatibility diffing. CI and API specifications are unchanged.
