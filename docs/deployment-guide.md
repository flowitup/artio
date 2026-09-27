# Atelier deployment guide

Atelier is a single-owner FastAPI service that runs in a confined Docker container on
`folio-prod-1`, a Hetzner CX33 in `eu-central` that already hosts Folio, cdn and LearnFlow.
It is published at `atelier.flowitup.com` through the existing Cloudflare tunnel, behind
Cloudflare Access, and drives GPU image generation on Modal. This guide covers the one-time
server and cloud setup, day-to-day deployment, and the manual operator procedures.

## Server layout

Atelier's files never mix with Folio's, cdn's or LearnFlow's:

| Path | Purpose | Owner / mode |
|---|---|---|
| `/opt/atelier/compose.yaml` | Compose service definition | `root:root`, 0644 |
| `/opt/atelier/deploy.sh` | Forced-command deploy entry point | `root:root`, 0755 |
| `/opt/atelier/.env` | Runtime secrets and configuration | `root:root`, 0600 |
| `/opt/atelier/current-tag` | The image tag the running container was started with | written only by `deploy.sh` |
| `/opt/atelier/previous-tag` | The image tag to fall back to on a manual rollback | written only by `deploy.sh` |
| `/mnt/atelier-data` | The dedicated 50 GB data volume, mounted by UUID with `nofail` | `10001:10001`, 0750 |
| `/mnt/atelier-data/.atelier-volume` | Sentinel file; the app refuses to start in production without it | `10001:10001` |

The container itself runs as uid 10001, with a read-only root filesystem, all capabilities
dropped, `no-new-privileges`, and binds only to `127.0.0.1:8090`. It sits on its own Docker
bridge network, separate from Folio's and LearnFlow's, so a compromise inside Atelier cannot
reach their containers or volumes.

## Where each credential lives

Every credential lives only where it is used. None is in this repository; CI runs a gitleaks scan of
the whole git history on every change. This table records names and places, never values.

| Credential | Stored in | Used by | To replace it |
|---|---|---|---|
| Modal token `atelier-runtime-2` (ID and secret) | `/opt/atelier/.env` on folio-prod-1 (root, 0600) | the Atelier container | New token in Modal; rewrite the two lines (editor, or the hidden-prompt helper); `deploy.sh start`; delete the old token |
| Modal token `atelier-ci-2` (ID and secret) | GitHub `flowitup/atelier` → Environments → `production` → `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | the Deploy Modal workflow | New token in Modal; `gh secret set` both from stdin; delete the old token |
| Access service token `atelier-plugin`, Client ID (not secret) | `.env` as `ATELIER_PLUGIN_CLIENT_ID`; also shown on the token's page in Zero Trust | the app's API authorization | Stays the same when the secret is rotated |
| Access service token `atelier-plugin`, Client Secret | wherever the owner saved it at creation; Cloudflare never shows it again | the Claude plugin (not set up yet) | Zero Trust → Service credentials → `atelier-plugin` → Rotate secret; the Client ID and `.env` don't change |
| Deploy key `atelier-deploy` (private half) | GitHub `production` secret `ATELIER_DEPLOY_SSH_KEY` only; the laptop copy was deleted | the Deploy workflow | New key; replace its line in root's `authorized_keys` (same forced command); set the secret |
| LearnFlow key `learnflow-ci` (private half) | GitHub `flowitup/learnflow` repository secret `SSH_PRIVATE_KEY` only; the laptop copy was deleted | LearnFlow's deploy | New key; replace its `rrsync` line; set the secret |
| Public halves of the server keys | root's `authorized_keys` on folio-prod-1 (see the audit table below) | sshd | — |
| Owner admin key `mac-admin-folio-prod-2026-09` | `~/.ssh/folio-prod-admin` on the owner's Mac (0600; passphrase set 2026-09-27 and kept in the macOS Keychain, with `UseKeychain`/`AddKeysToAgent` on the `folio-prod` alias) | the `folio-prod` SSH alias | New key; add, test, then remove the old line |
| The owner's own Modal CLI token (created 2026-09-25, unnamed) | `~/.modal.toml` on the owner's Mac, profile `yaiba2307` | the owner's `modal` commands | `modal token new` |
| GitHub CLI logins | the macOS Keychain, through `gh` | `gh` commands | `gh auth login` |
| Tunnel credentials and origin certificate | `/etc/cloudflared/` on folio-prod-1 (Folio's existing setup) | cloudflared | — |

`HETZNER_HOST` and `HETZNER_KNOWN_HOSTS` are stored as `production` secrets too, but they are not
credentials: they hold the server's address and its public host key.

## One-time setup

The items below are tracked to completion; some were already carried out by the owner ahead
of the rest of the rollout. Each item that still needs doing is marked **pending** together
with what it requires.

### Root SSH key audit and the LearnFlow key restriction — done

Before any Atelier data lands on the host, every key in root's `authorized_keys` must be
mapped to its holder, and Tailscale SSH's status must be recorded. This is read-only: it
changes nothing. Re-run it whenever a key is added or removed.

```bash
ssh folio-prod 'ssh-keygen -lf /root/.ssh/authorized_keys'                                   # fingerprints, in file order
ssh folio-prod 'ls -la /home/*/.ssh/authorized_keys 2>/dev/null; sshd -T | grep -Ei "^(permitrootlogin|authorizedkeysfile|passwordauthentication|acceptenv|permituserenvironment) "'
ssh folio-prod 'tailscale status --self; tailscale debug prefs | grep -i runssh'
```

`acceptenv` and `permituserenvironment` confirm that sshd cannot be made to forward a client's
own environment variables into a forced command — the basis for trusting that a forced
command's `$SSH_ORIGINAL_COMMAND` path in `deploy.sh` can't be redirected by anything the client
sends. If either ever shows a value that would allow client environment forwarding, treat that
as an unrestricted-CI-key-equivalent finding and stop before continuing.

Record the result as a table, by fingerprint only — **never the key material itself**.

**Audit of 2026-09-26.** Root's `authorized_keys` held two unrestricted ed25519 keys, and
there was no `authorized_keys2`. The holders were matched from sshd's accepted-publickey
journal entries.

- sshd: `PermitRootLogin prohibit-password`, password and keyboard-interactive login off,
  `AcceptEnv` limited to `LANG`, `LC_*`, `COLORTERM` and `NO_COLOR`, and
  `PermitUserEnvironment no`.
- Tailscale SSH is off (`RunSSH false`), and `rrsync` is at `/usr/bin/rrsync` (rsync 3.4.1).

The same day, the owner made two changes: LearnFlow got a key of its own, and the owner's
laptop key was rotated. The laptop key had to go because its private half had also been
LearnFlow's CI secret since July. Root's keys now are:

| Fingerprint | Holder | Options | Status |
|---|---|---|---|
| ~~`SHA256:JZx+jCJH…`~~ | the owner's old laptop key (`~/.ssh/hetzner-deploy`), LearnFlow's CI secret until 2026-09-26 | none | **removed** 2026-09-26 22:11 UTC (backup `authorized_keys.bak-20260926T221116Z`); a login with it is now refused |
| `SHA256:Qik/LV97…` | Folio's CI (`folio-ci-deploy`) | none | accepted residual risk, tracked for a separate Folio hardening task |
| `SHA256:sRwFrc79…` | LearnFlow's CI (`learnflow-ci`) | `restrict,command="/usr/bin/rrsync -wo /var/www/learnflow"` | added 2026-09-26 |
| `SHA256:DJ+/jxTo…` | the owner's Mac (`mac-admin-folio-prod-2026-09`, `~/.ssh/folio-prod-admin`) | none | added 2026-09-26; the laptop's `folio-prod` SSH alias uses it |
| `SHA256:7+LZJSAo…` | Atelier's CI (`atelier-deploy`, the `ATELIER_DEPLOY_SSH_KEY` secret) | `restrict,command="/opt/atelier/deploy.sh"` | added 2026-09-26 (see "Deploy key" below) |

If a later audit turns up any other unrestricted CI-held key, stop and decide with the owner
how to handle it before continuing. Don't fold it into a rollout silently.

**Residual risk (accepted):** any root-capable key can read Atelier's `.env` and images. That
means Folio's CI key and the owner's admin key.

**LearnFlow's restriction.** LearnFlow's new key can only run `rrsync` into
`/var/www/learnflow`, with write access only. rrsync prefixes every client path with that
directory, absolute paths included. So LearnFlow's workflow uploads to the relative path `./`;
an absolute path would land in a nested `var/www/learnflow` directory. The workflow also pins
its rsync action by commit SHA, and a guard step runs **before** the upload. The guard sends a
shell request with the key and uploads only if the server answers with rrsync's refusal. With
an unrestricted key, `./` would point into root's home directory, so the guard stops a wrong
secret from ever uploading there.

LearnFlow's first deploy on its own key (run 36275371916, 2026-09-26) passed:
- the guard ran and the upload went through;
- the site's files were updated at 22:10 UTC;
- nothing landed in a nested directory or in root's home;
- sshd logged only the `learnflow-ci` key from the runner.

These are the only changes made to LearnFlow. Folio, cdn and Atelier are unaffected by them.

The old `hetzner-deploy` key is still authorized on the separate dev server `dev-deploy`
(46.224.60.209). Its private half sat in LearnFlow's GitHub secrets from July to September, so
rotating it there is worth doing too.

### Cloudflare Access application — done

A self-hosted Access application for `atelier.flowitup.com` exists, with two policies:

- an **Allow** policy admitting the owner's own email only;
- a **Service Auth** policy admitting the Claude plugin's service token.

The application cookie's `SameSite` attribute is set to **Lax** (Cloudflare's default is
`None`); the session lasts 24 hours; all identity providers on the account are available to
the owner's policy.

The service token was issued with a **1-year** lifetime and expires on **2027-09-26 at 17:47
Paris time**, as the dashboard shows. Put a calendar reminder two weeks before that date to rotate
it. If the token is
ever suspected of leaking, revoke it immediately in Access → Service credentials — there is no
grace period for a suspected leak.

The Application Audience (AUD) tag and the service token's Client ID are in the `.env` file
described below. The Client ID isn't secret, and the dashboard keeps showing it on the token's
page (Access controls → Service credentials → Service Tokens → `atelier-plugin`); only the Client
Secret is shown just once. Neither value, nor the Client Secret, is recorded in this guide or
anywhere else in the repository. They live only in `.env` (0600, root-only) and the owner's
password manager.

### Modal tokens and spend limit — done

Two dedicated Modal tokens exist, both workspace-wide because the Modal workspace has no
Service Users to scope a token more tightly:
- `atelier-runtime-2` is used by the running container, through `.env`.
- `atelier-ci` is used by the CI pipeline, through the `production` environment's secrets.

Modal shows a token's secret only once, when it is created; its token list keeps showing only the
token IDs. The first runtime token, `atelier-runtime`, was deleted unused on 2026-09-27, because
its secret hadn't been saved, and replaced by `atelier-runtime-2`.

A workspace spend limit is set on Modal's Usage & Billing page at **$20**, the maximum the
account's plan allows; Modal stops billable workloads once the workspace reaches it. The
workspace held no proxy-auth tokens from the legacy endpoint, so there was nothing to revoke
there.

### Hetzner data volume — mounted

A 50 GB volume (Hetzner volume ID **106963035**, `/dev/sdb`) is attached to `folio-prod-1`. It
was formatted as ext4 (label `atelier-data`) on 2026-09-26 and is mounted at
`/mnt/atelier-data`.

- `/etc/fstab` has one added line, keyed by UUID, with `defaults,nofail,noatime 0 2`. A
  detached volume therefore never blocks the host's boot, and it can be reattached and mounted
  later without a reboot. The file was backed up first as `/etc/fstab.bak-20260926T215130Z`.
- The mount root, `images/` and `backup/` belong to uid 10001 with mode 0750, and the sentinel
  file `.atelier-volume` is present.
- The app refuses to start without the sentinel, so an accidentally empty mount point can
  never be mistaken for real data.
- **No backups.** The owner decided on 2026-09-27 that Atelier has no backups. This volume holds
  the only copy of the images and the database, and Hetzner's server backups and snapshots don't
  include volumes. If the volume is lost or deleted, the images are gone, so download anything
  worth keeping from the gallery. The empty `backup/` directory is unused.

Ubuntu 26.04 ships uutils coreutils, whose `install` rejects a numeric owner such as 10001 that
has no account on the host. Create Atelier's directories with `mkdir` and then
`chown 10001:10001`.

### GitHub repository — environment and secrets set

The private repository exists, and its history up to the web UI is pushed. On 2026-09-26 the
owner's login email was purged from that history with a rewrite and a force-push. GitHub may
still serve the old, now unreferenced commits by ID until it garbage-collects them.

The `production` environment exists, and only the `main` branch may deploy to it. There are no
repository-level secrets. Its secrets:

- `HETZNER_HOST` and `HETZNER_KNOWN_HOSTS`: set. The host key was checked against the fingerprint
  read on the server itself.
- `ATELIER_DEPLOY_SSH_KEY`: set by the owner on 2026-09-26.
- `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`: set by the owner on 2026-09-26. They hold the
  CI-scoped `atelier-ci` Modal token, not the runtime one.

Set each with `gh secret set <NAME> --env production`, typed interactively or piped from a
file — never as a command-line argument, which would land in shell history.

### First deploy — done (2026-09-27)

Run 36276417182 deployed commit `458f387` in about 17 seconds on the server, as digest
`sha256:ad872f9c…`. The test, secret-scan and build jobs passed first, and the server pulled the
private package with the job's own token, without any extra package setting. The size check
before the pull ran on the host through `docker manifest inspect`. Checks afterwards:

- `deploy.sh status`: the container is healthy, and `/healthz` reports the deployed version with
  both worker loops `ok`. The image is 346 MB on the host.
- The container runs as uid 10001, with a read-only root filesystem, every capability dropped,
  `no-new-privileges`, only `127.0.0.1:8090`, the data volume at `/data` and a 768 MiB memory limit.
- Without a valid Access JWT the origin answers 403, both with no header and with a forged one.
- From inside the container, the cloud metadata service and the host's own tailnet address are
  blocked, while the Modal API (with the runtime token) and the Access JWKS are reachable.
  Control checks from the host reach both blocked targets, so the blocks come from the egress
  rules.
- Folio's six containers kept their uptimes, and Folio, cdn and LearnFlow answered as before.

### Remaining pending steps

- Apply the tunnel ingress rule and DNS route (below).
- Run the live acceptance checks: a cold-start generation with no timeout, and two jobs that
  survive a `deploy.sh stop` / `deploy.sh start` cycle mid-render.
- Dispatch a Modal-only deploy once, to confirm that path independently of the main pipeline.

## `.env` variables

`/opt/atelier/.env` (mode 0600) holds these variables. Only names and meaning are recorded
here — never values:

| Variable | Meaning |
|---|---|
| `ATELIER_PUBLIC_ORIGIN` | The public origin the app believes it is served from |
| `ATELIER_CF_TEAM_DOMAIN` | The Cloudflare Access team domain used to fetch its JWKS |
| `ATELIER_CF_AUD` | The Access application's Audience tag, checked on every request |
| `ATELIER_OWNER_EMAIL` | The one email the owner-only HTML routes accept |
| `ATELIER_PLUGIN_CLIENT_ID` | The Claude plugin service token's `common_name`, accepted only on the API routes it's allowed to use |
| `ATELIER_DATA_CAP_GB` | Image storage cap; the app refuses new generation jobs at or over this, defaults to 40 |
| `ATELIER_MIN_FREE_GB` | Free-space floor on the volume; the app refuses new jobs below this, defaults to 5 |
| `ATELIER_TIMEZONE` | Timezone used for displayed timestamps |
| `MODAL_TOKEN_ID`, `MODAL_TOKEN_SECRET` | The runtime Modal token (the dedicated one, not the CI one) |
| `MODAL_ENVIRONMENT` | The Modal environment the app talks to (`main`) |

The file was created on 2026-09-26 (root, 0600) with every value that isn't a credential filled
in. The rest was filled in on 2026-09-27:
- `ATELIER_PLUGIN_CLIENT_ID` was read from the Cloudflare dashboard.
- The owner saved the `atelier-runtime-2` token themselves, through a helper script that reads it
  at hidden prompts and rewrites only its two lines over SSH.

To edit the file by hand, use an editor, never `echo`, which would put values in shell history.
The server has no terminal definition for Ghostty, so run
`TERM=xterm-256color ssh -t folio-prod nano /opt/atelier/.env`. While any required value is
empty, the compose check below fails on it, so no deploy can start with a missing credential.
After editing, check
that every `${VAR:?required}` interpolation resolves with `cd /opt/atelier && ATELIER_TAG=check
docker compose config --quiet`.

## Deploy key: setup and storage

The CI pipeline reaches the server through a single, purpose-restricted SSH key:

1. Generate it on a laptop, not on the server: an ed25519 key with no passphrase (the
   private key lives only in the GitHub environment secret and the password manager, both of
   which are themselves access-controlled).
2. Store the private key **deliberately** in the password manager as its recovery copy, and
   as the `ATELIER_DEPLOY_SSH_KEY` GitHub environment secret. Then delete the plaintext copy
   from the laptop with a plain `rm` — this is not secure erasure, and none is claimed.
3. On the server, back up root's `authorized_keys`, then append one line for this key,
   prefixed with `restrict,command="/opt/atelier/deploy.sh"`. `restrict` disables port/agent/
   X11 forwarding and PTY allocation; the forced command means this key can only ever run
   `deploy.sh`, regardless of what command the client asks for.
4. Pin the server's host key: capture it with `ssh-keyscan`, and check the fingerprint against
   one read directly on the server, before it's added to `HETZNER_KNOWN_HOSTS`. CI never
   connects with host-key checking disabled.
5. Prove the boundary before relying on it: connecting with this key and asking for `id` must
   print a rejection and exit with a non-zero status, never run the command.

**State on 2026-09-26.** The key `atelier-deploy` (`SHA256:7+LZJSAo…`) was installed with its
forced command. `authorized_keys` was backed up first as `authorized_keys.bak-20260926T215349Z`.
The server's ed25519 host key is `SHA256:6dN+8Mw+…`; the `ssh-keyscan` result matched the
fingerprint read on the server before it went into `HETZNER_KNOWN_HOSTS`.

The boundary was proven with the key itself. Each refusal below exited with status 2 and
appeared in `journalctl -t atelier-deploy`:
- `id` is refused with `rejected: unexpected command`;
- a plain shell request is refused the same way;
- a well-formed `deploy` with no registry token stops at `rejected: no registry token on
  stdin`, before any Docker call.

## Egress unit

`deploy/systemd/atelier-egress.service` blocks Atelier's container subnet from reaching cloud
metadata (`169.254.169.254`) and the tailnet (`100.64.0.0/10`), so a compromised container
cannot pivot into either. It installs four rules, one `ExecStart=` each (so a rule that fails to
install fails the unit instead of being masked by a later rule that succeeds): two in
`DOCKER-USER` (metadata, and the tailnet by destination CIDR), one more in `DOCKER-USER`
matching the `tailscale0` output interface directly (in case a future routed subnet doesn't fall
inside today's CIDR), and one in `INPUT` blocking the same tailnet range in case a tailnet-facing
service is ever bound to the host itself rather than only reached through forwarding. Each has a
matching `ExecStop=` that removes just that rule.

Copy the unit to the server, then install and enable it, after confirming the host's
`DOCKER-USER` iptables chain exists:

```bash
scp deploy/systemd/atelier-egress.service folio-prod:/tmp/atelier-egress.service
ssh folio-prod 'iptables -L DOCKER-USER -n'   # confirm the chain exists first
ssh folio-prod 'install -m 0644 /tmp/atelier-egress.service /etc/systemd/system/atelier-egress.service \
  && rm /tmp/atelier-egress.service && systemctl daemon-reload \
  && systemctl enable --now atelier-egress.service \
  && iptables -L DOCKER-USER -n && iptables -L INPUT -n'   # expect the new DROP rules
```

Disabling it (`systemctl disable --now atelier-egress.service`) removes all four rules; it never
touches Folio's or LearnFlow's networking.

**State on 2026-09-26.** The unit is installed, enabled and active.
- The host uses `iptables` 1.8.11 (nf_tables), and Docker's firewall backend is iptables.
- The full rule set from before the install was saved to
  `/root/iptables-before-atelier-egress-20260926T215239Z.rules`.
- `iptables -S DOCKER-USER` shows the three rules with their matches (`-o tailscale0`,
  `-d 100.64.0.0/10` and `-d 169.254.169.254/32`), all for source `172.30.90.0/24`.
- The INPUT rule sits above Tailscale's `ts-input` jump.
- Plain `iptables -L` hides interface matches, so the `tailscale0` rule looks like a blanket drop
  there. Use `-S` to read it.
- Folio `/health` (200), LearnFlow (302 to Access) and cdn (its storage backend's own
  `AccessDenied` 403 at the root) answered normally afterwards.

**Residual risk:** the unit only runs `After=docker.service`, and Docker's own startup ordering
is intentionally left unchanged (reordering it to run before Docker risks delaying Folio's and
LearnFlow's containers, which must keep serving through any Atelier-related change). This leaves
a brief window at boot, between Docker starting and this unit installing its rules, during which
a container that started immediately at boot could reach cloud metadata or the tailnet before
the egress rules exist. Atelier itself is deployed by `deploy.sh`, never started automatically at
boot outside of Docker's own `restart: unless-stopped` policy, which narrows this window to a
host reboot specifically; it is accepted as a residual risk rather than solved by reordering.

## Deploying

Every push to `main` runs the tests, builds `ghcr.io/flowitup/atelier`, and deploys the exact
resulting image digest to the server through the restricted key described above. The deploy
step validates the image's revision label against the commit SHA, its declared volumes and
its size before touching anything running. The size is checked twice. The first check, before
the pull, uses the registry's compressed layer sizes, so it only stops a grossly oversized image
from being downloaded. It uses `docker manifest inspect` (the host's Docker has no buildx) and
`jq`, and is skipped, with a log line, if that lookup fails. The second check reads the pulled
image's own size. The step then applies the image inside
a transient systemd unit (so
a dropped connection can't interrupt it), and requires `/healthz` to report the right version
and both worker loops ticking before it commits to the new release — rolling back
automatically otherwise. A rejected image (wrong revision label, declared volumes, or over the
size cap) is removed from the host immediately, so a bad pull never lingers on Folio's shared
disk.

Changes under `modal/` also redeploy the Modal backend, from a separate workflow whose only
step holding a Modal token is the `modal deploy` call itself.

### The only manual path

`deploy.sh` also accepts these subcommands, run as root directly on the server — this is the
**only** supported way to operate Atelier by hand; every manual action goes through it so the
tag files never drift from what is actually running. `stop`, `start`, `rollback` and the
CI-triggered `apply` step all run detached, inside their own transient systemd unit, exactly
like a deploy: a dropped terminal session can't leave one of them half-done.

| Command | Effect |
|---|---|
| `/opt/atelier/deploy.sh status` | Shows the current and previous tags, maintenance state, container state, and `/healthz` |
| `/opt/atelier/deploy.sh stop` | Stops the container and enters maintenance mode |
| `/opt/atelier/deploy.sh start` | Starts the current tag, and leaves maintenance mode once it is healthy |
| `/opt/atelier/deploy.sh rollback` | Swaps to the previous tag, after confirming it starts healthy; restores the current tag if that fails too |

Never run `docker compose` directly against Atelier's project, and never run `docker image
prune` or `docker system prune` on this host — Atelier's own pruning (kept to its own image,
down to 3 tags, plus a sweep of its own dangling images) is the only cleanup that ever runs,
precisely so a global prune can't delete Folio's images.

### Maintenance mode

`stop` marks Atelier as being in maintenance: a CI-triggered deploy that lands while the marker
is present refuses immediately (exit code 75, logged) rather than starting the container back up
underneath whatever manual work is in progress. `start` is what clears the marker, and only does
so once the container has come back up healthy.

Before any procedure that needs Atelier to stay down for a while (for example, manual work on
the data volume), also disable the deploy workflow itself, so a push to `main`
during the window can't even queue a deploy attempt:

```bash
gh workflow disable deploy.yml --repo flowitup/atelier
# ... run deploy.sh stop, do the maintenance work, run deploy.sh start ...
gh workflow enable deploy.yml --repo flowitup/atelier
```

## Tunnel runbook

Atelier's ingress rule (`deploy/cloudflared-ingress-rule.yml`) routes
`atelier.flowitup.com` to `http://localhost:8090`, ahead of the catch-all rule. Because
`cloudflared`'s drop window during a plain restart is undocumented, and Folio, cdn and
LearnFlow must keep serving throughout, the rule is applied through a temporary second
("replica") process rather than a direct restart:

1. **Prepare and validate.**

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ); CFG=/etc/cloudflared/config.yml
   cp -a "$CFG" "$CFG.bak-$TS"
   # insert the two-line rule from deploy/cloudflared-ingress-rule.yml immediately before the
   # catch-all `- service: http_status:404` line, writing the result to "$CFG.new"
   diff -u "$CFG" "$CFG.new"                          # expect exactly the two added lines
   cloudflared tunnel --config "$CFG.new" ingress validate
   ```

2. **Record baselines**, from the laptop:

   ```bash
   curl -s -o /dev/null -w 'folio %{http_code}\n' https://folio.flowitup.com/health
   curl -s -o /dev/null -w 'cdn %{http_code}\n'   https://cdn.flowitup.com/
   curl -s -o /dev/null -w 'learn %{http_code} %{redirect_url}\n' https://learn.flowitup.com/
   ```

   Only after the Access application exists (above), add the DNS route:
   `cloudflared tunnel route dns <tunnel-name> atelier.flowitup.com`.
3. **Start a replica.** Run a second `cloudflared` process from the validated config, on its own
   metrics port (check it's free with `ss -ltnp` first):

   ```bash
   systemd-run --unit=cloudflared-atelier-cutover --collect \
     cloudflared tunnel --config "$CFG.new" --metrics 127.0.0.1:<free-port> run <tunnel-name>
   journalctl -u cloudflared-atelier-cutover --no-pager | grep -c "Registered tunnel connection"   # expect >= 1
   ```

   If it doesn't register within about 60 seconds, `systemctl stop cloudflared-atelier-cutover`
   and change nothing else.
4. **Cut over.**

   ```bash
   install -m "$(stat -c %a "$CFG")" -o "$(stat -c %u "$CFG")" -g "$(stat -c %g "$CFG")" "$CFG.new" "$CFG"
   T_RESTART=$(date '+%Y-%m-%d %H:%M:%S'); systemctl restart cloudflared; sleep 10
   systemctl is-active cloudflared                                                            # expect active
   journalctl -u cloudflared --since "$T_RESTART" --no-pager | grep -c "Registered tunnel connection"  # expect >= 1
   ```

   **On any failure here:** keep the replica running (it is still serving Folio, cdn and
   LearnFlow), `cp -a "$CFG.bak-$TS" "$CFG"`, `systemctl restart cloudflared`, re-run the checks
   above against the restored config, and only once they pass, stop the replica and stop here.
5. **Verify externally**, repeating the step 2 commands plus
   `curl -s -o /dev/null -w 'atelier %{http_code} %{redirect_url}\n' https://atelier.flowitup.com/`.
   Folio, cdn and LearnFlow must match their recorded baselines, and Atelier must return a 302
   to the Access login.
6. **Stop the replica**, now that the live service carries the new rule correctly:
   `systemctl stop cloudflared-atelier-cutover`.
7. **Verify externally once more**, with only the live service running, expecting the same
   results as step 5.

Removing the rule entirely later follows the same replica method, in reverse (drop the two
lines instead of adding them), and is also the tunnel half of full removal, below.

## Rollback and full removal

- **A bad deploy:** handled automatically; manually, `/opt/atelier/deploy.sh rollback`.
- **The tunnel change:** the replica method above, restoring the backed-up config (step 4's
  failure path), or removing the rule the same way in reverse.
- **The LearnFlow key restriction:** prefer fixing forward in LearnFlow's workflow. A full
  undo has to change both sides together, because each half breaks the other alone.
  1. Revert LearnFlow's workflow commit, which set the relative path and added the pre-upload
     guard.
  2. Remove the `restrict,command=…` prefix from the single `learnflow-ci` line in root's
     `authorized_keys`, after backing the file up. Never restore an older whole-file backup,
     which would drop keys added since.
- **The egress unit:** `systemctl disable --now atelier-egress.service`, which removes all four
  rules via their `ExecStop=` lines.
- **Full removal of Atelier, leaving Folio, cdn and LearnFlow untouched:**

  ```bash
  /opt/atelier/deploy.sh stop
  # delete ONLY the Atelier line from root's authorized_keys (match its atelier-deploy comment;
  # review the diff before saving -- never restore an older whole-file backup, which would drop
  # keys added since)
  # remove the tunnel ingress rule via the replica method above
  # delete the atelier.flowitup.com DNS record and the Access application in Cloudflare
  systemctl disable --now atelier-egress.service
  umount /mnt/atelier-data   # the data itself stays on the volume; detach it in Hetzner if it's no longer needed
  ```

## Routine operations

- **Health and status:** `deploy.sh status` on the server; the CI workflow run list for the
  latest deploy result. Verification never goes through the public hostname directly, because
  Access answers it with a redirect rather than the app's own response.
- **Logs:** the deploy script logs to the system journal under the `atelier-deploy` tag
  (`journalctl -t atelier-deploy`); the container's own logs are size- and count-capped
  (`json-file`, 10 MB × 3 files) so they can't fill the disk.
- **Image cleanup:** handled automatically by `deploy.sh` on every successful (and failed)
  deploy, keeping the current tag, the previous tag and the newest other local tag, plus
  sweeping this repository's own dangling images; nothing else is ever pruned automatically.
- **Disk usage:** the app's own header shows usage against the volume's cap, and refuses new
  generation jobs at or over the cap, or when free space drops under the floor; `df -h
  /mnt/atelier-data` gives the underlying number directly.
- **GPU status, warm-up and stop:** the header badge and `/gpu` show each backend's state (warm,
  warming, running, scaled to zero, stopped or unhealthy), computed on read from short caches, so
  nothing polls Modal while no tab is open. A failed status read shows the error text and is itself
  cached briefly, so a Modal or CLI outage never turns into one CLI process per page read.
  "Warm 5/15/30 min" keeps the backend warm with a ping roughly every 30s (at most one in flight);
  a click only ever extends an open window (it never shortens one already running longer), and the
  backend always scales back to zero within about 2 minutes once pings stop, including after
  `deploy.sh stop`. That time is the last ping (up to 30s old), Modal's 60s idle window, and a
  short delay before Modal removes the container; on 2026-09-27 it measured 97–114s after a window
  ended and 97–108s after `deploy.sh stop`. Stop asks for confirmation when jobs are active (queued or running), then
  cancels every tracked call at once (the warm-up ping first, retrying any cancel that failed once
  before giving up on it), stops the backend's containers, and waits up to about a minute for its
  runner and backlog counts to reach zero -- pressing Stop again re-runs the same sequence if it
  doesn't converge, or if a Modal step itself failed, in time. Its outcome (stopped, still running,
  or failed) stays visible on the panel for a couple of minutes, not just in the one response that
  triggered it.
- **Backend unhealthy:** shown when a backend's ComfyUI has stopped answering while its container
  was otherwise running -- either a failed warm-up ping's health probe, or three of the backend's
  jobs failing on their very first poll with a connection-refused-style error (both feed the same
  circuit breaker; a successful ping or job render clears it again). A container that never
  finishes booting is a different failure (recycling it wouldn't help) and does not feed this
  breaker. By the time the notice appears, Atelier has already reacted: it cleared any open warm
  window, cancelled every queued and running job on that backend, and run the Stop sequence to
  terminate its containers, so the next job or warm-up starts a fresh one. To recover:
  - Retry the cancelled jobs from the queue view (each has a Retry button); they re-queue normally
    and dispatch to the recycled backend.
  - If a backend keeps recycling itself, check `modal app logs qwen21-uc` for what ComfyUI was doing
    when it stopped answering (an out-of-memory crash, a bad model file, a Modal-side failure).
    Redeploy once the underlying issue is understood (`gh workflow run deploy-modal.yml --ref main`),
    which also resets the autoscaler.
  - An occasional false trip (a one-off timeout, or a real but transient ComfyUI error) is expected
    to be rare; if it recurs without a real underlying cause, raise the breaker's 3-failure
    threshold in `worker.py` after telling the owner.
- **Presets, library and custom workflows:** `/library` saves, loads and deletes named presets of
  the generate form's own fields (prompt, negative, size, steps, cfg); loading one (`/generate?preset=`)
  only prefills the form, so it is still validated against the current model schema on submit like
  any other value typed there. Every image can be starred and tagged from its own page; `/gallery`
  filters by any combination of `q` (prompt or tag text, prefix-matched), `tag`, `starred` and
  `model` (including a "Custom workflows" option for images with no model, i.e. from a stored
  workflow's own run). `/workflows` uploads a ComfyUI **API-format** graph (Workflow -> Export (API)
  in ComfyUI, not a plain workflow save) for a chosen backend: at most 2 MB parsed, inside a 3 MB
  request body checked before any parsing starts, and it must contain a `SaveImage` or
  `PreviewImage` node or the upload is refused with a message. Running it takes a seed mode (random,
  fixed with increments, or keep the graph's own seed -- "keep" only with a count of 1) and a count
  of 1-8; a graph with no `KSampler`/`KSamplerAdvanced`/`RandomNoise` literal seed input can only be
  run once, since every further run would render the same image. Deleting a stored workflow does not
  touch any job that already ran it: each job keeps its own exact graph (`/jobs/{id}/graph.json`),
  only the link back to the deleted workflow clears. An invalid graph is rejected by ComfyUI itself
  at run time; the failed job's queue row shows its validation text verbatim, starting with
  `ComfyUI rejected workflow:`, and Retry re-queues the same graph unchanged. All of this is
  owner-only HTML, same as the rest of the app: the plugin's service identity cannot save a preset,
  star or tag an image, or upload, run or delete a workflow.
- **The domain:** `flowitup.com` is registered at Cloudflare with auto-renew **off**, by the
  owner's choice on 2026-09-27. It expires on **2027-04-07** and must be renewed by hand before
  then (about $10.45 a year). Every site behind the tunnel (Folio, cdn, LearnFlow and Atelier)
  depends on it.
- **Token rotation reminders:** the Cloudflare Access service token, due 2027-09-26 at 17:47 Paris time (see
  above). Rotating it means, in order: creating the new token in Access; adding it to the
  application's Service Auth policy (a new token is not automatically attached to any policy);
  updating `ATELIER_PLUGIN_CLIENT_ID` in `.env` with the new token's Client ID (the Client
  Secret is never stored server-side -- it belongs only in the plugin's own configuration, on
  whatever machine runs it); updating the plugin's configuration with the new Client ID and
  Secret; running `/opt/atelier/deploy.sh start` to restart the container with the new `.env`;
  and only then revoking the old token.
- **If the secret ever leaks** (committed by mistake, pasted somewhere it shouldn't have been, a
  compromised machine that had it configured): revoke it **immediately** in Zero Trust -> Access ->
  Service credentials -> `atelier-plugin` -> Rotate secret. There is no grace period for a suspected
  leak -- rotate first, ask questions after. Then create a new token bound to the same Service Auth
  policy, update `.env` and the plugin's own configuration as above, and confirm with a plugin call
  that the old secret no longer works.

## Installing the Claude plugin

The plugin (`plugin/`) lets Claude drive Atelier through the `atelier-plugin` service token: list
models, generate, check job and GPU status, and search, fetch and run stored workflows. It has no
tool to warm up or stop a backend, upload, or delete anything -- the service identity's allowlist
(`atelier/auth.py`'s `SERVICE_ROUTES`) simply doesn't include those routes. See `plugin/README.md`
for the full tool list and privacy notes; this section covers install by both routes.

**Primary route -- Claude desktop app:**

1. Build the zip once (from a checkout of this repository): `bash plugin/build.sh` produces
   `plugin/atelier.plugin`. It is not committed (`.gitignore` excludes `*.plugin`); rebuild it after
   any change under `plugin/`.
2. Open `atelier.plugin` with the Claude desktop app, or install it from Settings -> Capabilities.
3. When the configuration dialog appears, enter `cf_client_id` (not secret, also visible on the
   token's page in Zero Trust) and `cf_client_secret` (from the password manager -- Cloudflare shows
   it only once, at creation). Leave `base_url` and `save_dir` at their defaults unless told otherwise.
4. **If the desktop app's install route shows no configuration prompt** (no dialog, and
   `${user_config.*}` stays unexpanded in the running plugin's environment): fall back to environment
   passthrough, the same pattern Folio's plugin uses. Edit `plugin/.mcp.json` so each `env` value
   reads `"${ATELIER_CF_CLIENT_ID}"` / `"${ATELIER_CF_CLIENT_SECRET}"` etc. instead of
   `"${user_config.*}"`, and export those variables in the shell that starts Claude. A literal secret
   value must never land in any file either way.

**Secondary route -- Claude Code, from a checkout of this repository:**

```bash
claude plugin marketplace add /path/to/this/repo    # registers the one-entry local marketplace
claude plugin install atelier@atelier-local         # or /plugin install atelier@atelier-local in a session
/plugin configure atelier                            # enter cf_client_id and cf_client_secret
claude plugin validate /path/to/this/repo/plugin     # sanity check after any manifest change
```

A shell `claude plugin install` shows no configuration dialog; always follow it with
`/plugin configure atelier` in a session to enter the secrets.

Either route runs the server from its committed lockfile (`uv run --locked --script`, verified
against uv 0.9.26): a transitive dependency can never silently change under a process that holds the
service-token secret. If an older `uv` on the machine rejects `--locked`, upgrade it, or fall back to
`--frozen` in `plugin/.mcp.json`, which still installs only the locked versions without the staleness
check.

## Final review

The final acceptance run (criteria 1-14) is recorded in
`plans/260925-1331-atelier-image-studio/phase-08-api-plugin-and-acceptance.md`'s Acceptance record,
filled in during the owner-gated live pass. This deployment guide is the durable record of the setup
and the routine operator procedures; the acceptance record is a one-time, dated proof that they all
worked together in production, not a second copy of this guide.
