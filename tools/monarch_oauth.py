#!/usr/bin/env python3
"""One-shot OAuth 2.1 (PKCE) login for the Monarch MCP endpoint.

Registers a client (RFC 7591 DCR), prints an authorization URL, listens on
http://localhost:8791/callback for the code, exchanges it for tokens, and
writes them to ~/.langbang/monarch_oauth.json.

Monarch's DCR only accepts localhost/127.0.0.1 redirect URIs, so the browser
must run on this host, or pass the redirect URL back:
  --paste "http://localhost:8791/callback?code=...&state=..."
"""
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

AUTH_SERVER = "https://api.monarch.com"
AUTHZ = AUTH_SERVER + "/oauth/authorize/"
TOKEN_URL = AUTH_SERVER + "/oauth/token/"
REGISTER = AUTH_SERVER + "/oauth/register"
RESOURCE = "https://api.monarch.com/mcp"
REDIRECT = "http://localhost:8791/callback"
OUT = Path(os.path.expanduser("~/.langbang/monarch_oauth.json"))
SCOPES = "mcp:read mcp:write"
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")

result = {}


def parse_qs(path):
    return urllib.parse.parse_qs(urllib.parse.urlparse(path).query)


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        result.update({k: v[0] for k, v in parse_qs(self.path).items()})
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<h2>LangBang x Monarch: authorization received. "
                         b"You can close this tab.</h2>")

    def log_message(self, *a):
        pass


def post(url, data):
    body = urllib.parse.urlencode(data).encode()
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded",
                 "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def finish(ctx):
    if "error" in result:
        sys.exit("authz error: %s" % result)
    tok = post(TOKEN_URL, {
        "grant_type": "authorization_code", "code": result["code"],
        "redirect_uri": REDIRECT, "client_id": ctx["client_id"],
        "code_verifier": ctx["verifier"], "resource": RESOURCE,
    })
    tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 60
    tok["client_id"] = ctx["client_id"]
    tok["token_endpoint"] = TOKEN_URL
    tok["redirect_uri"] = REDIRECT
    OUT.write_text(json.dumps(tok, indent=2))
    OUT.chmod(0o600)
    print("TOKEN_SAVED scope=%s expires_in=%s" % (tok.get("scope"), tok.get("expires_in")))


def main():
    global result
    if "--paste" in sys.argv:
        result = {k: v[0] for k, v in parse_qs(sys.argv[sys.argv.index("--paste") + 1]).items()}
        ctx = json.loads(Path("/tmp/monarch_oauth_state.json").read_text())
        finish(ctx)
        return

    reg = json.load(urllib.request.urlopen(
        urllib.request.Request(
            REGISTER,
            data=json.dumps({
                "client_name": "LangBang",
                "redirect_uris": [REDIRECT],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            }).encode(),
            headers={"Content-Type": "application/json", "User-Agent": UA}),
        timeout=30))
    client_id = reg["client_id"]

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(24)

    params = {
        "response_type": "code", "client_id": client_id,
        "redirect_uri": REDIRECT, "scope": SCOPES, "state": state,
        "code_challenge": challenge, "code_challenge_method": "S256",
        "resource": RESOURCE,
    }
    url = AUTHZ + "?" + urllib.parse.urlencode(params)
    print("AUTH_URL " + url, flush=True)
    Path("/tmp/monarch_oauth_state.json").write_text(json.dumps(
        {"verifier": verifier, "state": state, "client_id": client_id}))

    srv = http.server.HTTPServer(("127.0.0.1", 8791), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    deadline = time.time() + 600
    while time.time() < deadline and not result:
        time.sleep(0.5)
    srv.shutdown()
    if not result:
        sys.exit("no callback received within 10 min")
    if result.get("state") != state:
        sys.exit("state mismatch: %s" % result)
    finish({"verifier": verifier, "client_id": client_id})


if __name__ == "__main__":
    main()
