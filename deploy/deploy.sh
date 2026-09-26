#!/usr/bin/env bash
# Atelier deploy entry point.
#   Forced command of the CI key:  SSH_ORIGINAL_COMMAND="deploy <40-hex sha> sha256:<64-hex digest>",
#                                   with a short-lived registry token and the registry user on stdin.
#   Owner at a root shell:         deploy.sh rollback | stop | start | status
# apply, rollback, stop and start all run detached, inside a transient systemd unit, so a dropped SSH
# or terminal session cannot interrupt a change half-way. Nothing outside /opt/atelier and Atelier's
# own images is ever touched.
set -euo pipefail
umask 077
readonly IMAGE=ghcr.io/flowitup/atelier KEEP=3 MAX_IMAGE_BYTES=1500000000
log()           { logger -t atelier-deploy -- "$*"; printf '%s\n' "$*" >&2 2>/dev/null || true; }
reject()        { log "rejected: $1"; exit 2; }
reject_pulled() { docker image rm "$IMAGE@$digest" >/dev/null 2>&1 || true; log "rejected: $1"; exit 2; }
lock()          { exec 9>"$DIR/.deploy.lock"; flock -n 9 || { log "another deploy is running"; exit 75; }; }
dc()            { docker compose -f "$DIR/compose.yaml" -p atelier "$@"; }
up()            { ATELIER_TAG="$1" dc up -d --wait --wait-timeout 60 2>&1 | logger -t atelier-deploy; }
healthy() {
  local body; body=$(curl -fsS --max-time 5 http://127.0.0.1:8090/healthz 2>/dev/null) || return 1
  [[ "$body" == *"\"version\":\"$1\""* && "$body" == *"\"loops\":\"ok\""* ]]
}
write_tags() {   # $1 = new current, $2 = new previous; each file is replaced by an atomic rename
  printf '%s\n' "$2" > previous-tag.new && printf '%s\n' "$1" > current-tag.new
  mv -f previous-tag.new previous-tag && mv -f current-tag.new current-tag
}
prune() {        # best-effort cleanup: keep current, previous and the newest other Atelier tag, and
                  # sweep this repository's own dangling images; a failure here must never fail a
                  # deploy that already succeeded, but is logged so a real failure isn't silently lost
  docker image ls "$IMAGE" --format '{{.Tag}}' | grep -E '^[0-9a-f]{40}$' \
    | awk -v c="$1" -v p="$2" -v k="$KEEP" '$0 != c && $0 != p { if (++n > k - 2) print }' \
    | while read -r old; do
        refs=$(docker image inspect -f '{{range .RepoDigests}}{{println .}}{{end}}' "$IMAGE:$old" | grep "^$IMAGE@" || true)
        # shellcheck disable=SC2086 # $refs is a deliberately unquoted list of zero or more digest refs
        docker image rm "$IMAGE:$old" $refs >/dev/null && log "pruned $old"
      done
  docker image ls "$IMAGE" --filter dangling=true --format '{{.ID}}' \
    | while read -r id; do docker image rm "$id" >/dev/null && log "pruned dangling $id"; done
}
size_from_registry() {   # best-effort: the linux/amd64 image's compressed byte size from registry
                          # metadata alone, without pulling (`docker manifest inspect` is built into
                          # the CLI; the host has no buildx); prints nothing and returns non-zero when
                          # the lookup fails
  local raw manifest_digest
  raw=$(docker --config "$cfg" manifest inspect "$IMAGE@$digest" 2>/dev/null) || return 1
  if jq -e 'has("manifests")' >/dev/null 2>&1 <<<"$raw"; then
    manifest_digest=$(jq -r '.manifests[] | select(.platform.os=="linux" and .platform.architecture=="amd64") | .digest' <<<"$raw" | head -1)
    [[ -n "$manifest_digest" ]] || return 1
    raw=$(docker --config "$cfg" manifest inspect "$IMAGE@$manifest_digest" 2>/dev/null) || return 1
  fi
  jq -e '([.layers[].size] | add // 0) + (.config.size // 0)' <<<"$raw"
}
detach() {   # re-exec ourselves inside a transient, clean-environment systemd unit; the unit gets the
             # directory already resolved below, never a value from the caller's own environment
  exec systemd-run --unit="atelier-$1" -p Type=oneshot --wait --collect --pipe --quiet \
    --setenv=ATELIER_DETACHED=1 --setenv="ATELIER_DEPLOY_DIR=$DIR" "$DIR/deploy.sh" "$@"
}

trap '' PIPE                                        # a vanished SSH or terminal session must not stop a change
trap 'log "interrupted by signal"; exit 1' TERM HUP INT

if [[ -n "${SSH_ORIGINAL_COMMAND:-}" ]]; then        # the CI key may only deploy; DIR is derived from
  DIR=$(dirname "$0")                                # $0 here, never from an environment variable, so
  readonly DIR                                        # there is nothing for a forced command to override
                                                      # even if a future misconfiguration stopped sshd
                                                      # from stripping the client's own environment
  [[ "$SSH_ORIGINAL_COMMAND" =~ ^deploy\ ([0-9a-f]{40})\ (sha256:[0-9a-f]{64})$ ]] || reject "unexpected command"
  verb=deploy; sha=${BASH_REMATCH[1]}; digest=${BASH_REMATCH[2]}
else
  readonly DIR=${ATELIER_DEPLOY_DIR:-/opt/atelier}
  verb=${1:-}; sha=${2:-}
  [[ "$verb" =~ ^(rollback|stop|start|status|apply)$ ]] || reject "unexpected command"
fi

if [[ "$verb" =~ ^(apply|stop|start|rollback)$ && -z "${ATELIER_DETACHED:-}" ]]; then
  if [[ "$verb" == apply ]]; then detach apply "$sha"; else detach "$verb"; fi
fi

case "$verb" in
  deploy)
    # sha and digest already satisfy these exact shapes: they came from the anchored whole-string
    # match above, which is at least as strict as either pattern on its own.
    IFS= read -r -t 15 token || reject "no registry token on stdin"
    IFS= read -r -t 5 user || reject "no registry user on stdin"
    [[ "$user" =~ ^[A-Za-z0-9][A-Za-z0-9-]{0,38}(\[bot\])?$ ]] || reject "bad registry user"
    cfg=$(mktemp -d); trap 'rm -rf "$cfg"' EXIT      # throwaway docker config; root's ~/.docker is never touched
    printf '%s' "$token" | docker --config "$cfg" login ghcr.io -u "$user" --password-stdin >/dev/null
    unset token
    if size=$(size_from_registry) && [[ "$size" =~ ^[0-9]+$ ]]; then
      (( size <= MAX_IMAGE_BYTES )) || reject "image is larger than the size cap (registry check)"
    else
      log "pre-pull size check unavailable (registry lookup failed); relying on the post-pull check"
    fi
    docker --config "$cfg" pull --quiet "$IMAGE@$digest" >/dev/null
    docker --config "$cfg" logout ghcr.io >/dev/null 2>&1 || true
    rev=$(docker image inspect "$IMAGE@$digest" --format '{{index .Config.Labels "org.opencontainers.image.revision"}}')
    [[ "$rev" == "$sha" ]] || reject_pulled "image revision label is not $sha"
    vols=$(docker image inspect "$IMAGE@$digest" --format '{{json .Config.Volumes}}')
    [[ "$vols" == "null" || "$vols" == "{}" ]] || reject_pulled "image declares volumes"
    size=$(docker image inspect "$IMAGE@$digest" --format '{{.Size}}')
    (( size <= MAX_IMAGE_BYTES )) || reject_pulled "image is larger than the size cap"
    docker tag "$IMAGE@$digest" "$IMAGE:$sha"
    rm -rf "$cfg"; trap - EXIT
    detach apply "$sha"
    ;;
  apply)                                             # runs inside the transient unit
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || reject "sha must be 40 lowercase hex"
    lock; cd "$DIR"
    if [[ -e maintenance ]]; then log "refused: maintenance mode is active"; exit 75; fi
    prev=$(cat current-tag 2>/dev/null || true)
    status=0
    if up "$sha" && healthy "$sha"; then
      if [[ -n "$prev" && "$prev" != "$sha" ]]; then write_tags "$sha" "$prev"; else printf '%s\n' "$sha" > current-tag; fi
      log "deployed $sha"
    else
      log "health check failed for $sha"
      if [[ -n "$prev" ]] && up "$prev" && healthy "$prev"; then log "rolled back to $prev"
      else log "rollback impossible or failed; previous=${prev:-none}"; fi
      status=1
    fi
    prune "$(cat current-tag)" "$(cat previous-tag 2>/dev/null || true)" || log "prune failed (ignored)"
    exit "$status"
    ;;
  rollback)
    lock; cd "$DIR"; cur=$(cat current-tag); prev=$(cat previous-tag 2>/dev/null || true)
    if [[ -z "$prev" ]] || ! docker image inspect "$IMAGE:$prev" >/dev/null 2>&1; then
      log "no previous image on this host"; exit 1
    fi
    if up "$prev" && healthy "$prev"; then write_tags "$prev" "$cur"; log "rolled back to $prev"; exit 0; fi
    log "rollback to $prev failed its health check; restoring $cur"; up "$cur" && healthy "$cur"; exit 1
    ;;
  stop)
    lock; cd "$DIR"; touch maintenance
    ATELIER_TAG=$(cat current-tag) dc down 2>&1 | logger -t atelier-deploy
    ;;
  start)
    lock; cd "$DIR"; t=$(cat current-tag)
    up "$t" && healthy "$t" && rm -f maintenance
    ;;
  status)
    cd "$DIR"; echo "current=$(cat current-tag) previous=$(cat previous-tag 2>/dev/null || true)"
    [[ -e maintenance ]] && echo "maintenance mode is active"
    ATELIER_TAG=$(cat current-tag) dc ps; curl -fsS --max-time 5 http://127.0.0.1:8090/healthz || true
    ;;
esac
