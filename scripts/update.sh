#!/usr/bin/env bash
# VPN-HUB BOT — safe one-command update.
#
#   cd HUB-BOT && ./scripts/update.sh
#
# Order matters so a broken update never loses data:
#   1. dump the DB into ./backups/  (rollback insurance)
#   2. git pull --ff-only           (never rewrites local history)
#   3. rebuild images + restart     (web runs alembic migrations on start)
#   4. wait for /health             (fail loudly with rollback instructions)
set -euo pipefail

B=$'\033[1m'; DIM=$'\033[2m'; R=$'\033[0m'
ORANGE=$'\033[38;5;208m'; GREEN=$'\033[1;32m'; RED=$'\033[1;31m'
LINE="────────────────────────────────────────────────────────"

hr()   { printf "%s%s%s\n" "$DIM" "$LINE" "$R"; }
step() { printf "\n%s[%s/4]%s %s%s%s\n" "$ORANGE" "$1" "$R" "$B" "$2" "$R"; }
ok()   { printf "  %s✔%s %s\n" "$GREEN" "$R" "$*"; }
fail() { printf "\n  %s✗ %s%s\n" "$RED" "$*" "$R"; exit 1; }

run_spin() { # run_spin "подпись" cmd...
  local label=$1; shift
  # Template must END in X's: busybox mktemp (the updater sidecar is alpine-based) rejects a
  # suffix after them with "mktemp: : Invalid argument". That aborted step 3 on every
  # button-triggered update — the sidecar path — while host runs (GNU mktemp) worked fine.
  local log; log=$(mktemp /tmp/vpnhub-update.XXXXXX)
  printf "  %s…%s %s " "$DIM" "$R" "$label"
  if "$@" >"$log" 2>&1; then
    printf "\r  %s✔%s %s%s\n" "$GREEN" "$R" "$label" "          "
    rm -f "$log"
  else
    printf "\r  %s✗ %s — последние строки лога:%s\n" "$RED" "$label" "$R"
    tail -n 25 "$log" | sed 's/^/    /'
    printf "  %sполный лог: %s%s\n" "$DIM" "$log" "$R"
    exit 1
  fi
}

# Serialize updates: a host run and the sidecar (or the 6 h auto-check) must never
# `docker compose build/up` the same project at once — concurrent recreation races the stack.
# Re-exec under an flock; a second run waits up to 30 min, then gives up. Skipped if flock absent.
if [ -z "${_VPNHUB_LOCKED:-}" ] && command -v flock >/dev/null 2>&1; then
  # Lock in the repo (bind-mounted identically on host and in the updater sidecar) so a host-run
  # update and the sidecar/6h auto-run actually serialize — /tmp is NOT shared between them.
  exec env _VPNHUB_LOCKED=1 flock -w 1800 "$(dirname "$0")/../.update.lock" bash "$0" "$@"
fi

cd "$(dirname "$0")/.."
# --env-file .env: Compose resolves ${VAR:?} interpolation against the compose file's dir
# (docker/), not the CWD, so without this it can't find our repo-root .env.
COMPOSE="docker compose --env-file .env -f docker/compose.prod.yml"
[ -f .env ] || fail ".env не найден — сначала установка: ./scripts/install.sh"

# Self-heal the updater profile. Installs made before the updater existed (or copied from
# .env.example, which had no COMPOSE_PROFILES) never start the sidecar, so «Обновить» in the bot
# answers «модуль обновлений не подключён» и предлагает запустить этот самый скрипт — который
# раньше ничего не чинил. Дописываем профиль один раз, идемпотентно.
ensure_updater_profile() {
  local cur new
  cur=$(grep -E '^COMPOSE_PROFILES=' .env | head -1 | cut -d= -f2- || true)
  case ",${cur}," in *,updater,*) return 0 ;; esac
  if grep -qE '^COMPOSE_PROFILES=' .env; then
    new=$([ -n "$cur" ] && echo "${cur},updater" || echo "updater")
    sed -i.bak -E "s|^COMPOSE_PROFILES=.*|COMPOSE_PROFILES=${new}|" .env && rm -f .env.bak
  else
    printf '\nCOMPOSE_PROFILES=updater\n' >>.env
  fi
  echo "  ✓ включил профиль updater — авто-обновление заработает после этого запуска"
}
ensure_updater_profile

# Путь установки на хосте — бот показывает его оператору вместо угадывания имени папки
# (`cd HUB-BOT` кончался «No such file or directory» там, где клон лежит в другом месте).
ensure_host_repo_dir() {
  local here; here=$(pwd)
  if grep -qE '^APP__HOST_REPO_DIR=' .env 2>/dev/null; then
    sed -i.bak -E "s|^APP__HOST_REPO_DIR=.*|APP__HOST_REPO_DIR=${here}|" .env && rm -f .env.bak
  else
    printf '\nAPP__HOST_REPO_DIR=%s\n' "$here" >>.env
  fi
}
ensure_host_repo_dir

notify_owner() { # best-effort Telegram DM of the update outcome to the owners; never fails us
  command -v curl >/dev/null 2>&1 || return 0
  local text=$1 token ids id
  # `|| true`: a grep miss returns 1, and under `set -o pipefail` that would abort the whole
  # script on the assignment. Reading an optional .env key must never fail the update.
  token=$(grep '^BOT__TOKEN=' .env | cut -d= -f2- || true)
  # APP__OWNER_IDS is stored as a JSON list — `[959558954]` or `[1, 2]`. Strip the brackets,
  # quotes and spaces, else chat_id becomes `[959558954]` and Telegram rejects it silently, so
  # the owner never got the update notification (the exact «нет ОС от бота» complaint).
  ids=$(grep '^APP__OWNER_IDS=' .env | cut -d= -f2- | tr -d '[]" ' | tr ',' ' ' || true)
  [ -n "$token" ] && [ -n "$ids" ] || return 0
  for id in $ids; do
    curl -fsS --max-time 8 -o /dev/null "https://api.telegram.org/bot${token}/sendMessage" \
      --data-urlencode "chat_id=${id}" --data-urlencode "text=${text}" 2>/dev/null || true
  done
}

bring_up() { # (re)create+start the stack; never recreate the updater if we ARE it
  if [ -n "${SKIP_UPDATER_RECREATE:-}" ]; then
    # Triggered from inside the updater container: recreate every service EXCEPT updater,
    # else `up -d` would kill this very process mid-update. The updater keeps the old code
    # (it's a tiny watch loop); to update it too, run ./scripts/update.sh on the host once.
    # Naming a service on the CLI activates it even if its profile is off — on a "behind an
    # existing proxy" install (caddy omitted from COMPOSE_PROFILES, :80/:443 already taken)
    # that would try to start caddy, fail to bind, and abort the whole update. So include
    # caddy only when its profile is actually enabled.
    local svc="postgres redis web bot worker scheduler"
    grep -qE '^COMPOSE_PROFILES=.*caddy' .env 2>/dev/null && svc="$svc caddy"
    $COMPOSE up -d --no-deps $svc
  else
    $COMPOSE up -d
  fi
}

# An update that dies half-way (dropped ssh, ^C, failed build, OOM) used to leave the stack
# down: `up -d` creates containers before starting them, so an interrupt strands bot/worker/
# scheduler in "Created" and the bot never comes back. Always try to put the stack back on
# its feet — with the new image if the build got that far, otherwise the old one.
_recovered=0
recover() {
  local code=$?
  [ "$code" -eq 0 ] && return 0
  [ "$_recovered" -eq 1 ] && return 0
  _recovered=1
  printf "\n  %s⚠ обновление прервано — возвращаю сервисы в строй%s\n" "$ORANGE" "$R"
  bring_up >/dev/null 2>&1 || true
  $COMPOSE ps 2>/dev/null | tail -n +2 | sed 's/^/    /' || true
  # Tell the owner it failed instead of leaving them to believe «Обновить» succeeded (the button
  # path only logs to a volume nobody watches). Best-effort; bring_up already restored the stack.
  notify_owner "⚠️ Обновление бота не завершилось (код $code). Сервисы возвращены в строй, бэкап БД цел. Загляни в логи обновления на сервере."
}
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
trap recover EXIT

printf "\n"; hr
printf "   %sVPN%s%s-HUB%s %sBOT%s  %s· безопасное обновление%s\n" \
  "$B" "$R" "$ORANGE$B" "$R" "$B" "$R" "$DIM" "$R"
hr

OLD_REV=$(git rev-parse --short HEAD)

# --- 1. backup ----------------------------------------------------------------
step 1 "Бэкап БД"
mkdir -p backups
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP="backups/pre-update-$STAMP.sql.gz"
DB_USER=$(grep '^DATABASE__USER=' .env | cut -d= -f2 || true)
DB_NAME=$(grep '^DATABASE__NAME=' .env | cut -d= -f2 || true)
$COMPOSE exec -T postgres pg_dump -U "${DB_USER:-vpn}" "${DB_NAME:-vpn}" | gzip > "$BACKUP" \
  || fail "бэкап не снялся — обновление отменено (стек запущен? $COMPOSE ps)"
[ -s "$BACKUP" ] || fail "бэкап пустой — обновление отменено"
ok "снят: $BACKUP ($(du -h "$BACKUP" | cut -f1))"
# Keep only the 5 most recent pre-update dumps so they can't silently fill the disk over many
# updates (a full disk would itself brick the next pg_dump/build). tail -n +6 = everything after
# the newest 5; xargs -r no-ops on an empty list.
ls -1t backups/pre-update-*.sql.gz 2>/dev/null | tail -n +6 | xargs -r rm -f || true

# --- 2. pull ------------------------------------------------------------------
step 2 "Обновления из git"
git pull --ff-only >/dev/null 2>&1 || fail "git pull не прошёл — есть локальные правки? (git stash, затем повторить)"
NEW_REV=$(git rev-parse --short HEAD)
# Optional supply-chain guard: with UPDATE_VERIFY_SIGNATURE=1 in .env, refuse to build a commit
# that isn't signed by a trusted key (git verify-commit uses the host's gpg keyring / allowed
# signers). Off by default so existing installs keep updating; turn on once you sign your releases.
if grep -qiE '^UPDATE_VERIFY_SIGNATURE=(1|true|yes)$' .env 2>/dev/null; then
  git verify-commit HEAD >/dev/null 2>&1 \
    || fail "подпись коммита $NEW_REV не проверена — обновление остановлено (UPDATE_VERIFY_SIGNATURE=1)"
  ok "подпись коммита проверена"
fi
if [ "$OLD_REV" = "$NEW_REV" ]; then
  ok "уже последняя версия ($NEW_REV) — пересобираю на всякий случай"
else
  ok "$OLD_REV → $NEW_REV"
  # `|| true`: under `set -o pipefail` a large commit range makes `head` close the pipe early,
  # SIGPIPE-killing `git log` (exit 141) and aborting the whole update AFTER the pull advanced
  # HEAD but before the rebuild. This line is cosmetic — never let it fail the update.
  git log --oneline "$OLD_REV..$NEW_REV" | head -8 | sed "s/^/    ${DIM}·${R} /" || true
fi

# --- 3. rebuild + restart -------------------------------------------------------
step 3 "Пересборка и перезапуск"
# Re-bake the git SHA after the pull so the update checker reports the new revision.
export GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
run_spin "docker compose build" $COMPOSE build
if [ -n "${SKIP_UPDATER_RECREATE:-}" ]; then
  run_spin "docker compose up -d (без updater)" bring_up
else
  run_spin "docker compose up -d" bring_up
fi

# --- 3b. re-attach web to an external reverse-proxy network (optional) ----------
# `docker network connect` is imperative: it's lost every time compose recreates the
# container. A `web` fronted by an EXISTING proxy on another network (e.g. a shared Caddy
# that already owns :443) therefore drops off the proxy on each update and its domain
# starts 502-ing. Set WEB_PROXY_NETWORK=<external network name> in .env to have every
# update re-attach it. No-op when unset or when the network doesn't exist.
# `|| true`: WEB_PROXY_NETWORK is OPTIONAL — a grep miss returns 1, and under `set -o pipefail`
# an unguarded `VAR=$(grep …)` would abort the update right here (after a successful up -d, before
# the health gate), firing the recover trap and a false "update failed" notice. This was that bug.
PROXY_NET=$(grep -E '^WEB_PROXY_NETWORK=' .env 2>/dev/null | cut -d= -f2- | xargs || true)
if [ -n "${PROXY_NET:-}" ]; then
  WEB_CID=$($COMPOSE ps -q web 2>/dev/null)
  if [ -n "$WEB_CID" ] && docker network inspect "$PROXY_NET" >/dev/null 2>&1; then
    docker network connect "$PROXY_NET" "$WEB_CID" 2>/dev/null \
      && ok "web подключён к сети прокси: $PROXY_NET" \
      || ok "web уже в сети прокси: $PROXY_NET"
  fi
fi

# --- 4. health-gate -------------------------------------------------------------
step 4 "Миграции и здоровье"
printf "  %s…%s жду /health " "$DIM" "$R"
for _ in $(seq 1 90); do
  if $COMPOSE exec -T web \
       python -c "import urllib.request as u; u.urlopen('http://localhost:8080/health', timeout=3)" \
       >/dev/null 2>&1; then
    printf "\n"
    ok "живой"
    printf "\n"; hr
    printf "   %s🎉 Обновлено до %s%s  %s(бэкап: %s)%s\n" "$GREEN$B" "$NEW_REV" "$R" "$DIM" "$BACKUP" "$R"
    hr
    notify_owner "✅ Бот обновлён до ${NEW_REV} и снова в строю. Бэкап БД: ${BACKUP}."
    exit 0
  fi
  printf "."
  sleep 2
done
printf "\n"

printf "\n  %s✗ web не поднялся после обновления.%s\n\n" "$RED" "$R"
printf "   %sЛоги%s    $COMPOSE logs --tail 100 web\n" "$DIM" "$R"
printf "   %sОткат%s   git checkout %s && $COMPOSE up -d --build\n" "$DIM" "$R" "$OLD_REV"
printf "   %sБД%s      gunzip -c %s | $COMPOSE exec -T postgres psql -U %s %s\n" \
  "$DIM" "$R" "$BACKUP" "${DB_USER:-vpn}" "${DB_NAME:-vpn}"
exit 1
