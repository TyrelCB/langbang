#!/usr/bin/env bash
# home-backup — weekly full + nightly incremental tar archives, pushed off-box
# with rsync. Filesystem-agnostic: GNU tar --listed-incremental + zstd, no
# btrfs/LVM snapshots.
#
#   home-backup.sh [run]           nightly job (full on FULL_DOW or when no cycle)
#   home-backup.sh run --full      force a new cycle starting with a full
#   home-backup.sh list            cycles/archives, local and remote
#   home-backup.sh restore CYCLE DEST [UNTIL]
#                                  replay CYCLE's full + incrementals (up to
#                                  archive name prefix UNTIL) into empty DEST
#
# Layout (ARCHIVE_DIR and REMOTE mirror each other):
#   cycles/<YYYYmmdd-HHMMSS>/          one cycle = one full + its incrementals
#       <stamp>-full.tar.zst           level 0
#       <stamp>-incr.tar.zst           level 1..n, chained (restore all, in order)
#       <stamp>-*.tar.zst.sha256       verified locally and again on the remote
#       state.snar                     tar's incremental state for the cycle
#
# Live SQLite databases (any *.db with a -wal sibling under SOURCES) are not
# tarred directly — a copy of a db mid-write is torn. Each one is copied with
# SQLite's online backup API into the stage dir and archived as
# <STAGE_REL>/_sqlite/<path relative to $HOME>; restore moves them back into place.
# WEEKLY_ONLY dbs (big, rebuildable indexes) are refreshed on fulls only.
#
# Config: ${HOME_BACKUP_CONF:-~/.config/home-backup/config} (see config.example).
set -euo pipefail
umask 077  # archives hold keys and private data

CONF=${HOME_BACKUP_CONF:-$HOME/.config/home-backup/config}
[[ -r $CONF ]] || { echo "home-backup: no config at $CONF (copy tools/backup/config.example)" >&2; exit 2; }
SOURCES=(); EXCLUDES=(); WEEKLY_ONLY=(); SYSTEM_FILES=()
ARCHIVE_DIR="" REMOTE="" KEEP_LOCAL=2 KEEP_REMOTE=8 FULL_DOW=7 ZSTD_LEVEL=10 NTFY_SETTINGS=""
# shellcheck source=/dev/null
source "$CONF"
[[ -n $ARCHIVE_DIR ]] || { echo "home-backup: ARCHIVE_DIR unset in $CONF" >&2; exit 2; }
CYCLES=$ARCHIVE_DIR/cycles
# Staged db copies must live under $HOME: tar --listed-incremental allows only one -C.
STAGE_REL=${STAGE_REL:-.local/state/home-backup/stage}
STAGE=$HOME/$STAGE_REL

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }

ntfy() {  # ntfy TITLE PRIORITY TAG MESSAGE — phone push via LangBang's ntfy settings
  [[ -n $NTFY_SETTINGS && -r $NTFY_SETTINGS ]] || return 0
  python3 - "$NTFY_SETTINGS" "$@" <<'PY' || true
import json, sys, urllib.request
n = json.load(open(sys.argv[1])).get("notify", {})
title, prio, tag, msg = sys.argv[2:6]
if n.get("ntfy_enabled") and n.get("ntfy_topic"):
    req = urllib.request.Request(f'{n["ntfy_server"].rstrip("/")}/{n["ntfy_topic"]}',
        data=msg.encode(), headers={"Title": title, "Priority": prio, "Tags": tag})
    urllib.request.urlopen(req, timeout=10)
PY
}
# Pushes only on failure, plus one "OK again" on the first success after a
# failure, so a stale FAILED alert on the phone is visibly superseded.
notify_fail() { touch "$ARCHIVE_DIR/.last_failed"; ntfy "Backup FAILED" high warning "$1"; }
notify_recovered() {
  [[ -f $ARCHIVE_DIR/.last_failed ]] || return 0
  rm -f "$ARCHIVE_DIR/.last_failed"; ntfy "Backup OK again" default white_check_mark "$1"
}

remote_host() { echo "${REMOTE%%:*}"; }
remote_path() { echo "${REMOTE#*:}"; }

cycle_dirs() { local d; for d in "$1"/*/; do [[ -d $d ]] && echo "${d%/}"; done; return 0; }  # glob order = sorted
latest_cycle() { cycle_dirs "$CYCLES" | tail -n1; }

prune() {  # prune DIR KEEP — drop all but the newest KEEP cycle dirs
  local dir=$1 keep=$2
  cycle_dirs "$dir" | head -n "-$keep" | while read -r d; do
    log "prune $d"; rm -rf -- "$d"
  done
}

stage_sqlite() {  # stage_sqlite LEVEL — online-backup every live db into $STAGE/_sqlite
  local level=$1 wal db rel dst w skip; mkdir -p "$STAGE/_sqlite"
  while IFS= read -r -d '' wal; do
    db=${wal%-wal}; [[ -f $db ]] || continue
    rel=${db#"$HOME"/}; dst=$STAGE/_sqlite/$rel
    skip=0; for w in "${WEEKLY_ONLY[@]}"; do [[ $rel == "$w" ]] && skip=1; done
    if [[ $level == incr && $skip == 1 && -f $dst ]]; then continue; fi
    mkdir -p "$(dirname "$dst")"
    python3 - "$db" "$dst.part" <<'PY'
import sqlite3, sys
src = sqlite3.connect(f"file:{sys.argv[1]}?mode=ro", uri=True, timeout=60)
dst = sqlite3.connect(sys.argv[2])
src.backup(dst)
assert dst.execute("PRAGMA quick_check").fetchone()[0] == "ok", "quick_check failed"
dst.close(); src.close()
PY
    mv -f "$dst.part" "$dst"
    SQLITE_LIVE+=("$rel")
  done < <(cd "$HOME" && for s in "${SOURCES[@]}"; do [[ -e $s ]] && find "$HOME/$s" -name '*.db-wal' -not -path '*/node_modules/*' -print0; done)
}

do_run() {
  local force_full=${1:-}
  mkdir -p "$CYCLES" "$STAGE"; chmod 700 "$ARCHIVE_DIR" "$HOME/${STAGE_REL%/stage}"
  exec 9>"$ARCHIVE_DIR/.lock"
  flock -n 9 || { log "another run holds the lock"; exit 1; }
  local on_err='log "FAILED at line $LINENO"; notify_fail "home-backup failed at line $LINENO on $(hostname)"'
  trap "$on_err" ERR

  local cur level stamp; cur=$(latest_cycle); stamp=$(date +%Y%m%d-%H%M%S)
  if [[ $force_full == --full || -z $cur || ! -f $cur/state.snar || ! -d $STAGE/_sqlite ]] \
     || { [[ $(date +%u) == "$FULL_DOW" ]] && [[ $(basename "$cur") != $(date +%Y%m%d)-* ]]; }; then
    level=full; cur=$CYCLES/$stamp; mkdir -p "$cur"
    rm -rf "$STAGE"; mkdir -p "$STAGE"
  else
    level=incr
  fi
  log "start $level → $(basename "$cur")"

  SQLITE_LIVE=()
  stage_sqlite "$level"
  # system files (unit files etc.) — rsync -a keeps mtimes so unchanged ones aren't re-archived
  mkdir -p "$STAGE/_system"
  if ((${#SYSTEM_FILES[@]})); then rsync -a --relative --delete "${SYSTEM_FILES[@]}" "$STAGE/_system/" 2>/dev/null || true; fi

  local srcs=() s; for s in "${SOURCES[@]}"; do
    if [[ -e $HOME/$s ]]; then srcs+=("$s"); else log "skip missing source ~/$s"; fi
  done
  local ex=(--no-anchored) e; for e in "${EXCLUDES[@]}"; do ex+=(--exclude="$e"); done
  # every live db anywhere in SOURCES (WEEKLY_ONLY included) is excluded — its staged copy stands in.
  # Anchored, or the pattern would also match (and drop) that copy under _sqlite/.
  ex+=(--anchored)
  while IFS= read -r -d '' e; do e=${e#"$HOME"/}; ex+=(--exclude="$e" --exclude="$e-wal" --exclude="$e-shm" --exclude="$e-journal")
  done < <(for s in "${srcs[@]}"; do find "$HOME/$s" -name '*.db-wal' -not -path '*/node_modules/*' -print0 | sed -z 's/-wal$//'; done)

  local out=$cur/$stamp-$level.tar.zst snar_tmp=$cur/.state.snar.part
  if [[ $level == incr ]]; then cp -f "$cur/state.snar" "$snar_tmp"; else rm -f "$snar_tmp"; fi
  set +e; trap - ERR
  tar --create --file=- --listed-incremental="$snar_tmp" --ignore-failed-read \
      --warning=no-file-changed --warning=no-file-removed --warning=no-file-shrank \
      "${ex[@]}" -C "$HOME" "${srcs[@]}" "$STAGE_REL" \
    | zstd -q -T0 "-$ZSTD_LEVEL" -o "$out.part"
  local rc=("${PIPESTATUS[@]}")
  set -e; trap "$on_err" ERR
  # tar exit 1 = "some files changed while reading" — expected on a live home dir
  if (( rc[0] > 1 || rc[1] != 0 )); then
    rm -f "$out.part" "$snar_tmp"; [[ $level == full ]] && rm -rf "$cur"
    log "FAILED tar=${rc[0]} zstd=${rc[1]}"; notify_fail "tar=${rc[0]} zstd=${rc[1]} on $(hostname) ($level)"; exit 1
  fi
  zstd -q -t "$out.part"
  mv -f "$out.part" "$out"; mv -f "$snar_tmp" "$cur/state.snar"
  (cd "$cur" && sha256sum "$(basename "$out")" > "$(basename "$out").sha256")
  log "wrote $(basename "$out") $(du -h "$out" | cut -f1) (sqlite: ${#SQLITE_LIVE[@]} dbs)"

  prune "$CYCLES" "$KEEP_LOCAL"

  if [[ -n $REMOTE ]]; then
    local rh rp; rh=$(remote_host); rp=$(remote_path)
    ssh -o BatchMode=yes "$rh" "mkdir -p '$rp/cycles' && chmod 700 '$rp'"
    rsync -a --partial --chmod=D700,F600 -e "ssh -o BatchMode=yes" "$CYCLES/" "$REMOTE/cycles/"
    ssh -o BatchMode=yes "$rh" "cd '$rp/cycles/$(basename "$cur")' && sha256sum -c --quiet '$(basename "$out").sha256'" \
      || { log "FAILED remote checksum"; notify_fail "remote checksum mismatch for $(basename "$out")"; exit 1; }
    ssh -o BatchMode=yes "$rh" "cd '$rp/cycles' && ls -1d */ | sort | head -n -$KEEP_REMOTE | xargs -r rm -rf --"
    log "synced → $rh (verified)"
  fi
  notify_recovered "$(basename "$out") written$([[ -n $REMOTE ]] && echo " and synced to $(remote_host)")"
  log "done"
}

do_list() {
  for c in $(cycle_dirs "$CYCLES"); do
    echo "local  $(basename "$c")"; (cd "$c" && ls -1sh -- *.tar.zst 2>/dev/null | sed 's/^/         /')
  done
  if [[ -n $REMOTE ]]; then
    ssh -o BatchMode=yes "$(remote_host)" "cd '$(remote_path)/cycles' 2>/dev/null && for c in \$(ls -1d */ | sort); do echo \"remote \${c%/}\"; (cd \"\$c\" && ls -1sh -- *.tar.zst | sed 's/^/         /'); done"
  fi
}

do_restore() {
  local cycle=${1:?cycle} dest=${2:?dest} until=${3:-} dir a n
  dir=$CYCLES/$cycle; [[ -d $dir ]] || { echo "no local cycle $cycle (rsync it back from $REMOTE first)" >&2; exit 2; }
  mkdir -p "$dest"
  [[ -z $(ls -A "$dest") ]] || { echo "restore target $dest is not empty" >&2; exit 2; }
  for a in "$dir"/*.tar.zst; do
    n=$(basename "$a"); [[ -n $until && ${n:0:${#until}} > $until ]] && break
    log "extract $(basename "$a")"
    (cd "$dir" && sha256sum -c --quiet "$(basename "$a").sha256")
    zstd -q -dc "$a" | tar --extract --listed-incremental=/dev/null -C "$dest"
  done
  local st=$dest/$STAGE_REL
  if [[ -d $st/_sqlite ]]; then  # put consistent db copies back where they live
    (cd "$st/_sqlite" && find . -type f -print0) | while IFS= read -r -d '' f; do
      mkdir -p "$dest/$(dirname "$f")"; mv -f "$st/_sqlite/$f" "$dest/$f"
    done
    rm -rf "$st/_sqlite"
  fi
  log "restored $cycle into $dest (paths relative to \$HOME; system files under $STAGE_REL/_system/)"
}

case ${1:-run} in
  run) do_run "${2:-}" ;;
  list) do_list ;;
  restore) shift; do_restore "$@" ;;
  *) sed -n '2,12p' "$0"; exit 2 ;;
esac
