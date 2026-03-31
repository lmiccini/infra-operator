# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

infra-operator is a Kubernetes operator for OpenStack infrastructure services (part of the openstack-k8s-operators ecosystem). It manages RabbitMQ, Memcached, Redis, InstanceHA, DNS (DNSMasq/DNSData), networking (NetConfig, IPSet, Reservation, BGPConfiguration), and Topology resources. Built with Kubebuilder v4 and operator-sdk, domain `openstack.org`.

## Common Commands

### Build & Run
- `make build` - Build the manager binary
- `make run` - Run controller locally (sets ENABLE_WEBHOOKS=false)
- `make run-with-webhook` - Run locally with webhooks enabled
- `make manifests` - Regenerate CRDs, RBAC, and webhook manifests (run after API type changes)
- `make generate` - Regenerate DeepCopy methods (run after API type changes)

### Testing
- `make test` - Run all tests (includes manifests, generate, fmt, vet, envtest setup)
- Run a single test file: `KUBEBUILDER_ASSETS=$(bin/setup-envtest --bin-dir bin use 1.31 -p path) OPERATOR_TEMPLATES=$(pwd)/templates bin/ginkgo --trace ./test/functional/ -- --focus "description match"`
- Run a specific test: make test GINKGO_ARGS='--focus="XXX"'
- `make test-instanceha` - Run InstanceHA-specific tests (shell-based)
- Tests use Ginkgo/Gomega with envtest (controller-runtime test framework)
- Kuttl integration tests: `make infra_kuttl` (run from install_yamls repo root); test cases in `test/kuttl/tests/`
- Prefer to save the tests output to a text file under /tmp/ so the results can be analyzed without having to rerun them multiple times

### Linting
- `make fmt` - Run go fmt
- `make vet` - Run go vet (both root and apis modules)
- `make golangci` - Run golangci-lint (requires CI_TOOLS_REPO)
- `make operator-lint` - Run operator-lint
- `make tidy` - Run go mod tidy on both modules

### Code Generation
- `make gowork` - Generate/update go.work file
- `make crd-schema-check` - Validate CRD schema changes against base branch

## Architecture

### Multi-module Repository
The repo has two Go modules linked via `go.work`:
- `.` (root) - Controllers, webhooks, and main binary
- `./apis` - API type definitions (CRD types), published separately so other operators can import them without pulling in controller dependencies

### Directory Structure
- `apis/<group>/v1beta1/` - API types (e.g., `apis/rabbitmq/v1beta1/`, `apis/network/v1beta1/`)
- `internal/controller/<group>/` - Reconciler implementations
- `internal/webhook/<group>/v1beta1/` - Webhook implementations (validation/defaulting)
- `internal/<component>/` - Internal business logic packages (e.g., `internal/ipam/`, `internal/dnsmasq/`, `internal/bgp/`)
- `pkg/rabbitmq/` - Public package for RabbitMQ utilities
- `templates/` - ConfigMap/deployment templates consumed at runtime via `OPERATOR_TEMPLATES` env var
- `test/functional/` - Ginkgo-based envtest functional tests
- `test/kuttl/tests/` - Kuttl integration test cases (e.g., rabbitmq, memcached, redis scenarios)
- `test/scripts/` - Test helper scripts (e.g., `test-ipset.sh`)
- `apis/test/helpers/` - Shared test helpers for API tests
- `docs/` - Architecture and upgrade documentation
- `config/` - Kustomize manifests for CRDs, RBAC, webhooks, and deployment
- `hack/` - Helper scripts (webhook setup, CRD schema checking)

### Key Patterns
- **API groups**: rabbitmq, memcached, redis, instanceha, network, topology - each under `<group>.openstack.org`. Note: topology is API-only (no dedicated controller); it provides types consumed by other controllers (e.g., instanceha)
- **Webhook separation**: Webhooks live in `internal/webhook/` (not in the API package), registered via `Setup*WebhookWithManager` functions
- **Defaults**: Each API group has a `SetupDefaults()` function called from `cmd/main.go` before webhooks are registered
- **lib-common dependency**: Heavy use of `github.com/openstack-k8s-operators/lib-common/modules/common` for shared operator utilities (conditions, endpoints, etc.)
- **Container runtime**: Uses `podman` (not docker) for image builds
