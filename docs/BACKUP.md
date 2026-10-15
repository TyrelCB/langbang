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
- **Failures** go to the journal (`journalctl --user -u home-backup`) and push
  to your phone via LangBang's ntfy topic (`NTFY_SETTINGS`). The first good run
  after a failure pushes "OK again".

## Setup

```sh
mkdir -p ~/.config/home-backup
cp tools/backup/config.example ~/.config/home-backup/config   # edit ARCHIVE_DIR, REMOTE, SOURCES
sudo loginctl enable-linger $USER    # user units run while you're logged out
cp tools/systemd/home-backup.{service,timer} ~/.config/systemd/user/
systemctl --user daemon-reload && systemctl --user enable --now home-backup.timer
systemctl --user start home-backup   # first full now
```

These are **user** units, so LangBang can start, stop and reschedule them
without root. The user manager also runs unconfined, which keeps rsync → ssh
working. Under SELinux, a *system* unit executing `rsync` transitions into the
confined `rsync_t`, and the push fails with `Failed to exec ssh: Permission
denied`.

## In LangBang: ⏰ SCHEDULES → HOME BACKUP

A system card, next to the RAG rescan, shows the backup without running it:

- **Status:** the current run and its phase, the last good run (with whether
  the copy was verified on the remote), the next run, and free archive disk.
- **Controls:** the nightly on/off switch, the run time (a timer drop-in at
  `~/.config/systemd/user/home-backup.timer.d/schedule.conf`), ▶ RUN NOW,
  ⧉ FULL NOW and ■ STOP. A stop deletes only the half-written archive. An
  interrupted copy to the remote resumes on the next run.
- **Details:** the archives per cycle, with each cycle's restore command ready
  to copy; the config, read-only; and the journal.

The config file stays the only place sources and excludes are edited. The
script writes its run state to `~/.local/state/home-backup/status.json`.
Touching `full-next` in that directory makes the next run a full.

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
