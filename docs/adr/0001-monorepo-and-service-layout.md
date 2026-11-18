# ADR-0001 — Monorepo with independent, self-contained services

- **Status:** Accepted
- **Date:** 2026-11-18
- **Stage:** 1

## Context

Four FastAPI services, a Next.js app, and shared infrastructure need to live in
one repository for a five-day MVP, while remaining separable later. The main
risk in microservice monorepos is accidental coupling: a shared "common"
library that every service imports turns four deployables into one distributed
monolith with a single release train.

## Decision

- One repository, one top-level folder per concern:
  `backend/services/<name>`, `frontend/`, `infrastructure/`, `docs/`.
- **Each backend service is an independent Python project** with its own
  `pyproject.toml`, `uv.lock`, `Dockerfile`, tests, and Alembic directory.
- The Docker build context for a service is **that service's directory**, so a
  service image cannot accidentally depend on files outside it.
- **No shared runtime library.** Small cross-cutting utilities (settings,
  logging, error envelope) are duplicated per service.
- No Python workspace/monorepo tooling. `uv` runs per service.

## Consequences

**Positive**

- Any service can be extracted into its own repository by copying a folder.
- No version-skew problem: services cannot import an incompatible common lib.
- Docker layer caching is straightforward; builds are small.
- Services can upgrade dependencies independently.

**Negative**

- ~30–60 lines duplicated per service (logging setup, error types).
- A cross-cutting change must be applied N times.

**Revisit when:** duplication exceeds three copies of non-trivial logic, at
which point package the stable subset and publish it as a versioned artifact —
not a relative-path import.

## Alternatives considered

| Alternative | Why rejected |
|---|---|
| `uv` workspace with one lockfile | Couples dependency resolution across services; Docker contexts get large; contradicts independent deployability. |
| Shared `backend/libs/common` imported by path | Silently couples releases; every image build needs the repo root as context. |
| Separate repositories | Too much ceremony and no shared CI for a five-day MVP. |
