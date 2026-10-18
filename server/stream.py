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
ZONE_ID = "ZONE_ID_REDACTED"       # tyrel.cloud
DOMAIN_SUFFIX = ".ai.tyrel.cloud"
CERT = "/etc/caddy/certs/ai.tyrel.cloud"  # wildcard *.ai.tyrel.cloud
# Shares resolve CNAME -> edge.example.com (AWS edge box, IaC-managed):
# its Caddy terminates TLS and reverse-proxies back to this box over an autossh
# reverse tunnel (systemd user unit autoshare-tunnel.service, :8443 -> :8338).
# CNAME (not A) so EC2 redeploys/region moves don't touch share records.
EDGE_CNAME = "edge.example.com."

_lock = threading.Lock()
_shares = {}   # slug -> {created, url}


def _discover(timeout=6):
    import pychromecast
    ccs, browser = pychromecast.get_chromecasts(timeout=timeout)
    out = []
    for cc in ccs:
        try:
            cc.wait(timeout=3)
            out.append({"name": cc.name, "model": getattr(cc.model_name, "model_name", "") or "",
                        "app": cc.app_display_name or ""})
        except Exception:
            out.append({"name": cc.name, "model": "", "app": ""})
    browser.stop_discovery()
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
        browser.stop_discovery()


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
        browser.stop_discovery()


def _r53_upsert(name: str):
    import boto3
    r53 = boto3.client("route53")
    r53.change_resource_record_sets(HostedZoneId=ZONE_ID, ChangeBatch={"Changes": [{
        "Action": "UPSERT", "ResourceRecordSet": {
            "Name": f"{name}{DOMAIN_SUFFIX}.", "Type": "CNAME", "TTL": 60,
            "ResourceRecords": [{"Value": EDGE_CNAME}]}}]})


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
        boto3.client("route53").change_resource_record_sets(HostedZoneId=ZONE_ID, ChangeBatch={"Changes": [{
            "Action": "DELETE", "ResourceRecordSet": {
                "Name": f"{slug}{DOMAIN_SUFFIX}.", "Type": "CNAME", "TTL": 60,
                "ResourceRecords": [{"Value": EDGE_CNAME}]}}]})
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
