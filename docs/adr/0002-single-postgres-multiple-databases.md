# ADR-0002 — One Postgres instance, three databases, three roles

- **Status:** Accepted
- **Date:** 2026-11-18
- **Stage:** 1

## Context

The specification requires database-per-service ownership (`auth_db`,
`profile_db`, `wallet_db`) but gives no hosting topology. Options are three
Postgres containers (true isolation, triple the resources and Compose surface)
or one container with three logical databases.

## Decision

Run **one `postgres:16-alpine` container** containing three databases, each
owned by a dedicated login role:

| Database | Role |
|---|---|
| `auth_db` | `auth_svc` |
| `profile_db` | `profile_svc` |
| `wallet_db` | `wallet_svc` |

The init scripts:

1. Create roles with `NOSUPERUSER NOCREATEDB NOCREATEROLE`.
2. Create each database with its service role as owner (`TEMPLATE template0`).
3. `REVOKE ALL ON DATABASE ... FROM PUBLIC` and grant `CONNECT` only to the
   owning role.
4. `ALTER SCHEMA public OWNER TO <role>` and revoke `PUBLIC` create rights, so
   each service owns the only schema it can reach.

There are **no cross-database foreign keys** and no shared tables. `user_id`
is an opaque UUID owned by Auth.

## Consequences

**Positive**

- One container to start, back up, and debug; fits a five-day budget.
- Ownership is still enforced: a service cannot connect to another service's
  database, so accidental joins are impossible rather than merely discouraged.
- Each service keeps its own Alembic history.

**Negative**

- Shared failure domain: one crash or `max_connections` exhaustion affects all
  three services.
- Logical, not physical, isolation — a superuser can still read everything,
  and a volume loss loses all three databases together.
- Connection budget must be shared: each service uses a small pool (5 + 5).

## Alternatives considered

| Alternative | Why rejected for Stage 1 |
|---|---|
| Three Postgres containers | ~3× memory and a larger Compose file for no MVP-visible benefit; still no real HA. |
| Schema-per-service in one database | Weaker boundary than separate databases; encourages cross-schema joins. |
| Single shared database | Violates the stated ownership model outright. |

**Migration path:** because DSNs are injected per service, moving to
per-service instances is an infrastructure change only — no application code
changes. Revisit before production.
