# Self-update sidecar: docker CLI + compose plugin (bundled in docker:cli) + git + bash.
# It bind-mounts the host repo and docker socket to run scripts/update.sh on the host daemon.
FROM docker:27-cli

# curl: DM the owner the update's outcome via the Bot API. gnupg: `git verify-commit` when
# UPDATE_VERIFY_SIGNATURE=1 (without it, signature checks fail even for correctly-signed commits).
# util-linux: GNU flock. BusyBox's built-in flock has no -w/--timeout, which scripts/update.sh
# uses to serialize concurrent updates.
RUN apk add --no-cache bash git curl gnupg util-linux

COPY scripts/updater.sh /usr/local/bin/updater.sh
RUN chmod +x /usr/local/bin/updater.sh

ENTRYPOINT ["/usr/local/bin/updater.sh"]
