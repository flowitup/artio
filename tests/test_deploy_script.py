"""Hermetic tests for deploy/deploy.sh.

Every test drives the real script through stub `docker`, `curl`, `systemd-run`, `flock` and `logger`
executables that this file writes into a temporary `bin/` and puts first on PATH, so nothing here
touches a real Docker daemon, network or systemd unit. `ATELIER_DEPLOY_DIR` points at a fresh temporary
directory -- deploy.sh only reads that variable on the argv (non-forced-command) path; the forced
command on the real server always uses its compiled-in /opt/atelier default, since `restrict` and
sshd's default `PermitUserEnvironment no` drop the client's environment before the script ever runs,
and deploy.sh itself ignores the variable whenever SSH_ORIGINAL_COMMAND is set, as defense in depth.

The stub `docker` and `curl` are answered from small state files under `$STUB_STATE`, which the
`Harness` helper below writes before invoking the script and reads back afterwards to assert on.
"""

import fcntl
import json
import os
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

DEPLOY_SH = Path(__file__).resolve().parent.parent / "deploy" / "deploy.sh"
IMAGE = "ghcr.io/flowitup/atelier"

NEW_SHA = "a" * 40
PREV_SHA = "b" * 40
OTHER_SHA = "c" * 40
DIGEST = "sha256:" + "d" * 64
TOKEN = "gh-registry-token"  # a fixed test fixture value, never a real credential
USER = "github-actions-bot"
MAX_IMAGE_BYTES = 1_500_000_000  # must track deploy.sh's own MAX_IMAGE_BYTES

STUB_DOCKER = '''#!/usr/bin/env python3
"""Hermetic stand-in for `docker`: records every invocation and answers canned responses driven by
small state files under $STUB_STATE. Never touches a real daemon, image or network."""
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])


def read_json(name, default):
    p = state / name
    return json.loads(p.read_text()) if p.exists() else default


def read_text(name):
    p = state / name
    return p.read_text() if p.exists() else ""


def write_text(name, value):
    (state / name).write_text(value)


def append_line(name, value):
    with (state / name).open("a") as fh:
        fh.write(value + "\\n")


def main(argv):
    with (state / "docker.calls").open("a") as fh:
        fh.write(" ".join(argv) + "\\n")

    args = list(argv)
    if args[:1] == ["--config"]:
        cfg_dir, args = args[1], args[2:]
    else:
        cfg_dir = None

    if not args:
        return 1
    cmd, rest = args[0], args[1:]

    if cmd == "login":
        sys.stdin.read()
        write_text("logged-in", "1")
        return 0
    if cmd == "logout":
        (state / "logged-in").unlink(missing_ok=True)
        return 0
    if cmd == "pull":
        return read_json("pull-exit-codes", {}).get(rest[-1], 0)
    if cmd == "tag":
        _src, dst = rest
        append_line("tags-created", dst)
        return 0

    if cmd == "manifest":
        if rest[:1] == ["inspect"] and len(rest) == 2:
            raw = read_json("registry-manifests.json", {}).get(rest[1])
            if raw is None:
                return 1  # simulates a failed lookup: deploy.sh must fall back to the post-pull check
            print(raw)
            return 0
        return 1

    if cmd == "image":
        sub, sub_rest = rest[0], rest[1:]
        if sub == "inspect":
            fmt, positional = None, []
            i = 0
            while i < len(sub_rest):
                tok = sub_rest[i]
                if tok in ("--format", "-f"):
                    fmt = sub_rest[i + 1]
                    i += 2
                    continue
                positional.append(tok)
                i += 1
            info = read_json("images.json", {}).get(positional[0])
            if info is None:
                print(f"Error: No such image: {positional[0]}", file=sys.stderr)
                return 1
            if fmt is None:
                return 0  # existence check only, as used by the rollback verb
            if "revision" in fmt:
                print(info.get("revision", ""))
            elif "Volumes" in fmt:
                print(info.get("volumes", "null"))
            elif "Size" in fmt:
                print(info.get("size", 0))
            elif "RepoDigests" in fmt:
                for ref in info.get("repo_digests", []):
                    print(ref)
            return 0
        if sub == "ls":
            # A call scoped to the Atelier repository (the "$IMAGE" positional argument present) sees
            # only Atelier's own tags; an unscoped call sees a realistic, other-repository-polluted
            # listing instead, so a missing repository filter is observable rather than harmless here.
            dangling = "--filter" in sub_rest and "dangling=true" in sub_rest
            scoped = "ghcr.io/flowitup/atelier" in sub_rest
            if dangling:
                name = "dangling-ids"
            elif scoped:
                name = "ls-tags"
            else:
                name = "ls-tags-unfiltered"
            print(read_text(name), end="")
            return 0
        if sub == "rm":
            # Like the real daemon: an unknown reference is an error; a reference listed in
            # rm-refused (an image still in use) stays and is an error; with the containerd image
            # store (the server's), removing a tag also drops that image's digest references.
            append_line("rm-calls", " ".join(sub_rest))
            images = read_json("images.json", {})
            refused = read_text("rm-refused").split()
            containerd = (state / "containerd-store").exists()
            status = 0
            for ref in sub_rest:
                if ref in refused:
                    print(f"Error response from daemon: conflict: unable to remove {ref}: in use", file=sys.stderr)
                    status = 1
                elif ref in images:
                    info = images.pop(ref)
                    if containerd:
                        for digest_ref in info.get("repo_digests", []):
                            images.pop(digest_ref, None)
                else:
                    print(f"Error response from daemon: No such image: {ref}", file=sys.stderr)
                    status = 1
            (state / "images.json").write_text(json.dumps(images))
            return status

    if cmd == "compose":
        # Skip global compose flags (-f <file>, -p <name>) to find the actual verb.
        i = 0
        while i < len(rest) and rest[i] in ("-f", "-p"):
            i += 2
        sub = rest[i] if i < len(rest) else ""
        tag = os.environ.get("ATELIER_TAG", "")
        if sub == "up":
            write_text("running-tag", tag)
            append_line("up-calls", tag)
            return read_json("up-exit-codes", {}).get(tag, 0)
        if sub == "down":
            write_text("running-tag", "")
            return 0
        if sub == "ps":
            return 0

    print(f"stub docker: unhandled invocation: {argv}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''

STUB_CURL = '''#!/usr/bin/env python3
"""Hermetic stand-in for `curl`: answers GET /healthz from $STUB_STATE instead of a real server. The
reported version tracks whichever tag the stub `docker compose up` last recorded as running, unless
that tag has been marked unhealthy, in which case a deliberately wrong version is reported -- exactly
the shape of a running container whose /healthz disagrees with the tag it was deployed with."""
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])


def main():
    running = state / "running-tag"
    running_tag = running.read_text().strip() if running.exists() else ""
    if not running_tag:
        return 1  # nothing running: curl's own connection-refused exit code
    unhealthy_path = state / "unhealthy-tags"
    unhealthy = set(unhealthy_path.read_text().split()) if unhealthy_path.exists() else set()
    version = "0" * 40 if running_tag in unhealthy else running_tag
    # Compact separators, matching Starlette's actual JSONResponse output: deploy.sh's healthy()
    # matches the literal substring "version":"X" with no space after the colon.
    print(json.dumps({"version": version, "loops": "ok"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''

# A second curl stub, swapped in only by the stale-loops test: reports the running tag's own version
# (so it always matches) but flags the loops as "stale" for tags recorded in $STUB_STATE/stale-tags.
STUB_STALE_CURL = '''#!/usr/bin/env python3
import json
import os
import sys
from pathlib import Path

state = Path(os.environ["STUB_STATE"])
running = state / "running-tag"
running_tag = running.read_text().strip() if running.exists() else ""
if not running_tag:
    sys.exit(1)
stale_path = state / "stale-tags"
stale = set(stale_path.read_text().split()) if stale_path.exists() else set()
loops = "stale" if running_tag in stale else "ok"
print(json.dumps({"version": running_tag, "loops": loops}, separators=(",", ":")))
'''

STUB_SYSTEMD_RUN = '''#!/usr/bin/env bash
# Hermetic stand-in for `systemd-run -p Type=oneshot --wait --collect --pipe`. deploy.sh always
# `exec`s this command, so running the wrapped command synchronously in-place and propagating its
# exit status reproduces the same observable behaviour (from the caller's side) as the real
# --wait --pipe combination. Every invocation is recorded verbatim for the systemd-run-arguments test.
#
# Real systemd-run gives the transient unit a CLEAN environment: SSH_ORIGINAL_COMMAND never reaches
# it, which is exactly why apply/rollback/stop/start can rely on plain positional args instead of the
# forced-command grammar. Only PATH, STUB_STATE and whatever the caller explicitly asked to carry over
# via --setenv (matching real systemd-run's own env-passing mechanism) survive into the nested call.
printf '%s\\n' "$*" >> "$STUB_STATE/systemd-run.calls"
args=(); setenvs=(); skip_next=0
for a in "$@"; do
  if [[ "$skip_next" == 1 ]]; then skip_next=0; continue; fi
  case "$a" in
    --unit=*|--wait|--collect|--pipe|--quiet) ;;
    -p) skip_next=1 ;;
    --setenv=*) setenvs+=("${a#--setenv=}") ;;
    *) args+=("$a") ;;
  esac
done
env_args=("PATH=$PATH" "STUB_STATE=$STUB_STATE")
for kv in "${setenvs[@]}"; do env_args+=("$kv"); done
exec env -i "${env_args[@]}" "${args[@]}"
'''

STUB_NOOP_OK = "#!/usr/bin/env bash\nexit 0\n"

# Mimics real logger's own stdin behaviour: it reads (and here, discards) stdin only when invoked with
# no trailing message argument, exactly like the real util-linux logger. deploy.sh's log() always calls
# `logger -t atelier-deploy -- "$*"` (a message argument, marked by "--"), which real logger never reads
# stdin for; only up()'s and stop()'s `... | logger -t atelier-deploy` (no message argument) pipes real
# output through it. log() runs inside prune()'s `while read` loop body, sharing that loop's stdin (the
# awk pipe) -- a stub that unconditionally drained stdin here would silently steal the loop's next line.
STUB_LOGGER = '''#!/usr/bin/env bash
for a in "$@"; do
  if [[ "$a" == "--" ]]; then
    exit 0
  fi
done
cat >/dev/null
'''

# Swapped in only by the real-lock test: an actual advisory lock via fcntl.flock on the given fd,
# exactly like the real util-linux `flock` utility's fd-only form.
STUB_REAL_FLOCK = """#!/usr/bin/env python3
import fcntl
import sys

fd = int(sys.argv[-1])
flags = fcntl.LOCK_EX | (fcntl.LOCK_NB if "-n" in sys.argv[1:-1] else 0)
try:
    fcntl.flock(fd, flags)
except BlockingIOError:
    sys.exit(1)
"""


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


@dataclass
class Harness:
    """Drives deploy.sh with stub tools on PATH and a temporary deploy directory."""

    bin_dir: Path
    state_dir: Path
    deploy_dir: Path

    def env(self, *, ssh_command: str | None, deploy_dir_env: Path | None = None) -> dict[str, str]:
        merged = dict(os.environ)
        merged.pop("SSH_ORIGINAL_COMMAND", None)
        merged["PATH"] = f"{self.bin_dir}{os.pathsep}{merged.get('PATH', '')}"
        merged["STUB_STATE"] = str(self.state_dir)
        merged["ATELIER_DEPLOY_DIR"] = str(deploy_dir_env or self.deploy_dir)
        if ssh_command is not None:
            merged["SSH_ORIGINAL_COMMAND"] = ssh_command
        return merged

    def run(
        self,
        *args: str,
        ssh_command: str | None = None,
        stdin: str = "",
        close_stdio: bool = False,
        timeout: float = 10,
        deploy_dir_env: Path | None = None,
    ) -> subprocess.CompletedProcess:
        env = self.env(ssh_command=ssh_command, deploy_dir_env=deploy_dir_env)
        # Invoked through the symlink inside deploy_dir, exactly as production invokes deploy.sh from
        # inside /opt/atelier: deploy.sh derives its own $DIR from $0's directory on the forced-command
        # path, so the script under test must see itself at a path whose dirname is this temp directory.
        script = self.deploy_dir / "deploy.sh"
        if close_stdio:
            # The wrapper closes its own stdout/stderr before exec-ing into deploy.sh, so the script
            # under test genuinely has no fd 1/2 -- not merely fds redirected to /dev/null.
            cmd = ["bash", "-c", 'exec 1>&- 2>&-; exec "$@"', "deploy-harness", "bash", str(script), *args]
        else:
            cmd = ["bash", str(script), *args]
        return subprocess.run(
            cmd, env=env, input=stdin, text=True, capture_output=True, timeout=timeout, check=False
        )

    def write_tag(self, name: str, value: str) -> None:
        (self.deploy_dir / name).write_text(value + "\n")

    def read_tag(self, name: str) -> str | None:
        p = self.deploy_dir / name
        return p.read_text().strip() if p.exists() else None

    def _images(self) -> dict:
        p = self.state_dir / "images.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def _save_images(self, images: dict) -> None:
        (self.state_dir / "images.json").write_text(json.dumps(images))

    def set_image(self, ref: str, *, revision: str, volumes: str = "null", size: int = 100_000_000) -> None:
        images = self._images()
        images[ref] = {"revision": revision, "volumes": volumes, "size": size}
        self._save_images(images)

    def mark_tag_exists(self, ref: str) -> None:
        images = self._images()
        images[ref] = {}
        self._save_images(images)

    def set_repo_digests(self, ref: str, digests: list[str]) -> None:
        images = self._images()
        images[ref] = {"repo_digests": digests}
        self._save_images(images)

    def mark_unhealthy(self, tag: str) -> None:
        p = self.state_dir / "unhealthy-tags"
        existing = p.read_text() if p.exists() else ""
        p.write_text(existing + tag + "\n")

    def image_exists(self, ref: str) -> bool:
        return ref in self._images()

    def set_ls_tags(self, tags: list[str]) -> None:
        (self.state_dir / "ls-tags").write_text("\n".join(tags) + "\n")

    def set_ls_tags_unfiltered(self, tags: list[str]) -> None:
        (self.state_dir / "ls-tags-unfiltered").write_text("\n".join(tags) + "\n")

    def set_dangling_ids(self, ids: list[str]) -> None:
        (self.state_dir / "dangling-ids").write_text("\n".join(ids) + "\n")

    def set_registry_manifest(self, ref: str, raw_json: str) -> None:
        p = self.state_dir / "registry-manifests.json"
        manifests = json.loads(p.read_text()) if p.exists() else {}
        manifests[ref] = raw_json
        p.write_text(json.dumps(manifests))

    def docker_calls(self) -> str:
        p = self.state_dir / "docker.calls"
        return p.read_text() if p.exists() else ""

    def up_calls(self) -> list[str]:
        p = self.state_dir / "up-calls"
        return p.read_text().split() if p.exists() else []

    def tags_created(self) -> list[str]:
        p = self.state_dir / "tags-created"
        return p.read_text().split() if p.exists() else []

    def rm_calls(self) -> list[str]:
        p = self.state_dir / "rm-calls"
        return p.read_text().splitlines() if p.exists() else []

    def systemd_run_calls(self) -> list[str]:
        p = self.state_dir / "systemd-run.calls"
        return p.read_text().splitlines() if p.exists() else []

    def is_logged_in(self) -> bool:
        return (self.state_dir / "logged-in").exists()

    def config_dir_from_first_call(self) -> Path:
        first = self.docker_calls().splitlines()[0]
        assert first.startswith("--config "), first
        return Path(first.split()[1])


@pytest.fixture
def harness(tmp_path) -> Harness:
    bin_dir = tmp_path / "bin"
    state_dir = tmp_path / "state"
    deploy_dir = tmp_path / "opt-atelier"
    for d in (bin_dir, state_dir, deploy_dir):
        d.mkdir()
    # In production deploy.sh is installed inside /opt/atelier itself (server layout, docs/deployment
    # guide.md), and its `deploy)` branch re-invokes "$DIR/deploy.sh apply <sha>" through systemd-run.
    # Mirror that layout here so the self-re-exec resolves the same way it does on the server.
    (deploy_dir / "deploy.sh").symlink_to(DEPLOY_SH)
    _write_executable(bin_dir / "docker", STUB_DOCKER)
    _write_executable(bin_dir / "curl", STUB_CURL)
    _write_executable(bin_dir / "systemd-run", STUB_SYSTEMD_RUN)
    _write_executable(bin_dir / "flock", STUB_NOOP_OK)
    _write_executable(bin_dir / "logger", STUB_LOGGER)
    return Harness(bin_dir=bin_dir, state_dir=state_dir, deploy_dir=deploy_dir)


# Every case sends a *valid* token and registry user on stdin, so a case that slipped past the
# SSH_ORIGINAL_COMMAND grammar would go on to call docker -- rejection here can only be the grammar
# itself, not an incidental empty-stdin failure further down.
MALFORMED_FORCED_COMMANDS = [
    pytest.param("", id="empty"),
    pytest.param("deploy", id="deploy-with-no-sha-or-digest"),
    pytest.param(f"deploy {'a' * 39} {DIGEST}", id="39-hex-sha"),
    pytest.param(f"deploy {'a' * 41} {DIGEST}", id="41-hex-sha"),
    pytest.param(f"deploy {'A' * 40} {DIGEST}", id="uppercase-sha"),
    pytest.param(f"deploy {NEW_SHA} {'d' * 64}", id="digest-without-sha256-prefix"),
    pytest.param(f"deploy {NEW_SHA} {DIGEST}x", id="digest-with-trailing-character"),
    pytest.param(f"deploy {NEW_SHA} {DIGEST} extra", id="trailing-argument"),
    pytest.param(f"deploy {NEW_SHA} {DIGEST}\n", id="newline-suffixed"),
    pytest.param(f" deploy {NEW_SHA} {DIGEST}", id="leading-space"),
    pytest.param(f"deploy {NEW_SHA}; id", id="shell-metacharacter-payload"),
    pytest.param(f"rollback {NEW_SHA}", id="manual-subcommand-as-forced-command"),
]


@pytest.mark.parametrize("command", MALFORMED_FORCED_COMMANDS)
def test_rejects_malformed_commands(harness, command):
    """Every malformed forced command is rejected, with the exact grammar-rejection message, before
    deploy.sh ever calls docker -- even though a valid token and registry user are on stdin."""
    result = harness.run(ssh_command=command, stdin=f"{TOKEN}\n{USER}\n")
    assert result.returncode == 2, result.stderr
    assert result.stderr.strip().splitlines()[-1] == "rejected: unexpected command"
    assert harness.docker_calls() == ""


def test_failed_health_rolls_back_and_keeps_both_tags(harness):
    harness.write_tag("current-tag", PREV_SHA)
    harness.write_tag("previous-tag", OTHER_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)
    harness.mark_unhealthy(NEW_SHA)  # the stub curl will report the wrong version for the new sha only

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 1, result.stderr
    assert f"health check failed for {NEW_SHA}" in result.stderr
    assert f"rolled back to {PREV_SHA}" in result.stderr
    assert harness.up_calls() == [NEW_SHA, PREV_SHA]  # tried the new release, then rolled back to it
    assert harness.read_tag("current-tag") == PREV_SHA
    assert harness.read_tag("previous-tag") == OTHER_SHA


def test_stale_loops_rolls_back_and_keeps_tags(harness):
    """A container that answers with the right version but a stale worker loop must be treated as
    unhealthy, exactly like a wrong version: automatic rollback, tag files untouched."""
    _write_executable(harness.bin_dir / "curl", STUB_STALE_CURL)
    (harness.state_dir / "stale-tags").write_text(NEW_SHA + "\n")
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 1, result.stderr
    assert f"health check failed for {NEW_SHA}" in result.stderr
    assert f"rolled back to {PREV_SHA}" in result.stderr
    assert harness.up_calls() == [NEW_SHA, PREV_SHA]
    assert harness.read_tag("current-tag") == PREV_SHA
    assert harness.read_tag("previous-tag") is None


def test_successful_deploy_moves_current_to_previous(harness):
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 0, result.stderr
    assert f"deployed {NEW_SHA}" in result.stderr
    assert harness.read_tag("current-tag") == NEW_SHA
    assert harness.read_tag("previous-tag") == PREV_SHA
    assert f"{IMAGE}:{NEW_SHA}" in harness.tags_created()


@pytest.mark.parametrize(
    ("revision", "volumes", "expected_reason"),
    [
        pytest.param(OTHER_SHA, "null", "revision label is not", id="wrong-revision-label"),
        pytest.param(NEW_SHA, '{"/data":{}}', "declares volumes", id="declared-volumes"),
    ],
)
def test_rejects_image_with_wrong_revision_label_or_declared_volumes(harness, revision, volumes, expected_reason):
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=revision, volumes=volumes)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 2, result.stderr
    assert expected_reason in result.stderr
    assert harness.tags_created() == []  # rejected before `docker tag` ever ran
    # Both checks run after the pull, so the rejected digest must be removed from the shared host.
    assert f"image rm {IMAGE}@{DIGEST}" in harness.docker_calls()


def test_rejects_image_over_the_size_cap_before_pulling(harness):
    """The registry-metadata size check runs before the pull: an oversized image is rejected without
    ever reaching `docker pull`, protecting the shared host's disk and bandwidth."""
    manifest = json.dumps({"config": {"size": 1000}, "layers": [{"size": 500_000_000}, {"size": 1_200_000_000}]})
    harness.set_registry_manifest(f"{IMAGE}@{DIGEST}", manifest)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 2, result.stderr
    assert "size cap" in result.stderr
    assert "pull" not in harness.docker_calls()


@pytest.mark.parametrize(
    ("amd64_layer_bytes", "rejected_before_pull"),
    [
        pytest.param(1_600_000_000, True, id="oversized-platform-image"),
        pytest.param(150_000_000, False, id="platform-image-under-the-cap"),
    ],
)
def test_size_check_follows_an_image_index_to_its_linux_amd64_manifest(
    harness, amd64_layer_bytes, rejected_before_pull
):
    """A pushed build is an image index: the linux/amd64 image plus a provenance attestation whose
    platform is unknown/unknown. The registry check must size the linux/amd64 manifest, not the
    attestation and not the index itself."""
    amd64 = "sha256:" + "1" * 64
    attestation = "sha256:" + "2" * 64
    index = json.dumps({"manifests": [
        {"digest": attestation, "platform": {"os": "unknown", "architecture": "unknown"}},
        {"digest": amd64, "platform": {"os": "linux", "architecture": "amd64"}},
    ]})
    harness.set_registry_manifest(f"{IMAGE}@{DIGEST}", index)
    harness.set_registry_manifest(
        f"{IMAGE}@{amd64}", json.dumps({"config": {"size": 1000}, "layers": [{"size": amd64_layer_bytes}]})
    )
    harness.set_registry_manifest(
        f"{IMAGE}@{attestation}", json.dumps({"config": {"size": 10}, "layers": [{"size": 10}]})
    )
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    if rejected_before_pull:
        assert result.returncode == 2, result.stderr
        assert "size cap (registry check)" in result.stderr
        assert "pull" not in harness.docker_calls()
    else:
        assert result.returncode == 0, result.stderr
        assert "pre-pull size check unavailable" not in result.stderr
        assert harness.read_tag("current-tag") == NEW_SHA


def test_rejects_image_over_the_size_cap_after_pulling_and_removes_it(harness):
    """When the registry lookup fails (no manifest registered here), the post-pull `docker image
    inspect` size check is the backstop, and a rejection after the pull removes the offending digest
    from the shared host."""
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA, size=MAX_IMAGE_BYTES + 1)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 2, result.stderr
    assert "size cap" in result.stderr
    assert "pull" in harness.docker_calls()
    assert any(f"image rm {IMAGE}@{DIGEST}" in call for call in harness.docker_calls().splitlines())


@pytest.mark.parametrize(
    "stdin",
    [
        pytest.param(f"{TOKEN}\n", id="missing-second-line"),
        pytest.param(f"{TOKEN}\nbad user!\n", id="forbidden-characters"),
        # No whitespace at all (so a merely whitespace-based check would wrongly accept this), but a
        # semicolon the real actor-name grammar (letters, digits, hyphen, an optional [bot] suffix)
        # forbids.
        pytest.param(f"{TOKEN}\nuser;injected\n", id="forbidden-character-without-whitespace"),
    ],
)
def test_rejects_a_missing_or_malformed_registry_user(harness, stdin):
    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=stdin)

    assert result.returncode == 2, result.stderr
    assert harness.docker_calls() == ""  # rejected before any `docker login`


def test_registry_token_never_appears_in_recorded_docker_argv(harness):
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert TOKEN not in harness.docker_calls()


def test_registry_session_is_logged_out_after_the_pull(harness):
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert not harness.is_logged_in()


def test_throwaway_docker_config_is_removed_even_on_rejection(harness):
    """The EXIT trap on the throwaway config directory is the only cleanup on a rejection path (the
    explicit `rm -rf` only runs after every check has already passed), so this must reject *after* the
    pull -- a wrong revision label -- to actually exercise it."""
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=OTHER_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 2, result.stderr
    assert not harness.config_dir_from_first_call().exists()


def test_stdin_token_read_has_a_timeout(harness):
    """A CI client that connects but never sends the token must not hang deploy.sh forever on a
    shared host: the read is bounded, so a stalled connection is rejected instead of leaving a
    process running indefinitely. This test genuinely waits out that bound (~15s)."""
    read_end, write_end = os.pipe()  # stays open and empty: never closed, never written to
    try:
        result = subprocess.run(
            ["bash", str(harness.deploy_dir / "deploy.sh")],
            env=harness.env(ssh_command=f"deploy {NEW_SHA} {DIGEST}"),
            stdin=read_end,
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    finally:
        os.close(read_end)
        os.close(write_end)

    assert result.returncode == 2
    assert "no registry token on stdin" in result.stderr


def test_rollback_swaps_the_tags(harness):
    harness.write_tag("current-tag", NEW_SHA)
    harness.write_tag("previous-tag", PREV_SHA)
    harness.mark_tag_exists(f"{IMAGE}:{PREV_SHA}")

    result = harness.run("rollback")

    assert result.returncode == 0, result.stderr
    assert f"rolled back to {PREV_SHA}" in result.stderr
    assert harness.read_tag("current-tag") == PREV_SHA
    assert harness.read_tag("previous-tag") == NEW_SHA


def test_failed_manual_rollback_restores_the_current_tag(harness):
    harness.write_tag("current-tag", NEW_SHA)
    harness.write_tag("previous-tag", PREV_SHA)
    harness.mark_tag_exists(f"{IMAGE}:{PREV_SHA}")
    harness.mark_unhealthy(PREV_SHA)  # the previous image no longer starts healthy either

    result = harness.run("rollback")

    assert result.returncode == 1, result.stderr
    assert harness.up_calls() == [PREV_SHA, NEW_SHA]  # tried the previous tag, then restored current
    assert harness.read_tag("current-tag") == NEW_SHA
    assert harness.read_tag("previous-tag") == PREV_SHA


def test_lock_contention_refuses_and_never_starts(harness):
    """A real advisory lock (fcntl.flock, exactly like the util-linux `flock` deploy.sh calls), held
    by another process, must make the script refuse immediately without ever calling `up`."""
    _write_executable(harness.bin_dir / "flock", STUB_REAL_FLOCK)
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)
    with open(harness.deploy_dir / ".deploy.lock", "w") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)  # an apply/rollback/stop/start is "in progress"
        result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 75, result.stderr
    assert harness.up_calls() == []


def test_prune_removes_only_the_oldest_atelier_tags_and_sweeps_dangling(harness):
    """A realistic, newest-first tag list: KEEP=3 protects current, previous and the single newest
    other Atelier tag, so among three older candidates only the two oldest are removed, each only by
    its own Atelier-repository digest reference -- a Folio digest recorded alongside one is untouched.
    The dangling sweep runs independently and only ever lists this repository's own images."""
    o1, o2, o3 = "1" * 40, "2" * 40, "3" * 40  # o1 is newest-of-the-rest, o3 is oldest
    harness.set_ls_tags([NEW_SHA, o1, PREV_SHA, o2, o3])
    harness.set_dangling_ids(["dangling0000"])
    folio_digest = f"europe-west1-docker.pkg.dev/x/folio@sha256:{'f' * 64}"
    for tag in (o1, o2, o3):
        harness.set_repo_digests(f"{IMAGE}:{tag}", [f"{IMAGE}@sha256:{tag[0] * 64}", folio_digest])
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 0, result.stderr
    removed = harness.rm_calls()
    removed_tags = {call.split()[0].rsplit(":", 1)[-1] for call in removed if call.split()[0].startswith(IMAGE)}
    assert removed_tags == {o2, o3}  # o1 (the single newest-other) survives; current/previous were never candidates
    assert not any("folio" in call for call in removed)  # never another repository's reference
    assert any(call == "dangling0000" for call in removed)  # the dangling sweep also ran


def _old_images_with_digests(harness, tags, *, extra_digest: str | None = None) -> None:
    """Registers each tag with its own Atelier digest reference (plus an optional foreign one), all
    as existing images, the way a real host lists them after earlier deploys."""
    for tag in tags:
        own = f"{IMAGE}@sha256:{tag[0] * 64}"
        harness.set_repo_digests(f"{IMAGE}:{tag}", [own] + ([extra_digest] if extra_digest else []))
        harness.mark_tag_exists(own)
    if extra_digest:
        harness.mark_tag_exists(extra_digest)


def test_prune_logs_each_removal_when_the_tag_takes_its_digest_with_it(harness):
    """On the containerd image store (the server's), removing an image's last tag also removes its
    digest reference. Prune must not then remove that reference a second time: no daemon error reaches
    the deploy's output, and each removal is logged."""
    (harness.state_dir / "containerd-store").touch()
    o1, o2 = "1" * 40, "2" * 40
    harness.set_ls_tags([NEW_SHA, o1, PREV_SHA, o2])
    _old_images_with_digests(harness, (o1, o2))
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 0, result.stderr
    assert "No such image" not in result.stderr
    assert f"pruned {o2}" in result.stderr.splitlines()
    assert not harness.image_exists(f"{IMAGE}:{o2}")
    assert not harness.image_exists(f"{IMAGE}@sha256:{'2' * 64}")
    assert harness.image_exists(f"{IMAGE}:{o1}")


def test_prune_removes_digest_references_the_classic_store_leaves_behind(harness):
    """On the classic image store, removing a tag leaves the image's digest reference, which would
    keep its layers on the shared disk. Prune removes Atelier's own leftover reference too, and never
    another repository's."""
    o1, o2 = "1" * 40, "2" * 40
    folio_digest = f"europe-west1-docker.pkg.dev/x/folio@sha256:{'f' * 64}"
    harness.set_ls_tags([NEW_SHA, o1, PREV_SHA, o2])
    _old_images_with_digests(harness, (o1, o2), extra_digest=folio_digest)
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 0, result.stderr
    assert not harness.image_exists(f"{IMAGE}:{o2}")
    assert not harness.image_exists(f"{IMAGE}@sha256:{'2' * 64}")
    assert harness.image_exists(folio_digest)
    assert harness.image_exists(f"{IMAGE}:{o1}") and harness.image_exists(f"{IMAGE}@sha256:{'1' * 64}")
    assert f"pruned {o2}" in result.stderr.splitlines()


def test_prune_reports_an_image_it_could_not_remove_without_failing_the_deploy(harness):
    """An old image the daemon refuses to remove (for example, still used by a stopped container)
    is logged as not pruned, and the deploy that already succeeded still reports success."""
    o1, o2 = "1" * 40, "2" * 40
    harness.set_ls_tags([NEW_SHA, o1, PREV_SHA, o2])
    _old_images_with_digests(harness, (o1, o2))
    (harness.state_dir / "rm-refused").write_text(f"{IMAGE}:{o2}\n")
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 0, result.stderr
    assert f"could not prune {o2}" in result.stderr.splitlines()
    assert f"pruned {o2}" not in result.stderr.splitlines()
    assert harness.image_exists(f"{IMAGE}:{o2}")


def test_prune_is_scoped_to_its_own_repository(harness):
    """`docker image ls` must be called with the repository filter, not globally. This test's stub
    docker only returns Atelier's own tags when that filter is present on the argv, and a realistic,
    other-repository-polluted listing otherwise, so a missing filter is observable: two unrelated
    tags would consume the "newest other image" slot that KEEP=3 reserves, wrongly pruning it."""
    o1, o2 = "1" * 40, "2" * 40
    harness.set_ls_tags([NEW_SHA, o1, PREV_SHA, o2])  # correctly scoped: only Atelier's own tags
    harness.set_ls_tags_unfiltered(["f" * 40, "e" * 40, NEW_SHA, o1, PREV_SHA, o2])
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 0, result.stderr
    removed = harness.rm_calls()
    removed_tags = {call.split()[0].rsplit(":", 1)[-1] for call in removed if call.split()[0].startswith(IMAGE)}
    assert removed_tags == {o2}  # o1 is the single newest-other candidate and must survive


def test_systemd_run_arguments_are_correct(harness):
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    calls = harness.systemd_run_calls()
    assert len(calls) == 1
    for expected in (
        "--unit=atelier-apply", "-p Type=oneshot", "--wait", "--collect", "--pipe", "--quiet",
        "--setenv=ATELIER_DETACHED=1", f"--setenv=ATELIER_DEPLOY_DIR={harness.deploy_dir} ",
    ):
        assert expected in calls[0], calls[0]


def test_forced_command_ignores_a_deploy_dir_from_the_environment(harness, tmp_path):
    """On the forced-command path the directory comes from the script's own location. A deploy dir
    set in the caller's environment reaches neither the pull phase nor the detached apply."""
    decoy = tmp_path / "decoy"
    decoy.mkdir()
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(
        ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n", deploy_dir_env=decoy
    )

    assert result.returncode == 0, result.stderr
    assert harness.read_tag("current-tag") == NEW_SHA
    assert harness.read_tag("previous-tag") == PREV_SHA
    assert list(decoy.iterdir()) == []
    assert str(decoy) not in harness.systemd_run_calls()[0]


def test_apply_refuses_during_maintenance(harness):
    harness.write_tag("current-tag", PREV_SHA)
    (harness.deploy_dir / "maintenance").touch()
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n")

    assert result.returncode == 75, result.stderr
    assert "maintenance" in result.stderr
    assert harness.up_calls() == []
    assert harness.read_tag("current-tag") == PREV_SHA


def test_stop_enters_maintenance_and_start_clears_it(harness):
    harness.write_tag("current-tag", PREV_SHA)

    stopped = harness.run("stop")
    assert stopped.returncode == 0, stopped.stderr
    assert (harness.deploy_dir / "maintenance").exists()
    assert harness.read_tag("current-tag") == PREV_SHA  # stop never touches the tag files

    started = harness.run("start")
    assert started.returncode == 0, started.stderr
    assert not (harness.deploy_dir / "maintenance").exists()
    assert harness.up_calls() == [PREV_SHA]


def test_status_reports_current_and_previous_tags(harness):
    harness.write_tag("current-tag", PREV_SHA)
    harness.write_tag("previous-tag", OTHER_SHA)

    result = harness.run("status")

    assert result.returncode == 0, result.stderr
    assert f"current={PREV_SHA} previous={OTHER_SHA}" in result.stdout


def test_survives_closed_stdout_and_stderr(harness):
    """The forced-command path (deploy -> apply) is the one that must outlive a dropped SSH session,
    so this repeats the successful-deploy flow with fds 1 and 2 genuinely closed, not merely
    redirected, and checks the same outcome as the ordinary happy path."""
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)

    result = harness.run(
        ssh_command=f"deploy {NEW_SHA} {DIGEST}", stdin=f"{TOKEN}\n{USER}\n", close_stdio=True
    )

    assert result.returncode == 0
    assert harness.read_tag("current-tag") == NEW_SHA
    assert harness.read_tag("previous-tag") == PREV_SHA


def test_survives_a_broken_pipe_like_a_vanished_ssh_peer(harness):
    """stdout and stderr are a pipe whose read end has already been closed, so every write raises
    SIGPIPE -- a closer simulation of a dropped SSH connection than a fully closed file descriptor,
    since the peer's read end going away is exactly what SIGPIPE models."""
    harness.write_tag("current-tag", PREV_SHA)
    harness.set_image(f"{IMAGE}@{DIGEST}", revision=NEW_SHA)
    read_end, write_end = os.pipe()
    os.close(read_end)
    try:
        result = subprocess.run(
            ["bash", str(harness.deploy_dir / "deploy.sh")],
            env=harness.env(ssh_command=f"deploy {NEW_SHA} {DIGEST}"),
            input=f"{TOKEN}\n{USER}\n".encode(),
            stdout=write_end,
            stderr=write_end,
            timeout=10,
            check=False,
        )
    finally:
        os.close(write_end)

    assert result.returncode == 0
    assert harness.read_tag("current-tag") == NEW_SHA
    assert harness.read_tag("previous-tag") == PREV_SHA
