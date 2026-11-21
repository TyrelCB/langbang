#!/usr/bin/env python3
"""🔐 One-time setup for LangBang's secret vaults (see README → Secret vault).

Creates, never overwrites:
  data/vault/LangBang.kdbx       interactive vault — YOUR master password
  data/vault/LangBang-auto.kdbx  unattended vault for scheduled runs
both with groups Passwords/, Cards/, Identity/. Then seals the auto vault's
password to this box's TPM2 (sudo systemd-creds) and adds the
langbang.service drop-in that hands it to the service. Does NOT restart
LangBang (gate restarts on /api/runs + bench).

Run it in a real terminal (passwords are read with getpass, never echoed):
    .venv/bin/python tools/vault_init.py            # both vaults + TPM seal
    .venv/bin/python tools/vault_init.py --main-only
"""
import argparse
import getpass
import os
import secrets
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
VAULT_DIR = ROOT / "data" / "vault"
GROUPS = ("Passwords", "Cards", "Identity")
CRED = "/etc/credstore.encrypted/vault_auto"
DROPIN = "/etc/systemd/system/langbang.service.d/vault.conf"


def ask_pw(what: str, allow_generate: bool = False) -> str:
    hint = " (Enter = generate a random one)" if allow_generate else ""
    while True:
        pw = getpass.getpass(f"{what} password{hint}: ")
        if not pw and allow_generate:
            pw = secrets.token_urlsafe(24)
            print(f"\n  Generated {what} password (store it in your personal KeePassXC —"
                  " you need it to edit this vault; shown once):\n\n    " + pw + "\n")
            return pw
        if len(pw) < 12:
            print("  at least 12 characters, please")
            continue
        if getpass.getpass("  again: ") != pw:
            print("  didn't match")
            continue
        return pw


def create(path: Path, pw: str) -> None:
    from pykeepass import create_database
    if path.exists():
        sys.exit(f"{path} already exists — refusing to overwrite")
    kp = create_database(str(path), password=pw)
    for g in GROUPS:
        kp.add_group(kp.root_group, g)
    kp.save()
    os.chmod(path, 0o600)
    print(f"  created {path}  (groups: {', '.join(g + '/' for g in GROUPS)})")


def seal(pw: str) -> None:
    print("\nSealing the auto-vault password to the TPM (sudo)…")
    subprocess.run(["sudo", "mkdir", "-p", "-m", "700", os.path.dirname(CRED)], check=True)
    subprocess.run(["sudo", "systemd-creds", "encrypt", "--with-key=tpm2",
                    "--name=vault_auto", "-", CRED], input=pw.encode(), check=True)
    subprocess.run(["sudo", "mkdir", "-p", os.path.dirname(DROPIN)], check=True)
    subprocess.run(["sudo", "tee", DROPIN], check=True, stdout=subprocess.DEVNULL,
                   input=b"# LangBang unattended vault password (TPM2-sealed, see tools/vault_init.py)\n"
                         b"[Service]\nLoadCredentialEncrypted=vault_auto\n")
    subprocess.run(["sudo", "systemctl", "daemon-reload"], check=True)
    print(f"  {CRED} + {DROPIN} written, daemon reloaded")
    print("  takes effect on LangBang's next restart")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--main-only", action="store_true", help="skip the unattended vault")
    a = ap.parse_args()
    VAULT_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(VAULT_DIR, 0o700)
    main_p, auto_p = VAULT_DIR / "LangBang.kdbx", VAULT_DIR / "LangBang-auto.kdbx"
    if not main_p.exists():
        print("Interactive vault — the master password you'll type into 🔐 UNLOCK.")
        create(main_p, ask_pw("Master"))
    else:
        print(f"  {main_p} exists — kept")
    if a.main_only:
        return
    if auto_p.exists():
        print(f"  {auto_p} exists — kept (re-seal by hand if needed, see README)")
        return
    print("\nUnattended vault — only for credentials scheduled tasks may use.")
    pw = ask_pw("Auto-vault", allow_generate=True)
    create(auto_p, pw)
    seal(pw)
    print("\nDone. Add entries in KeePassXC (File → Open → data/vault/…).")


if __name__ == "__main__":
    main()
