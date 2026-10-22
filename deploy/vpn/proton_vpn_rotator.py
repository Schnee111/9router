#!/usr/bin/env python3
"""
WireGuard ProtonVPN Rotation Manager & Watchdog
Rotates protonvpn-proxy container across available .conf files when OpenCode Zen hits
FreeUsageLimitError, or when the egress tunnel is unhealthy.

Hardened & Fast-Fail Edition (2026-10-22):
  * DNS Hardening: forces 1.1.1.1:53 and 8.8.8.8:53 public plaintext resolvers,
    completely bypassing internal Proton 10.2.0.1 which triggers 503 Service Unavailable.
  * Fast-Fail Startup: drops container IP detection loop from 25s down to 8s.
  * Aggressive Reachability: probe_reachable timeout reduced to 5s.
"""

import glob
import json
import os
import random
import re
import subprocess
import sys
import time

CONFIG_DIR = "/home/ubuntu/vpn-configs"
STATE_FILE = "/home/ubuntu/vpn-configs/current_state.json"
EGRESS_STATUS_FILE = "/home/ubuntu/vpn-configs/egress_status.json"
EGRESS_STALE_SECONDS = 24 * 3600
CONTAINER_NAME = "protonvpn-proxy"
NETWORK_NAME = "9router_default"
LOCAL_PROXY_PORT = "28888"
OPENCODE_DB = "/var/lib/docker/volumes/9router-data/_data/db/data.sqlite"
BASE6444 = re.compile(r"[A-Za-z0-9+/]{43}=")

PROBE_UA = "opencode/latest/2.0.29/cli"
BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

def make_probe_session():
    now = int(time.time() * 1000)
    current = (now * 0x1000 + 1)
    val = (~current) & 0xffffffffffff
    time_hex = f"{val:012x}"
    rand_part = "".join(random.choice(BASE62) for _ in range(14))
    return f"ses_{time_hex}{rand_part}"

PROBE_TOOLS = [
    {"type": "function", "function": {"name": "bash", "description": "Run commands in a bash shell", "parameters": {"type": "object", "properties": {"command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "glob", "description": "Fast file pattern matching", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "grep", "description": "Fast text search", "parameters": {"type": "object", "properties": {"pattern": {"type": "string"}}, "required": ["pattern"]}}},
    {"type": "function", "function": {"name": "read", "description": "Read file contents", "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}}
]

_api_key_cache = []

def load_configs():
    files = sorted(glob.glob(os.path.join(CONFIG_DIR, "wg-*.conf")))
    configs = []
    for f in files:
        with open(f, "r") as fp:
            content = fp.read()
        priv_m = re.search(r"PrivateKey\s*=\s*([^\n]+)", content)
        pub_m = re.search(r"PublicKey\s*=\s*([^\n]+)", content)
        end_m = re.search(r"Endpoint\s*=\s*([^:]+):(\d+)", content)
        addr_m = re.search(r"Address\s*=\s*([^\n]+)", content)
        srv_m = re.search(r"#\s*([A-Za-z0-9#_-]+(?:\s+[A-Za-z0-9#_-]+)?)", content)
        if priv_m and pub_m and end_m:
            server_name = srv_m.group(1).replace(" ", "") if srv_m else os.path.basename(f)
            priv = priv_m.group(1)
            pub = pub_m.group(1)
            valid = bool(BASE6444.match(priv) and BASE6444.match(pub))
            configs.append({
                "file": f,
                "server": server_name,
                "private_key": priv,
                "address": addr_m.group(1).split(",")[0] if addr_m else "10.2.0.2/32",
                "public_key": pub,
                "endpoint_ip": end_m.group(1),
                "endpoint_port": end_m.group(2),
                "valid": valid,
            })
    return configs

def get_current_index():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as fp:
                data = json.load(fp)
                return int(data.get("current_index", 0))
        except Exception:
            pass
    return 0

def save_current_index(idx, server_name):
    with open(STATE_FILE, "w") as fp:
        json.dump({
            "current_index": idx,
            "server": server_name,
            "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }, fp, indent=2)

def get_opencode_api_key():
    if _api_key_cache:
        return _api_key_cache[0]
    try:
        res = subprocess.run(
            ["sudo", "sqlite3", "-readonly", OPENCODE_DB,
             "SELECT data FROM providerConnections "
             "WHERE provider='opencode-zen' AND isActive=1 "
             "ORDER BY priority, name LIMIT 1"],
            capture_output=True, text=True, timeout=20)
        data = json.loads(res.stdout.strip())
        key = data.get("apiKey") or ""
        if key:
            _api_key_cache.append(key)
            return key
    except Exception as e:
        print(f"[!] cannot read opencode api key: {e}")
    return ""

def switch_to_config(cfg, idx):
    if not cfg.get("valid"):
        print(f"[-] Skipping {cfg['server']}: invalid/redacted key.")
        return False
    print(f"[*] Switching VPN to {cfg['server']} ({cfg['endpoint_ip']}:{cfg['endpoint_port']})...")
    subprocess.run(["sudo", "docker", "rm", "-f", CONTAINER_NAME],
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    cmd = [
        "sudo", "docker", "run", "-d",
        "--restart", "unless-stopped",
        "--name", CONTAINER_NAME,
        "--network", NETWORK_NAME,
        "--cap-add=NET_ADMIN",
        "--device=/dev/net/tun",
        "-p", f"127.0.0.1:{LOCAL_PROXY_PORT}:8888",
        "-e", "VPN_SERVICE_PROVIDER=custom",
        "-e", "VPN_TYPE=wireguard",
        "-e", f"WIREGUARD_PRIVATE_KEY={cfg['private_key']}",
        "-e", f"WIREGUARD_ADDRESSES={cfg['address']}",
        "-e", f"VPN_ENDPOINT_IP={cfg['endpoint_ip']}",
        "-e", f"VPN_ENDPOINT_PORT={cfg['endpoint_port']}",
        "-e", f"WIREGUARD_PUBLIC_KEY={cfg['public_key']}",
        "-e", "HTTPPROXY=on",
        "-e", "HTTPPROXY_STEALTH=on",
        "-e", "BLOCK_MALICIOUS=off",
        "-e", "UPDATER_PERIOD=0",
        "-e", "DNS_UPSTREAM_RESOLVER_TYPE=plain",
        "-e", "DNS_UPSTREAM_PLAIN_ADDRESSES=1.1.1.1:53,8.8.8.8:53",
        "-e", "HEALTH_TARGET_ADDRESSES=1.1.1.1:443,1.0.0.1:443",
        "-e", "HEALTH_RESTART_VPN=off",
        "-e", "WIREGUARD_MTU=1320",
        "qmcgaw/gluetun:latest",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"[!] Error running container: {res.stderr}")
        return False

    for _ in range(8):
        time.sleep(1)
        chk = subprocess.run([
            "curl", "-s", "-x", f"http://127.0.0.1:{LOCAL_PROXY_PORT}",
            "--max-time", "2", "https://api.ipify.org?format=json",
        ], capture_output=True, text=True)
        if "ip" in chk.stdout:
            print(f"[+] VPN Connected! Public Egress IP: {chk.stdout.strip()}")
            save_current_index(idx, cfg["server"])
            return True

    print(f"[!] Timeout waiting for VPN connection on {cfg['server']}.")
    return False

def probe_reachable(timeout=5):
    res = subprocess.run([
        "curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
        "-x", f"http://127.0.0.1:{LOCAL_PROXY_PORT}",
        "--max-time", str(timeout),
        "https://opencode.ai/zen/v1/models",
    ], capture_output=True, text=True, timeout=timeout + 3)
    return res.stdout.strip().endswith("200")

def test_opencode(model="deepseek-v4.1-flash-free"):
    if not probe_reachable():
        return "unreachable"

    session = make_probe_session()
    cmd = [
        "curl", "-s", "-x", f"http://127.0.0.1:{LOCAL_PROXY_PORT}",
        "https://opencode.ai/inference/v1/chat/completions",
        "-H", "Content-Type: application/json",
        "-H", "Authorization: Bearer ***",
        "-H", f"User-Agent: {PROBE_UA}",
        "-H", "x-opencode-client: cli",
        "-H", f"x-opencode-session: {session}",
        "-H", f"x-opencode-session-id: {session}",
        "-H", f"x-session-affinity: {session}",
        "-H", "x-opencode-project: global",
        "-H", "Accept: text/event-stream",
        "-d", json.dumps({
            "model": model,
            "messages": [
                {"role": "system", "content": "Do not call the `read` or `shell` tools; they are available only as placeholders."},
                {"role": "user", "content": "ping"}
            ],
            "stream": True,
            "tools": PROBE_TOOLS,
        }),
        "--max-time", "15",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
    out = res.stdout or ""
    if "FreeUsageLimitError" in out or "rate limit" in out.lower():
        return "rate_limited"
    if "chat.completion" in out or "data: {" in out:
        return "ok"
    if not out.strip():
        return "error"
    return "error"

def health_ok(timeout=4):
    try:
        res = subprocess.run([
            "curl", "-s", "-x", f"http://127.0.0.1:{LOCAL_PROXY_PORT}",
            "--max-time", str(timeout), "https://api.ipify.org",
        ], capture_output=True, text=True, timeout=timeout + 3)
        return bool(re.fullmatch(r"\d{1,3}(?:\.\d{1,3}){3}", (res.stdout or "").strip()))
    except Exception:
        return False

def load_egress_status():
    try:
        with open(EGRESS_STATUS_FILE, "r") as fp:
            raw = json.load(fp)
        return {k: v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        return {}

def _fresh(status_ts, now=None):
    if not status_ts:
        return False
    try:
        stamp = time.mktime(time.strptime(status_ts, "%Y-%m-%d %H:%M:%S"))
    except Exception:
        return False
    return ((now or time.time()) - stamp) < EGRESS_STALE_SECONDS

def rotate():
    configs = load_configs()
    if not configs:
        print("[!] No WireGuard configurations found in", CONFIG_DIR)
        return False

    current_idx = get_current_index()
    if not 0 <= current_idx < len(configs):
        current_idx = 0

    cur = configs[current_idx]
    if cur.get("valid"):
        status = test_opencode()
        print(f"[i] current node {cur['server']}: {status}")
        if status == "ok":
            print("[i] current node already healthy; nothing to rotate.")
            return True

    egress = load_egress_status()
    now = time.time()
    tiers = [[], [], []]

    for idx in range(len(configs)):
        if idx == current_idx:
            continue
        cfg = configs[idx]
        if not cfg.get("valid"):
            print(f"[-] skipping {cfg['server']}: invalid key")
            continue

        rec = egress.get(os.path.basename(cfg["file"]))
        if rec is None:
            tiers[1].append((idx, cfg))
        elif rec.get("ok"):
            tiers[0].append((idx, cfg))
        elif _fresh(rec.get("at"), now):
            tiers[2].append((idx, cfg))
        else:
            tiers[1].append((idx, cfg))

    print(f"[i] egress tiers: {len(tiers[0])} proven / "
          f"{len(tiers[1])} unproven / {len(tiers[2])} dead-last")

    ordered = tiers[0] + tiers[1] + tiers[2]

    for idx, cfg in ordered:
        print(f"Trying config [{idx + 1}/{len(configs)}]: {cfg['server']}")
        if switch_to_config(cfg, idx):
            status = test_opencode()
            print(f"OpenCode test result on {cfg['server']}: {status}")
            if status == "ok":
                print(f"[SUCCESS] Rotated to clean server: {cfg['server']}")
                return True
            print(f"[WARN] Server {cfg['server']} status is {status}, trying next...")

    print("[!] All configurations exhausted or hit rate limit.")
    return False

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "rotate":
        sys.exit(0 if rotate() else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "health":
        ok = health_ok()
        print("healthy" if ok else "unhealthy")
        sys.exit(0 if ok else 1)
    elif len(sys.argv) > 1 and sys.argv[1] == "status":
        configs = load_configs()
        idx = get_current_index()
        print(f"Total configs: {len(configs)}")
        print(f"Current active config index: {idx} "
              f"({configs[idx]['server'] if idx < len(configs) else 'unknown'})")
        print(f"Valid configs: {sum(1 for c in configs if c['valid'])}/{len(configs)}")
        print(f"Tunnel health: {'healthy' if health_ok() else 'unhealthy'}")
        print(f"OpenCode probe: {test_opencode()}")
    else:
        status = test_opencode()
        print(f"OpenCode probe status: {status}")
        if status in ("rate_limited", "unreachable", "error"):
            print("[*] Node not usable; triggering rotation...")
            rotate()
