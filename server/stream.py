"""WATCH backend: cast the current stream to house Chromecasts, and mint
public share links under *.ai.tyrel.cloud (Route53 + Caddy, wildcard cert).

Cast uses pychromecast mDNS discovery (LAN-only, so it runs here on the box).
Share creates a temp A record + Caddy snippet pointing at the local HLS proxy
(se-hls-proxy.py on :8338); Caddy reload goes through a narrow sudoers rule.
"""
import json
import subprocess
import threading
import time
import urllib.parse
import uuid

PROXY_PORT = 8338
CAST_TTL = 6 * 3600  # shares/devices entries expire after 6h

from . import config


def _zone_id():  # Route53 hosted zone of DOMAIN_SUFFIX (settings.json private.zone_id)
    return config.private("zone_id", "LANGBANG_ZONE_ID")


DOMAIN_SUFFIX = ".ai.tyrel.cloud"
CERT = "/etc/caddy/certs/ai.tyrel.cloud"  # wildcard *.ai.tyrel.cloud
# Shares are served by the AWS edge box (IaC socks-proxy host): its Caddy
# terminates TLS for *.ai.tyrel.cloud and reverse-proxies to se-edge-proxy.py,
# which pulls StreamEast DIRECTLY — the house is out of the data path.
# Records are plain A to the edge's CURRENT IP (resolved from its DNS name);
# bin/se-edge-control.sh re-converges them after any EC2 redeploy.
def _edge_ip():
    import subprocess
    edge = config.private("edge_dns", "LANGBANG_EDGE_DNS")
    if not edge:
        raise RuntimeError("share edge host not configured (settings.json private.edge_dns)")
    out = subprocess.run(["dig", "+short", edge, "@1.1.1.1"],
                         capture_output=True, text=True, timeout=10).stdout
    ips = [l for l in out.splitlines() if l and l[0].isdigit()]
    if not ips:
        raise RuntimeError(f"cannot resolve edge {edge}")
    return ips[-1]

_lock = threading.Lock()
_shares = {}   # slug -> {created, url}


def _close(ccs, browser):
    """Disconnect every Chromecast BEFORE stopping discovery. A connected
    cast's socket thread outlives stop_discovery() and, with zeroconf gone,
    retries in a tight loop logging a 25-line traceback each time
    (2026-10-18: 440k of them, an 11 GB /tmp/langbang.log in an hour)."""
    for cc in ccs:
        try:
            cc.disconnect(timeout=3)
        except Exception:
            pass
    browser.stop_discovery()


def _discover(timeout=6):
    import pychromecast
    ccs, browser = pychromecast.get_chromecasts(timeout=timeout)
    out = []
    try:
        for cc in ccs:
            try:
                cc.wait(timeout=3)
                out.append({"name": cc.name, "model": getattr(cc.model_name, "model_name", "") or "",
                            "app": cc.app_display_name or ""})
            except Exception:
                out.append({"name": cc.name, "model": "", "app": ""})
    finally:
        _close(ccs, browser)
    return out


def devices():
    return _discover()


def cast(name: str, url: str, title: str = "LangBang stream"):
    import pychromecast
    ccs, browser = pychromecast.get_chromecasts(timeout=8)
    try:
        targets = [c for c in ccs if c.name == name]
        if not targets:
            raise ValueError(f"no device named {name!r}; found: {[c.name for c in ccs]}")
        cc = targets[0]
        cc.wait(timeout=5)
        cc.quit_app()
        time.sleep(1)
        mc = cc.media_controller
        mc.play_media(url, "application/vnd.apple.mpegurl", title=title)
        for _ in range(15):
            time.sleep(1)
            mc.update_status()
            if mc.status.player_state == "PLAYING":
                return {"ok": True, "device": name, "state": "PLAYING"}
        return {"ok": True, "device": name, "state": mc.status.player_state}
    finally:
        _close(ccs, browser)


def stop_cast(name: str):
    import pychromecast
    ccs, browser = pychromecast.get_chromecasts(timeout=8)
    try:
        for cc in ccs:
            if cc.name == name:
                cc.wait(timeout=5)
                cc.quit_app()
                return {"ok": True, "device": name}
        raise ValueError(f"no device named {name!r}")
    finally:
        _close(ccs, browser)


def _r53_upsert(name: str):
    import boto3
    r53 = boto3.client("route53")
    r53.change_resource_record_sets(HostedZoneId=_zone_id(), ChangeBatch={"Changes": [{
        "Action": "UPSERT", "ResourceRecordSet": {
            "Name": f"{name}{DOMAIN_SUFFIX}.", "Type": "A", "TTL": 60,
            "ResourceRecords": [{"Value": _edge_ip()}]}}]})


def share():
    """Mint <slug>.ai.tyrel.cloud -> local proxy. Returns the public URL."""
    slug = "s" + uuid.uuid4().hex[:6]
    host = slug + DOMAIN_SUFFIX
    snippet = (
        f"{host} {{\n"
        f"\ttls {CERT}/fullchain.pem {CERT}/privkey.pem\n"
        f"\treverse_proxy 127.0.0.1:{PROXY_PORT} {{\n"
        f"\t\tflush_interval -1\n\t}}\n}}\n"
    )
    path = f"/tmp/{slug}.caddyfile"
    with open(path, "w") as f:
        f.write(snippet)
    _r53_upsert(slug)
    subprocess.run(["sudo", "-n", "cp", path, f"/etc/caddy/Caddyfile.d/{slug}.caddyfile"], check=True)
    subprocess.run(["sudo", "-n", "systemctl", "reload", "caddy"], check=True)
    url = f"https://{host}/"
    with _lock:
        _shares[slug] = {"created": time.time(), "url": url}
        _gc()
    return {"slug": slug, "url": url}


def unshare(slug: str):
    with _lock:
        rec = _shares.pop(slug, None)
    if not rec:
        raise ValueError(f"unknown share {slug!r}")
    subprocess.run(["sudo", "-n", "rm", "-f", f"/etc/caddy/Caddyfile.d/{slug}.caddyfile"])
    subprocess.run(["sudo", "-n", "systemctl", "reload", "caddy"])
    try:
        import boto3
        boto3.client("route53").change_resource_record_sets(HostedZoneId=_zone_id(), ChangeBatch={"Changes": [{
            "Action": "DELETE", "ResourceRecordSet": {
                "Name": f"{slug}{DOMAIN_SUFFIX}.", "Type": "A", "TTL": 60,
                "ResourceRecords": [{"Value": _edge_ip()}]}}]})
    except Exception:
        pass  # Caddy already gone; a stale record is harmless
    return {"ok": True}


def shares():
    with _lock:
        _gc()
        return list(_shares.values())


def _gc():
    now = time.time()
    for slug in [s for s, r in _shares.items() if now - r["created"] > CAST_TTL]:
        threading.Thread(target=unshare, args=(slug,), daemon=True).start()
