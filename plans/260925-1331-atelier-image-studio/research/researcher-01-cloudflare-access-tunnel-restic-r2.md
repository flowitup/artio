# Research: Cloudflare Access/Tunnel and restic-to-R2 for Atelier

Date: 2026-09-25 · Scope: infrastructure facts for the Atelier plan, no changes made to any server or cloud account.

## Top-line answers

1. Verify the JWT from the `Cf-Access-Jwt-Assertion` header (fall back to the `CF_Authorization` cookie), fetch signing keys from `https://flowitupteam.cloudflareaccess.com/cdn-cgi/access/certs`, and check `aud`, `iss`, `exp` (PyJWT does this by default) plus, for user requests, `email`. Use PyJWT's `PyJWKClient` with its default 5-minute JWK-set cache; that is short enough to absorb Cloudflare's every-6-weeks key rotation (old key stays valid 7 days) without any extra caching code.
2. Service tokens authenticate via `CF-Access-Client-Id` / `CF-Access-Client-Secret` headers and still produce a normal Access JWT at the origin, just with `common_name` (the client ID) instead of `email` and an empty `sub`. Give the Atelier Access application a **Service Auth** policy in addition to the owner's email **Allow** policy.
3. For the shared tunnel, validate the edited config locally before touching the live file (`cloudflared tunnel ingress validate`), route DNS with `cloudflared tunnel route dns`, and expect **no hot reload** — Cloudflare's own guidance for changing a locally-managed tunnel's config is to run a second `cloudflared` replica and cut over, specifically to avoid downtime on `systemctl restart`. Given the "no exceptions" rule for touching `folio-prod-1`, the replica method is the lower-risk path; a bare restart is the fallback everyone falls back to but its downtime window is undocumented.
4. Cloudflare's current documented proxy read timeout is 125 s (524 past that), not exactly 100 s as commonly cited; nothing in the tunnel docs raises or lowers this per-hostname. The brainstorm's `spawn()` + non-blocking `get(timeout=0)` polling design is the correct way to stay under it — keep it, and keep HTMX polling short (client re-polls every few seconds; never hold a request open waiting on the job).
5. Use `restic` in `s3:https://<ACCOUNT_ID>.r2.cloudflarestorage.com/<bucket>` form, region `auto`, from the official `restic/restic` Docker image, with `forget --prune` for retention and weekly `check --read-data-subset=...` plus a `restore --dry-run` test. **Do not** turn on R2 bucket locks for this bucket — they block the deletes that `prune` needs.
6. For the nightly SQLite snapshot, use Python's `sqlite3.Connection.backup()`, not the CLI `.backup` or `VACUUM INTO` — same WAL-safe guarantee, no extra OS package, and a progress callback Atelier can use for its "last backup status" UI.
7. At 5–50 GB with one restic run per night, R2 cost is expected to land inside the free tier (10 GB-month storage, 1M Class A, 10M Class B ops/month) for most of that range, and at most a couple of dollars a month at the top end.

---

## 1. Access JWT at the origin

The JWT reaches the origin two ways: the `Cf-Access-Jwt-Assertion` request header (Cloudflare's recommended path) and the `CF_Authorization` cookie for browser requests; the docs note the cookie "is not guaranteed to be passed," so check the header first and fall back to the cookie. Source: [Validate JWTs](https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/validating-json/).

The JWKS endpoint is `https://<team-name>.cloudflareaccess.com/cdn-cgi/access/certs`, returning current and previous keys. Cloudflare's own guidance: "Validate tokens using the external endpoint rather than saving the public key as a hard-coded value," and match the JWT's `kid` against the returned key set rather than reading a single `public_cert` value that a cache might have gone stale on. Same source. Key rotation: "By default, Access rotates the signing key every 6 weeks... Previous keys remain valid for 7 days after rotation." Same source.

Claims to verify: `aud` (the app's Application Audience tag), `iss` (`https://<team>.cloudflareaccess.com`), and `exp`. A full user JWT also carries `email`, `iat`, `nbf`, `type` (`app`/`org`), `identity_nonce`, `sub` (user id), and `country`. A **service-token** JWT carries `type`, `aud`, `exp`, `iss`, `iat`, plus `common_name` ("The Client ID of the service token") and `sub` as an empty string; it has no `email` claim at all. Source: [Access application JWT claims](https://developers.cloudflare.com/cloudflare-one/identity/authorization-cookie/application-token/). This is the practical way to tell a plugin call from the owner apart at the origin: `email` present → owner; `email` absent and `common_name` present → the service token.

Minimal FastAPI dependency (PyJWT `PyJWKClient`, RS256, audience, issuer, leeway):

```python
import jwt
from jwt import PyJWKClient
from fastapi import Depends, HTTPException, Request

TEAM_DOMAIN = "https://flowitupteam.cloudflareaccess.com"
JWKS_URL = f"{TEAM_DOMAIN}/cdn-cgi/access/certs"
ATELIER_AUD = "<app-aud-tag-from-dashboard>"

# Module-level singleton. cache_jwk_set defaults to True with a 300s
# lifespan, cache_keys (per-kid LRU) defaults to False — leave both as-is.
jwks_client = PyJWKClient(JWKS_URL)

def verify_access_jwt(request: Request) -> dict:
    token = request.headers.get("Cf-Access-Jwt-Assertion") or request.cookies.get("CF_Authorization")
    if not token:
        raise HTTPException(status_code=403, detail="missing Access JWT")
    try:
        signing_key = jwks_client.get_signing_key_from_jwt(token)
        return jwt.decode(
            token,
            signing_key.key,
            algorithms=["RS256"],
            audience=ATELIER_AUD,
            issuer=TEAM_DOMAIN,
            leeway=30,
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=403, detail=f"invalid Access JWT: {exc}")
```

`PyJWKClient(url, cache_keys=False, max_cached_keys=16, cache_jwk_set=True, lifespan=300)`: the whole JWK-set response is cached 300 s by default and refetched after that or (via `get_signing_key_from_jwt`) when a `kid` isn't in the cached set, so a rotation is picked up automatically well inside Cloudflare's 7-day overlap; no custom caching layer is needed. Sources: [PyJWT usage](https://pyjwt.readthedocs.io/en/stable/usage.html), [PyJWT API reference](https://pyjwt.readthedocs.io/en/stable/api.html).

**Gotcha:** since Access itself already gates on email/service-token at the edge, treat the origin check as defense-in-depth on signature/`aud`/`iss`/`exp`, not a second identity gate — do not additionally hard-require `email` in `verify_access_jwt`, or the plugin's service-token calls (acceptance criterion 10) will 403 even though Access already let them through.

## 2. Service tokens

Create one at Zero Trust → Access controls → Service credentials → Service Tokens → Create Service Token; the Client Secret is shown once only ("If you lose the Client Secret, you must generate a new service token"). Source: [Service tokens](https://developers.cloudflare.com/cloudflare-one/access-controls/service-credentials/service-tokens/). Requests authenticate with `CF-Access-Client-Id: <CLIENT_ID>` and `CF-Access-Client-Secret: <CLIENT_SECRET>`; same source.

Give the Atelier Access application a **Service Auth** policy (a fourth action alongside Allow/Block/Bypass) so the token is accepted without an IdP login prompt, in addition to the owner's email Allow policy. Source: [Common Access policies](https://developers.cloudflare.com/cloudflare-one/access-controls/policies/common-policies/) ("Service Auth policies allow machine-to-machine communication by authenticating requests that present valid service token headers").

Duration is chosen at creation time (docs show an `"8760h"`/1-year example); the exact default/maximum is **[UNVERIFIED]** — not stated on the fetched page. Rotation is done from the dashboard: choose a grace period from "one hour to 30 days" during which the old and new secret both work, then the old one is revoked; the Client ID does not change. Same source as above.

The origin still receives `Cf-Access-Jwt-Assertion` for service-token requests — it is the same Access application JWT described in §1, just with the `common_name`/empty-`sub` shape instead of the identity shape. The `verify_access_jwt` dependency above needs no special-casing for this; it validates either shape identically.

## 3. Locally-managed tunnel

Before touching `/etc/cloudflared/config.yml`: back it up, edit a copy, then run `cloudflared tunnel ingress validate` against it to check the ingress rule set is well-formed, and `cloudflared tunnel ingress rule https://atelier.flowitup.com` to confirm which rule (and only that rule) would match, before it ever reaches the daemon. These are documented `cloudflared` subcommands for exactly this pre-flight check; I could reach the content only through a search-engine cache of Cloudflare's docs, not by fetching the page directly (it 404'd on every path variant tried), so mark the exact current doc URL as **[UNVERIFIED]** even though the command syntax itself is corroborated by multiple independent hits and matches the existing `cloudflared` CLI.

DNS: `cloudflared tunnel route dns <tunnel-name-or-uuid> atelier.flowitup.com` creates a CNAME to `<tunnel-UUID>.cfargotunnel.com`; it needs `cert.pem` present ("To create DNS records using cloudflared, the cert.pem file must be installed on your system") — the existing config already pins `origincert: /etc/cloudflared/cert.pem`, so this should just work from that box. The DNS record and the tunnel's running state are independent: the record persists even if the tunnel is down, in which case visitors get a `1016` error rather than reaching the ingress catch-all. Source: [Create DNS records](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/routing-to-tunnel/dns/).

Reload behaviour: nothing in Cloudflare's config-file docs says `cloudflared` hot-reloads `config.yml`. Their own advice for changing config is: "When making changes to the configuration file for a given tunnel, we suggest relying on `cloudflared` replicas to minimize downtime," i.e. start a second `cloudflared` process with the new config (it opens 4 more edge connections), confirm it is healthy, then stop the old one — rather than editing in place and restarting. Sources: [Configuration file](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/do-more-with-tunnels/local-management/configuration-file/), [Tunnel availability / replicas](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/tunnel-availability/). That is the **lower-risk method** requested: it never drops `folio.flowitup.com`/`cdn.flowitup.com` because their existing connections stay up on the old process the whole time. A plain `systemctl restart cloudflared` also works, since existing tunnels use one shared process serving multiple hostnames from one config file, but its exact drop window is not documented — treat it as **[UNVERIFIED]**, budget for a brief (seconds-scale) gap, and get the owner's go-ahead per the brainstorm's server-change rule before doing it that way.

Access applications attach to a hostname, not to the tunnel's ingress rule directly: the Access application is created against the hostname (e.g. `atelier.flowitup.com`) in Zero Trust → Access → Applications, and Cloudflare's edge evaluates that application's policies before the request is ever forwarded into the tunnel; the ingress rule only decides where `cloudflared` sends traffic once Access has let it through. The dashboard can auto-create the Access application when you add a tunnel hostname there, but that auto-creation path is for dashboard-managed tunnels; for a locally-managed tunnel (config.yml, as here) the Access application is created separately and just needs to name the same hostname. Sources: [Publish a self-hosted application](https://developers.cloudflare.com/cloudflare-one/access-controls/applications/http-apps/self-hosted-public-app/), general architecture corroborated by LearnFlow already running this pattern on `folio-prod-1` per the brainstorm's evidence.

## 4. Timeouts

Cloudflare's current documented default Proxy Read Timeout is **125 s** (not 100 s — 100 s appears to be an older/commonly-misquoted figure), after which a proxied request gets a 524; a Proxy Write Timeout of 30 s applies separately to sending data to the origin. Enterprise plans can raise the 524 threshold up to 6,000 s; whether `flowitup.com`'s zone is on a plan that allows that is **[UNVERIFIED]** — not something this research could check without touching the account. Source: [Error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/).

Nothing in the tunnel docs describes a Tunnel-specific override of that edge-side read timeout; `cloudflared`'s own `originRequest` settings (`connectTimeout: 30s`, `tlsTimeout: 10s`, `tcpKeepAlive: 30s`, `keepAliveTimeout: 1m30s`) govern the connection from `cloudflared` to the local service, not the edge-to-visitor timeout, and none of them exceeds 100 s either. Source: [Origin parameters](https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/configure-tunnels/origin-parameters/). This confirms the brainstorm's chosen design is necessary, not optional: any request that might run past ~100 s (a cold Modal start is 68 s, close to the edge) has to return immediately after `spawn()` and be polled with `get(timeout=0)`, exactly as planned. For HTMX, this means the gallery/job-status views should poll (e.g. `hx-trigger="every 3s"`) rather than hold a request open; a naive long-poll implementation would eventually 524 on a slow job.

## 5. restic → R2

Repository URL: `s3:https://<ACCOUNT_ID>.r2.cloudflarestorage.com/<bucket>`, matching restic's requirement for path-style S3 URLs (`bucket_name.s3....` virtual-hosted style is explicitly unsupported). Region: R2's own S3 API doc says "the region for an R2 bucket is `auto`... an empty value and `us-east-1` will alias to the `auto` region," so `AWS_DEFAULT_REGION=auto` (or simply omitting it) is correct. Sources: [restic S3 backend](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html), [R2 S3 API](https://developers.cloudflare.com/r2/api/s3/api/).

Environment variables: `RESTIC_REPOSITORY`, `RESTIC_PASSWORD` (or `RESTIC_PASSWORD_FILE`), `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY` — the R2 API token's access-key/secret pair, not a Cloudflare API token. Source: [restic S3 backend](https://restic.readthedocs.io/en/stable/030_preparing_a_new_repo.html).

Running from the official image: `docker pull restic/restic`. The image forwards `NICE`, `IONICE_CLASS`, `IONICE_PRIORITY` env vars to `nice`/`ionice`, and restic uses the container's hostname for snapshot metadata, so pass a stable `--hostname` (e.g. `docker run --hostname atelier-backup ...`) rather than letting Docker assign a random one each run. Source: [Installation](https://restic.readthedocs.io/en/stable/020_installation.html).

Core command sequence:

```bash
restic -r "$RESTIC_REPOSITORY" init
restic -r "$RESTIC_REPOSITORY" backup /data/atelier.sqlite.snapshot /data/images
restic -r "$RESTIC_REPOSITORY" forget --keep-daily 7 --keep-weekly 4 --keep-monthly 6 --prune
restic -r "$RESTIC_REPOSITORY" check --read-data-subset=5%   # or an absolute size like 2G, or n/t shards
restic -r "$RESTIC_REPOSITORY" restore latest --target /tmp/restore-test --dry-run --verbose=2
```

`forget --prune` runs `prune` automatically whenever snapshots were actually removed, so one command covers both retention and reclaiming space; without `--prune` you would need to run `forget` then `prune` separately. `check --read-data-subset` accepts a shard fraction (`n/t`), a percentage (`x%`), or a size (`nS`, e.g. `5G`) — good for a weekly partial-integrity check that doesn't re-download the whole (potentially tens-of-GB) repository. The `--dry-run --verbose=2` restore is the documented pattern to verify a restore without doing the full data transfer; that is the lightweight half of "restore-tested every week" — the acceptance criterion's "verifies the image count against it" needs an actual (non-dry-run) restore to a temp directory, at least for the SQLite DB. Sources: [forget](https://restic.readthedocs.io/en/stable/060_forget.html), [check](https://restic.readthedocs.io/en/stable/045_working_with_repos.html), [restore](https://restic.readthedocs.io/en/stable/050_restore.html).

Locks: restic takes a non-exclusive lock for `backup`/`check`-type operations (several can run concurrently) and an exclusive lock for `prune`/repair-type operations (only one at a time, blocking everything else); a lock that fails to be acquired is exit code 11 ("Failed to lock repository," restic ≥0.17.0). If a process dies mid-run, its lock can go stale; `restic unlock` "removes stale locks that have been created by other restic processes," and works even against append-only repositories. For a single nightly cron/systemd-timer job this mostly matters as a failure mode to alert on (job died last night → today's job hits a stale lock) rather than something to design around. Sources: [Scripting / exit codes](https://restic.readthedocs.io/en/stable/075_scripting.html), [restic-unlock man page](https://github.com/restic/restic/blob/master/doc/man/restic-unlock.1), locking design corroborated by restic's own GitHub issue tracker (e.g. [#3219](https://github.com/restic/restic/issues/3219)).

R2 API token scoping: create it from the R2 dashboard, choose **Object Read & Write** (or Object Read only), and that permission tier lets you scope the token to a specific set of buckets rather than the whole account; note this bucket-scoped, S3-compatible-API tier is distinct from the account-wide **Admin Read & Write** tier. Source: [R2 API tokens](https://developers.cloudflare.com/r2/api/tokens/).

**R2 bucket locks vs. `prune` — do not combine them.** R2's bucket-lock feature "prevent[s] the deletion and overwriting of objects in an R2 bucket for a specified period," and "a bucket cannot be emptied while any bucket lock rules are configured." `restic prune` works by deleting pack files that are no longer referenced after `forget` removes snapshots (and sometimes rewrites/repacks pack files, which is also an overwrite). A locked bucket would block exactly that, silently turning `--prune` into a no-op at best or a hard failure at worst. This mirrors a real-world report of the same conflict against Backblaze B2's object-lock feature. Recommendation: leave R2 bucket locks off for the restic bucket, and rely on restic's own encryption plus the account-scoped API token (not object-level immutability) for protection; if immutability is ever required later, it would need a separate, retention-aware backup design, not `restic prune`. Sources: [R2 bucket locks](https://developers.cloudflare.com/r2/buckets/bucket-locks/), corroborating issue: [restic #3491](https://github.com/restic/restic/issues/3491).

## 6. SQLite backup method

| Method | WAL-safe / consistent | Extra dependency | Progress hook for UI | Documented crash risk |
|---|---|---|---|---|
| CLI `sqlite3 db ".backup file"` | Yes (wraps the C Backup API) | Needs the `sqlite3` CLI binary in the image | No, exit code only | Low |
| Python `Connection.backup()` | Yes, same API | None — stdlib | Yes, `progress` callback | Low |
| `VACUUM INTO 'file'` | Yes for a live DB per the docs | None, plain SQL | No | Docs warn: "if... interrupted by an unplanned shutdown or power loss, then the generated output database might be incomplete and corrupt" |

**Recommendation: Python `sqlite3.Connection.backup()`.** All three are safe to run against a database that the app keeps writing (SQLite's Backup API "works even if the database is being accessed by other clients," and completing it "make[s] the destination a bit-wise identical copy of the source database as it was when the copying commenced"), so consistency is not the deciding factor. The deciding factors are architectural fit: Atelier is already a Python/FastAPI service, so `Connection.backup()` needs no extra OS package in the Docker image (the CLI's `.backup` needs the separate `sqlite3` binary installed), and its `progress(status, remaining, total)` callback gives a natural hook for the "last backup status" the UI must show (acceptance criterion 11) — whereas `VACUUM INTO` carries a documented crash-corruption caveat that neither backup-API path has. Sources: [SQLite Backup API](https://sqlite.org/backup.html), [VACUUM INTO](https://sqlite.org/lang_vacuum.html), [Python `sqlite3.Connection.backup`](https://docs.python.org/3/library/sqlite3.html#sqlite3.Connection.backup). (The CLI `.backup` command itself being a thin wrapper over the same Backup API is well established SQLite project knowledge but wasn't stated verbatim on the page fetched; treat that specific implementation detail as corroborated-but-**[UNVERIFIED against a single primary citation]**.)

```python
def snapshot_sqlite(src_path: str, dst_path: str) -> None:
    src = sqlite3.connect(src_path)
    dst = sqlite3.connect(dst_path)
    try:
        src.backup(dst)   # whole-file copy in one step; fine at this DB's size
    finally:
        dst.close()
        src.close()
```

Run this before the restic `backup` step in the nightly job, into a fresh temp file (`VACUUM INTO`'s "file must not previously exist, or... be an empty file" restriction does not apply to `Connection.backup()`, but starting from a clean path each night is still the simplest approach), then point `restic backup` at that snapshot plus the image directory.

## 7. R2 cost

Standard storage: $0.015/GB-month; Class A (write/list-heavy: uploads, lists) $4.50/million requests; Class B (read-heavy: downloads, metadata) $0.36/million requests. Free tier: 10 GB-month storage, 1 million Class A ops, and 10 million Class B ops per month, and no egress fees on any tier; the free tier applies to Standard storage only. Source: [R2 pricing](https://developers.cloudflare.com/r2/pricing/).

At 5 GB, storage is fully inside the free tier ($0). At 50 GB, storage is 40 GB over the free allowance: 40 × $0.015 ≈ **$0.60/month**. A nightly restic run against a mostly-static image directory generates a small, bounded number of new pack-file PUTs (Class A) plus the `check`/`list` calls (Class B) each night; even a generous estimate of low thousands of API calls per run stays far under the 1M/10M monthly free allowances (30 nights × even 1,000 Class A calls = 30,000, 3% of the free tier). The operation-count estimate itself is my own reasoning from restic's chunked, mostly-incremental backup model, not a documented number — restic does not publish an "ops per run" figure — so treat that part as an estimate, not a citation. Net expectation: **$0/month for most of the 5–50 GB range, at most low single-digit dollars/month at the top end**, all storage cost with operations staying inside the free tier.

## Gotchas (consolidated)

- Don't add a hard `email` requirement to the origin's JWT check — it would reject legitimate service-token (plugin) requests, which have no `email` claim.
- Prefer the `cloudflared` replica cutover for the config change over a bare restart; the brainstorm's own rule requires the owner's go-ahead for any tunnel-config change on `folio-prod-1` regardless, so build the runbook around whichever method that go-ahead is given for.
- Never enable R2 bucket locks on the restic bucket; they are incompatible with `prune`.
- Use `--hostname` on the restic Docker container so snapshot metadata doesn't vary randomly between runs.
- `check --read-data-subset` is a partial check by design; the weekly "restore-tested" acceptance criterion needs an actual restore (at least of the SQLite snapshot) to compare the image count, not just a dry-run.
- HTMX views must poll, not long-poll, given the ~125 s (not 100 s) edge read timeout and no documented tunnel-side override.

## Unresolved questions

1. What Cloudflare plan does the `flowitup.com` zone run on? This determines whether the 524 threshold could ever be raised past the default (Enterprise-only per the docs), though the current async job design makes this moot for job execution.
2. What is the exact current doc URL and any example output for `cloudflared tunnel ingress validate` / `cloudflared tunnel ingress rule`? I could not get a non-404 direct fetch of that specific page; the command syntax is corroborated by search-engine snippets of Cloudflare's own content but not by a page I could load directly.
3. What is the actual downtime window (if any) for existing hostnames during a plain `systemctl restart cloudflared`, versus using a replica cutover? Not stated in Cloudflare's docs; would need to be measured empirically (e.g. in a low-traffic window) if the replica method is not used.
4. What is the default/maximum service-token duration Cloudflare's dashboard offers today? Only an example value (`8760h`) was visible in the fetched content.
