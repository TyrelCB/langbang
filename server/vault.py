"""🔐 Secret vault — KeePass (KDBX) files the agent can USE but never SEE.

The model only ever handles opaque handles: {{secret:Group/Entry.field}}.
agent._SecretBroker swaps a handle for its value at tool-execution time (on a
copy of the call — checkpoints/trajectory keep the handle) and runs every
tool result through redact() before it is persisted or streamed.

Two vaults, both edited in KeePassXC and only ever READ here:
  data/vault/LangBang.kdbx       interactive: locked until the user unlocks
                                 it in the UI (master password kept in RAM
                                 only), auto-locks after idle minutes.
  data/vault/LangBang-auto.kdbx  unattended: opened at startup with a key
                                 sealed to this box's TPM2 (systemd-creds →
                                 $CREDENTIALS_DIRECTORY/vault_auto). Scheduled
                                 runs may use ONLY entries from here, and
                                 only those the task was granted in the UI.

Tier: top-level group Cards/ or Identity/ → "sensitive" (each use needs the
user's approval; never unattended unless the entry sets lb_unattended=true),
anything else → "login". A custom attribute lb_tier overrides the group.

Honest limit (README ⚠ Security): the agent shell is a child of this
process. While a vault is open a crafted command could read process memory.
The vault keeps secrets out of model context, logs, exports and backups; it
is not isolation from a hostile shell."""
from __future__ import annotations

import asyncio
import base64
import fnmatch
import logging
import os
import re
import threading
import time
import urllib.parse
from pathlib import Path

from . import config

logger = logging.getLogger("langbang.vault")

DATA = Path(__file__).resolve().parent.parent / "data"
VAULT_DIR = DATA / "vault"
MAIN_PATH = VAULT_DIR / "LangBang.kdbx"
AUTO_PATH = VAULT_DIR / "LangBang-auto.kdbx"
KEYFILE = DATA / "keys" / "vault.key"  # optional second factor (main vault)
AUTO_CRED = "vault_auto"  # systemd-creds name (LoadCredentialEncrypted=)

SENSITIVE_GROUPS = {"cards", "identity"}
STD_FIELDS = ("username", "password", "url", "notes")
MIN_REDACT = 4  # shorter values would redact ordinary text

# {{secret:Group/Entry.field}} — titles may contain dots, so the field is
# whatever follows the LAST dot.
HANDLE_RE = re.compile(r"\{\{\s*secret:([^{}]+?)\s*\}\}")


class VaultError(Exception):
    pass


def parse_handle(inner: str) -> tuple[str, str]:
    path, _, field = inner.strip().rpartition(".")
    if not path or not field:
        raise VaultError(f"bad secret handle {{{{secret:{inner}}}}} — use "
                         "{{secret:Group/Entry.field}} (see list_secrets)")
    return path.strip().strip("/"), field.strip()


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


class _Vault:
    def __init__(self, name: str, path: Path, auto: bool = False):
        self.name, self.path, self.auto = name, path, auto
        self._kp = None
        self._pw: str | None = None
        self._keyfile: str | None = None
        self._mtime = 0.0
        self._entries: dict[str, dict] = {}
        self.unlocked_at = 0.0
        self.last_used = 0.0
        self._lock = threading.RLock()  # _fresh → open re-enters

    # -- lifecycle --
    def open(self, password: str, keyfile: str | None = None) -> int:
        from pykeepass import PyKeePass  # heavy import, only when used
        if not self.path.exists():
            raise VaultError(f"no vault file at {self.path} — run tools/vault_init.py")
        try:
            kp = PyKeePass(str(self.path), password=password or None, keyfile=keyfile)
        except Exception as e:  # CredentialsError, HeaderChecksumError, …
            raise VaultError(f"could not open {self.path.name}: {type(e).__name__}") from None
        with self._lock:
            self._kp, self._pw, self._keyfile = kp, password, keyfile
            self._mtime = self.path.stat().st_mtime
            self._entries = self._index(kp)
            self.unlocked_at = self.last_used = time.time()
        logger.info("vault %s unlocked (%d entries)", self.name, len(self._entries))
        return len(self._entries)

    def close(self) -> None:
        with self._lock:
            was = self._kp is not None
            self._kp = self._pw = self._keyfile = None
            self._entries = {}
            self.unlocked_at = 0.0
        if was:
            logger.info("vault %s locked", self.name)

    @property
    def is_open(self) -> bool:
        return self._kp is not None

    def _fresh(self) -> None:
        """Re-read after an edit in KeePassXC (same credentials)."""
        try:
            m = self.path.stat().st_mtime
        except OSError:
            return
        if self._kp is not None and m != self._mtime:
            try:
                self.open(self._pw or "", self._keyfile)
                logger.info("vault %s reloaded after file change", self.name)
            except VaultError as e:  # master password changed in KeePassXC
                logger.warning("vault %s reload failed (%s) — locking", self.name, e)
                self.close()

    @staticmethod
    def _index(kp) -> dict[str, dict]:  # noqa: ANN001
        out: dict[str, dict] = {}
        for e in kp.entries:
            gpath = [g for g in ((e.group.path if e.group else None) or []) if g]
            if "Recycle Bin" in gpath:
                continue
            key = "/".join([*gpath, e.title or "untitled"])
            fields = {f: getattr(e, f) for f in STD_FIELDS if getattr(e, f)}
            attrs = dict(e.custom_properties or {})
            meta = {k: v for k, v in attrs.items() if k.startswith("lb_")}
            fields.update({k: v for k, v in attrs.items()
                           if not k.startswith("lb_") and v and not k.startswith("KP2A_")})
            top = gpath[0].lower() if gpath else ""
            tier = (meta.get("lb_tier") or "").strip().lower() or (
                "sensitive" if top in SENSITIVE_GROUPS else "login")
            out[key] = {
                "fields": fields,
                "tier": "sensitive" if tier == "sensitive" else "login",
                "unattended": str(meta.get("lb_unattended", "")).lower() in ("1", "true", "yes"),
                "tier_set": (meta.get("lb_tier") or "").strip().lower(),  # explicit override, if any
                "domain": _site(_host(fields.get("url", ""))),
            }
        return out

    # -- reads --
    def entries(self) -> dict[str, dict]:
        with self._lock:
            self._fresh()
            return dict(self._entries)


def _host(url: str) -> str:
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower().removeprefix("www.")
    except ValueError:
        return ""


def _site(host: str) -> str:
    """Bind to the site, not the exact login host: login.example.com → example.com
    (so the fill on www./accounts. still matches). IPs stay as-is."""
    if not host or re.fullmatch(r"[\d.]+|\[?[0-9a-f:]+\]?", host):
        return host
    return ".".join(host.split(".")[-2:])


MAIN = _Vault("main", MAIN_PATH)
AUTO = _Vault("auto", AUTO_PATH, auto=True)


def _idle_minutes() -> float:
    try:
        return float((config.load().get("vault") or {}).get("auto_lock_min", 15))
    except Exception:  # noqa: BLE001
        return 15.0


# ---- writes (LangBang manages the vault; KeePassXC is optional) ----
# Every save: re-read if KeePassXC changed the file meanwhile, back the old
# file up (data/vault/.bak, newest BAK_KEEP per vault), write a temp file and
# os.replace it in — a crash never leaves a half-written vault.
BAK_DIR = VAULT_DIR / ".bak"
BAK_KEEP = 30
UI_TOKENS: set[str] = set()  # issued at unlock, held by the browser only


def new_ui_token() -> str:
    import secrets
    t = secrets.token_urlsafe(32)
    UI_TOKENS.add(t)
    return t


def ui_token_ok(t: str | None) -> bool:
    import hmac
    return bool(t) and MAIN.is_open and any(hmac.compare_digest(t, x) for x in UI_TOKENS)


def _vault(name: str) -> _Vault:
    v = {"main": MAIN, "auto": AUTO}.get(name or "main")
    if v is None:
        raise VaultError(f"unknown vault {name!r} (main | auto)")
    if not v.is_open:
        raise VaultError(f"the {v.name} vault is not open")
    return v


def _split(path: str) -> tuple[list[str], str]:
    parts = [p.strip() for p in (path or "").strip().strip("/").split("/") if p.strip()]
    if not parts:
        raise VaultError("entry path is empty — use Group/Title")
    if any(p in (".", "..") or "{" in p or "}" in p for p in parts):
        raise VaultError(f"bad entry path {path!r}")
    return parts[:-1], parts[-1]


def _save(v: _Vault) -> None:
    import shutil
    BAK_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(BAK_DIR, 0o700)
    if v.path.exists():
        bak = BAK_DIR / f"{v.path.stem}-{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.kdbx"
        shutil.copy2(v.path, bak)
        os.chmod(bak, 0o600)
        olds = sorted(BAK_DIR.glob(v.path.stem + "-*.kdbx"))
        for o in olds[:-BAK_KEEP]:
            o.unlink(missing_ok=True)
    tmp = v.path.with_suffix(".kdbx.tmp")
    v._kp.save(filename=str(tmp))
    os.chmod(tmp, 0o600)
    os.replace(tmp, v.path)
    v._mtime = v.path.stat().st_mtime
    v._entries = v._index(v._kp)


def _find(v: _Vault, groups: list[str], title: str, create: bool = False):  # noqa: ANN202
    kp = v._kp
    g = kp.root_group
    for name in groups:
        nxt = next((c for c in g.subgroups if c.name == name), None)
        if nxt is None:
            if not create:
                return None, None
            nxt = kp.add_group(g, name)
        g = nxt
    e = next((x for x in g.entries if x.title == title), None)
    return g, e


def get_entry(vault: str, path: str) -> dict:
    """UI-only reveal (token-gated in main.py): every field WITH values."""
    v = _vault(vault)
    with v._lock:
        v._fresh()
        e = v._entries.get(path.strip().strip("/"))
    if e is None:
        raise VaultError(f"no entry {path!r} in the {vault} vault")
    return {"vault": v.name, "entry": path, **e}


def exists(vault: str, path: str) -> dict | None:
    v = _vault(vault)
    with v._lock:
        v._fresh()
        return v._entries.get(path.strip().strip("/"))


def put_entry(vault: str, path: str, fields: dict[str, str] | None = None,
              remove: list[str] | None = None, tier: str | None = None,
              unattended: bool | None = None, old_path: str | None = None) -> dict:
    """Create or update an entry; fields merge (empty string = leave as is is
    NOT assumed: an explicit "" clears the field). old_path renames/moves."""
    v = _vault(vault)
    groups, title = _split(path)
    with v._lock:
        v._fresh()
        kp = v._kp
        if old_path and old_path.strip("/") != path.strip("/"):
            og, oe = _find(v, *_split(old_path))
            if oe is None:
                raise VaultError(f"no entry {old_path!r} to rename")
            if _find(v, groups, title)[1] is not None:
                raise VaultError(f"{path!r} already exists")
            g, _ = _find(v, groups, title, create=True)
            kp.move_entry(oe, g)
            oe.title = title
        g, e = _find(v, groups, title, create=True)
        if e is None:
            e = kp.add_entry(g, title, "", "")
        for k, val in (fields or {}).items():
            k = str(k).strip()
            if not k or k.startswith("lb_"):
                continue
            val = "" if val is None else str(val)
            if k in STD_FIELDS:
                setattr(e, k, val or None)
            elif val:
                e.set_custom_property(k, val, protect=True)
            elif k in (e.custom_properties or {}):
                e.delete_custom_property(k)
        for k in remove or []:
            if k in STD_FIELDS:
                setattr(e, k, None)
            elif k in (e.custom_properties or {}):
                e.delete_custom_property(k)
        if tier is not None:
            if tier in ("", "auto"):
                if "lb_tier" in (e.custom_properties or {}):
                    e.delete_custom_property("lb_tier")
            else:
                e.set_custom_property("lb_tier", "sensitive" if tier == "sensitive" else "login")
        if unattended is not None:
            if unattended:
                e.set_custom_property("lb_unattended", "true")
            elif "lb_unattended" in (e.custom_properties or {}):
                e.delete_custom_property("lb_unattended")
        e.touch(modify=True)
        _save(v)
        key = "/".join([*groups, title])
        logger.info("vault %s: saved %s", v.name, key)
        return {"vault": v.name, "entry": key, **{k: x for k, x in v._entries[key].items() if k != "fields"},
                "fields": sorted(v._entries[key]["fields"])}


def delete_entry(vault: str, path: str, field: str = "") -> None:
    v = _vault(vault)
    groups, title = _split(path)
    with v._lock:
        v._fresh()
        _, e = _find(v, groups, title)
        if e is None:
            raise VaultError(f"no entry {path!r} in the {vault} vault")
        if field:
            if field in STD_FIELDS:
                setattr(e, field, None)
            elif field in (e.custom_properties or {}):
                e.delete_custom_property(field)
            else:
                raise VaultError(f"{path} has no field {field!r}")
        else:
            v._kp.delete_entry(e)  # backup in .bak is the undo
        _save(v)
        logger.info("vault %s: deleted %s%s", v.name, path, "." + field if field else "")


def generate(length: int = 24, symbols: bool = True) -> str:
    import secrets
    import string
    length = max(8, min(int(length or 24), 128))
    alpha = string.ascii_letters + string.digits + ("!#%+-._~@^*" if symbols else "")
    while True:  # at least one of each class
        pw = "".join(secrets.choice(alpha) for _ in range(length))
        if (any(c.islower() for c in pw) and any(c.isupper() for c in pw)
                and any(c.isdigit() for c in pw)
                and (not symbols or any(not c.isalnum() for c in pw))):
            return pw


# ---- public API ----

def unlock(password: str) -> int:
    kf = str(KEYFILE) if KEYFILE.exists() else None
    return MAIN.open(password, kf)


def lock() -> None:
    MAIN.close()
    UI_TOKENS.clear()


def touch() -> None:
    MAIN.last_used = time.time()


def status() -> dict:
    _expire()
    idle = _idle_minutes()
    left = max(0, int(MAIN.last_used + idle * 60 - time.time())) if MAIN.is_open and idle > 0 else None
    return {
        "exists": MAIN_PATH.exists(),
        "locked": not MAIN.is_open,
        "entries": len(MAIN.entries()) if MAIN.is_open else 0,
        "expires_in": left,
        "auto_lock_min": idle,
        "keyfile": KEYFILE.exists(),
        "auto": {"exists": AUTO_PATH.exists(), "open": AUTO.is_open,
                 "entries": len(AUTO.entries()) if AUTO.is_open else 0},
        "path": str(MAIN_PATH),
    }


def _expire() -> None:
    idle = _idle_minutes()
    if MAIN.is_open and idle > 0 and time.time() - MAIN.last_used > idle * 60:
        logger.info("vault idle %.0f min — auto-lock", idle)
        MAIN.close()
        UI_TOKENS.clear()


def open_auto() -> bool:
    """Startup: open the unattended vault with the TPM-sealed credential
    systemd hands the service. Silent no-op when either piece is missing."""
    cdir = os.environ.get("CREDENTIALS_DIRECTORY")
    if not AUTO_PATH.exists() or not cdir:
        return False
    cred = Path(cdir) / AUTO_CRED
    try:
        pw = cred.read_text().rstrip("\n")
    except OSError:
        return False
    try:
        n = AUTO.open(pw)
        logger.info("unattended vault open (%d entries)", n)
        return True
    except VaultError as e:
        logger.warning("unattended vault: %s", e)
        return False


async def loop() -> None:
    while True:
        await asyncio.sleep(30)
        try:
            _expire()
        except Exception:  # noqa: BLE001
            logger.exception("vault expiry tick failed")


def catalog(query: str = "", unattended: bool = False) -> list[dict]:
    """Entries as the model may see them — names, fields, tier, domain,
    card last-4. NEVER values."""
    _expire()
    out, seen = [], set()
    srcs = [AUTO] if unattended else [MAIN, AUTO]
    q = (query or "").lower()
    for v in srcs:
        if not v.is_open:
            continue
        for key, e in sorted(v.entries().items()):
            if key in seen or (q and q not in key.lower()):
                continue
            seen.add(key)
            d = {"entry": key, "fields": sorted(e["fields"]), "tier": e["tier"],
                 "vault": v.name}
            if e["domain"]:
                d["domain"] = e["domain"]
            num = _digits(e["fields"].get("number", ""))
            if len(num) >= 12:
                d["last4"] = num[-4:]
            if v.auto:
                d["unattended_ok"] = e["tier"] == "login" or e["unattended"]
            out.append(d)
    return out


def lookup(inner: str, unattended: bool = False, grants: list[str] | None = None) -> dict:
    """Resolve one handle → {value, entry, field, tier, domain, vault}.
    Raises VaultError with a model-readable reason."""
    _expire()
    path, field = parse_handle(inner)
    if unattended:
        if not AUTO.is_open:
            raise VaultError("no unattended vault is open (data/vault/LangBang-auto.kdbx "
                             "+ TPM credential) — scheduled runs can't use secrets")
        if not any(fnmatch.fnmatchcase(f"{path}.{field}", g) or fnmatch.fnmatchcase(path, g)
                   for g in (grants or [])):
            raise VaultError(f"this scheduled task has no grant for {path} — the user "
                             "adds grants on the task (🔑 in SCHEDULES)")
        srcs = [AUTO]
    else:
        srcs = [v for v in (MAIN, AUTO) if v.is_open]
        if not srcs:
            raise VaultError("the vault is locked — ask the user to unlock it "
                             "(🔐 VAULT in settings), then retry")
    for v in srcs:
        e = v.entries().get(path)
        if e is None:
            continue
        if field not in e["fields"]:
            raise VaultError(f"{path} has no field {field!r} (has: {', '.join(sorted(e['fields']))})")
        if unattended and e["tier"] == "sensitive" and not e["unattended"]:
            raise VaultError(f"{path} is sensitive and not marked lb_unattended — "
                             "it can't be used without the user present")
        if v is MAIN:
            touch()
        return {"value": e["fields"][field], "entry": path, "field": field,
                "tier": e["tier"], "domain": e["domain"], "vault": v.name}
    raise VaultError(f"no vault entry {path!r} — call list_secrets to see what exists")


def _labels() -> list[tuple[str, str]]:
    out = []
    for v in (MAIN, AUTO):
        if not v.is_open:
            continue
        for key, e in v.entries().items():
            for f, val in e["fields"].items():
                if f in ("url", "notes", "username") or not isinstance(val, str):
                    # urls/usernames aren't secret (and "tyrel" would redact
                    # every /home/tyrel path); notes are free text
                    continue
                if len(val) >= MIN_REDACT:
                    out.append((val, f"{key}.{f}"))
    return out


def redact(text: str) -> str:
    """Scrub every open-vault value (plus url-encoded / base64 / digit-spaced
    card forms) from text. Cheap no-op when no vault is open."""
    if not text or not (MAIN.is_open or AUTO.is_open):
        return text
    for val, label in sorted(_labels(), key=lambda p: -len(p[0])):
        mark = f"‹secret:{label}›"
        forms = {val, urllib.parse.quote(val, safe=""), urllib.parse.quote_plus(val),
                 base64.b64encode(val.encode()).decode().rstrip("=")}
        for f in sorted(forms, key=len, reverse=True):
            if len(f) >= MIN_REDACT and f in text:
                text = text.replace(f, mark)
        d = _digits(val)
        if len(d) >= 12 and len(d) == len(val.replace(" ", "").replace("-", "")):
            # 4111 1111 1111 1111 / 4111-1111-… / 4111111111111111
            pat = r"(?<!\d)" + r"[ -]?".join(map(re.escape, d)) + r"(?!\d)"
            text = re.sub(pat, mark, text)
    return text


def redact_obj(o):  # noqa: ANN001, ANN201
    """redact() through str / list / dict content (tool content blocks)."""
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        return [redact_obj(x) for x in o]
    if isinstance(o, dict):
        return {k: redact_obj(v) for k, v in o.items()}
    return o


def host_of(url: str) -> str:
    return _host(url)
