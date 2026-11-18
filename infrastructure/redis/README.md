# Redis — rate limiting only

Stage 1 uses Redis for exactly one thing: fixed-window rate-limit counters at
the gateway. Counters are disposable, so persistence is deliberately off.

| Setting | Value | Why |
|---|---|---|
| `maxmemory` | 128 MB | Counters are tiny and expire; a hard cap prevents runaway memory. |
| `maxmemory-policy` | `allkeys-lru` | Under pressure, evicting a counter only weakens throttling briefly. Never blocks writes. |
| `save` / `appendonly` | off | No value in restoring rate-limit windows after a restart. |
| `protected-mode` | off | The port is not published and is only reachable on the `app` network. |

## Production note

`protected-mode no` is acceptable here only because the base Compose file does
not publish port 6379 and the network is private. Before any real deployment:

1. Set `requirepass` (or use ACLs) and pass the password to the gateway via
   `REDIS_URL=redis://:<password>@redis:6379/0`.
2. Keep the port unpublished.
3. Consider `volatile-lru` if a mix of disposable and durable keys is ever
   introduced.

## Logical databases

| Index | Use |
|---|---|
| 0 | runtime rate limiting |
| 1 | reserved for tests |

## Verifying manually

```bash
docker compose exec redis redis-cli ping          # PONG
docker compose exec redis redis-cli info memory   # maxmemory_human: 128.00M
docker compose exec redis redis-cli config get maxmemory-policy
```
