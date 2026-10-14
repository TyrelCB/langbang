# Backups

`tools/backup/home-backup.sh` makes nightly archives of the data that matters,
LangBang's `data/` included, and copies them off-box. It doesn't depend on the
filesystem: GNU tar `--listed-incremental` + zstd + rsync over ssh, with no
btrfs/LVM snapshots.

## How it works

- **Cycles.** A cycle is one full (level 0) archive followed by chained daily
  incrementals. A new cycle starts each `FULL_DOW` (default Sunday), or when
  there is no usable cycle yet. Restoring a point in time means extracting the
  cycle's full archive and then each incremental, in order, up to that day.
  tar's dumpdir records replay deletions and renames too.
- **Live SQLite.** Every `*.db` with a `-wal` sibling under `SOURCES` is
  copied with SQLite's online backup API, checked with `quick_check`, and
  archived under `.local/state/home-backup/stage/_sqlite/`. The live
  db/-wal/-shm files are excluded, because a db copied mid-write is torn.
  `WEEKLY_ONLY` dbs, such as the RAG index (big and rebuildable), are refreshed
  on full runs only.
- **Integrity.** Each archive is checked with `zstd -t`, then hashed with
  sha256. After the rsync, the hash is checked again on the remote.
- **Retention.** Keeps `KEEP_LOCAL` cycles locally and `KEEP_REMOTE` cycles
  on the remote, pruned by cycle.
- **Failures** go to the journal (`journalctl -u home-backup`) and push to your
  phone via LangBang's ntfy topic (`NTFY_SETTINGS`).

## Setup

```sh
mkdir -p ~/.config/home-backup
cp tools/backup/config.example ~/.config/home-backup/config   # edit ARCHIVE_DIR, REMOTE, SOURCES
sudo cp tools/systemd/home-backup.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now home-backup.timer
sudo systemctl start home-backup     # first full now
```

The unit runs with `SELinuxContext=unconfined_t`. Under the default service
domain, executing `rsync` transitions into the confined `rsync_t`, and the push
fails with `Failed to exec ssh: Permission denied`.

`REMOTE` needs key-based ssh that works without an agent, because the timer
runs outside your login session. Archives contain whatever `SOURCES` holds,
including `~/.ssh` and `data/keys/` if listed. They are not encrypted, so
point `REMOTE` at a machine you trust. The remote directory is created 0700.

## Restore

```sh
tools/backup/home-backup.sh list                       # cycles, local + remote
# if the cycle only exists remotely:
rsync -a host:backups/home/cycles/<cycle> "$ARCHIVE_DIR/cycles/"
tools/backup/home-backup.sh restore <cycle> /tmp/restore            # latest state in that cycle
tools/backup/home-backup.sh restore <cycle> /tmp/restore 20261016   # up to that day
```

Restored paths are relative to `$HOME`, and the db copies are moved back to
where they live. Copy back what you need, and stop the owning service (e.g.
`langbang`) before replacing a live db.

To grab one file from one night without a full replay:
`zstd -dc <archive> | tar -x -C /tmp/x path/relative/to/home`. A file that
didn't change that night is in an earlier archive of the cycle.
