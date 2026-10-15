"""Home backup (tools/backup/home-backup.sh) for the ⏰ SCHEDULES card.

LangBang shows and steers the backup; it does NOT run it. The job is a systemd
USER unit (home-backup.service/.timer, lingering on) so it keeps backing up
while LangBang is down or wedged — the one job that must survive the thing it
protects. This module only reads the script's status file, the archive dir and
the timer, and asks the user manager to start/stop/reschedule.

Config stays in ~/.config/home-backup/config (one writer: you, in an editor);
it is read here by sourcing it in bash exactly as the script does, and shown
read-only.
"""
import json
import os
import re
import shutil
import subprocess
import time

UNIT = "home-backup"
HOME = os.path.expanduser("~")
CONF = os.environ.get("HOME_BACKUP_CONF") or os.path.join(HOME, ".config/home-backup/config")
STATE_DIR = os.path.join(HOME, ".local/state/home-backup")
STATUS = os.path.join(STATE_DIR, "status.json")
DROPIN = os.path.join(HOME, ".config/systemd/user", UNIT + ".timer.d", "schedule.conf")
SCRIPT = "tools/backup/home-backup.sh"


def _env():
    # LangBang is a SYSTEM unit (User=tyrel): no session env, so point
    # systemctl/journalctl at this user's manager explicitly.
    run = f"/run/user/{os.getuid()}"
    return {**os.environ, "XDG_RUNTIME_DIR": run, "DBUS_SESSION_BUS_ADDRESS": f"unix:path={run}/bus"}


def _ctl(*args, timeout=15):
    r = subprocess.run(["systemctl", "--user", *args], env=_env(), capture_output=True,
                       text=True, timeout=timeout)
    if r.returncode:
        raise RuntimeError((r.stderr or r.stdout).strip() or f"systemctl {args[0]} failed")
    return r.stdout


def _show(unit, *props):
    out = _ctl("show", unit, *[f"-p{p}" for p in props])
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def config():
    """The script's config, sourced the way the script sources it."""
    if not os.path.isfile(CONF):
        return {"path": CONF, "missing": True}
    dump = ('source "$1" >/dev/null 2>&1 || exit 3; '
            'for n in SOURCES EXCLUDES WEEKLY_ONLY; do declare -n a=$n; printf "%s\\0" "@$n" "${a[@]}"; unset -n a; done; '
            'for n in ARCHIVE_DIR REMOTE KEEP_LOCAL KEEP_REMOTE FULL_DOW ZSTD_LEVEL; do printf "%s\\0%s\\0" "=$n" "${!n}"; done')
    r = subprocess.run(["bash", "-c", dump, "conf", CONF], capture_output=True, timeout=10)
    if r.returncode:
        return {"path": CONF, "error": "config did not source cleanly"}
    out, cur, toks = {"path": CONF}, None, r.stdout.decode(errors="replace").split("\0")
    it = iter(toks)
    for t in it:
        if t.startswith("@"):
            cur = out.setdefault(t[1:].lower(), [])
        elif t.startswith("=") and t[1:].isupper():
            out[t[1:].lower()] = next(it, "")
            cur = None
        elif cur is not None and t:
            cur.append(t)
    return out


def _cycles(archive_dir):
    cy = os.path.join(archive_dir, "cycles")
    out = []
    try:
        names = sorted(n for n in os.listdir(cy) if os.path.isdir(os.path.join(cy, n)))
    except OSError:
        return out
    for n in names:
        d, arch = os.path.join(cy, n), []
        for f in sorted(os.listdir(d)):
            if f.endswith(".tar.zst"):
                st = os.stat(os.path.join(d, f))
                arch.append({"name": f, "size": st.st_size, "mtime": int(st.st_mtime),
                             "level": "full" if f.endswith("-full.tar.zst") else "incr"})
        out.append({"cycle": n, "archives": arch, "size": sum(a["size"] for a in arch),
                    "restore": f"{SCRIPT} restore {n} ~/restore-{n}"})
    return out


def schedule():
    t = _show(UNIT + ".timer", "UnitFileState", "ActiveState", "TimersCalendar")
    m = re.search(r"OnCalendar=\S+ (\d\d):(\d\d)", t.get("TimersCalendar", ""))
    # list-timers JSON: usec, with the RandomizedDelaySec already applied
    # (`show --timestamp=unix` leaves NextElapseUSecRealtime human-formatted)
    lt = next(iter(json.loads(_ctl("list-timers", "--all", UNIT + ".timer", "-o", "json") or "[]")), {})
    us = lambda v: int(v) // 1_000_000 if v else None
    return {"enabled": t.get("UnitFileState") == "enabled" and t.get("ActiveState") == "active",
            "at": f"{m.group(1)}:{m.group(2)}" if m else None,
            "next": us(lt.get("next")), "last": us(lt.get("last"))}


def status(log=False):
    cfg = config()
    try:
        st = json.load(open(STATUS))
    except (OSError, ValueError):
        st = {}
    try:
        svc = _show(UNIT + ".service", "ActiveState", "ExecMainStartTimestamp")
        sched, err = schedule(), None
    except (RuntimeError, OSError, subprocess.SubprocessError) as e:
        svc, sched, err = {}, None, f"user systemd unreachable: {e}"
    running = svc.get("ActiveState") in ("activating", "active", "deactivating")
    if st.get("state") == "running" and svc and not running:
        st["state"] = "interrupted"  # killed hard (reboot, OOM): the script never wrote an end state
    ad = cfg.get("archive_dir") or ""
    out = {"unit_error": err, "running": running, "status": st, "schedule": sched, "config": cfg,
           "full_next": os.path.exists(os.path.join(STATE_DIR, "full-next")),
           "failed_marker": bool(ad) and os.path.exists(os.path.join(ad, ".last_failed")),
           "cycles": _cycles(ad) if ad else []}
    if running and st.get("phase") == "archiving" and st.get("archive"):
        try:
            out["part_size"] = os.path.getsize(st["archive"] + ".part")
        except OSError:
            pass
    if ad:
        try:
            du = shutil.disk_usage(ad)
            out["disk"] = {"free": du.free, "total": du.total}
        except OSError:
            pass
    if log:
        r = subprocess.run(["journalctl", "--user", "-u", UNIT, "-n", "40", "-o", "cat", "--no-pager"],
                           env=_env(), capture_output=True, text=True, timeout=10)
        out["log"] = r.stdout.splitlines()[-40:]
    return out


def run(full=False):
    if _show(UNIT + ".service", "ActiveState").get("ActiveState") in ("activating", "active"):
        raise RuntimeError("a backup is already running")
    if full:
        os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
        open(os.path.join(STATE_DIR, "full-next"), "w").close()  # the script consumes it
    _ctl("start", "--no-block", UNIT + ".service")
    time.sleep(0.3)


def stop():
    _ctl("stop", "--no-block", UNIT + ".service")


def set_schedule(enabled: bool, at: str | None):
    """Move the nightly time with a timer drop-in; the unit file in git keeps
    the default. Disabling stops the timer; a run in progress carries on."""
    if at is not None:
        if not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", at):
            raise ValueError("time must be HH:MM")
        os.makedirs(os.path.dirname(DROPIN), exist_ok=True)
        with open(DROPIN + ".tmp", "w") as fh:
            fh.write("# written by LangBang's SCHEDULES card\n[Timer]\nOnCalendar=\n"
                     f"OnCalendar=*-*-* {at}:00\n")
        os.replace(DROPIN + ".tmp", DROPIN)
        _ctl("daemon-reload")
    if enabled:
        _ctl("enable", UNIT + ".timer")
        _ctl("restart", UNIT + ".timer")  # pick up a new OnCalendar now
    else:
        _ctl("disable", "--now", UNIT + ".timer")
    return schedule()
