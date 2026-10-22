#!/usr/bin/env python3
"""
VPN Rotator Micro-Daemon
Exposes a lightweight HTTP server on 127.0.0.1:28880 and 172.19.0.1:28880
Endpoints:
  GET /rotate -> triggers rotation to next clean WireGuard config, returns JSON
  GET /status -> returns current VPN status

2026-10-22 Hardened Edition:
  * Watchdog: 15s interval, 2 consecutive fails (30s dead detection).
  * Auto-recovery: bypasses cooldown if tunnel is hard-dead.
  * Health status check included in GET /status payload.
"""

import http.server
import socketserver
import json
import threading
import sys
import os
import time

sys.path.insert(0, "/home/ubuntu/scripts")
import proton_vpn_rotator as rotator

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore
        sys.stderr.reconfigure(line_buffering=True)  # type: ignore
    except Exception:
        pass

PORT = 28880
lock = threading.Lock()

WATCHDOG_INTERVAL = 15
WATCHDOG_THRESHOLD = 2
WATCHDOG_COOLDOWN = 180
_watchdog_state = {"fails": 0, "last_rotate": 0.0}

class QuietRotatorServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

class RotatorHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/rotate" or self.path.startswith("/rotate?"):
            with lock:
                print("[Daemon] Received /rotate request...")
                success = rotator.rotate()
                cfg_idx = rotator.get_current_index()
                configs = rotator.load_configs()
                current_server = configs[cfg_idx]['server'] if cfg_idx < len(configs) else "unknown"
                
                resp = {
                    "success": success,
                    "server": current_server,
                    "index": cfg_idx,
                    "total": len(configs)
                }
                self.send_response(200 if success else 500)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp).encode("utf-8"))

        elif self.path == "/status":
            cfg_idx = rotator.get_current_index()
            configs = rotator.load_configs()
            current_server = configs[cfg_idx]['server'] if cfg_idx < len(configs) else "unknown"
            healthy = rotator.health_ok(timeout=3)
            resp = {
                "server": current_server,
                "index": cfg_idx,
                "total": len(configs),
                "healthy": healthy
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(resp).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()

def watchdog_loop():
    """Probe egress on an interval; rotate away when the tunnel stays dead."""
    while True:
        time.sleep(WATCHDOG_INTERVAL)
        try:
            healthy = rotator.health_ok(timeout=3)
        except Exception as e:
            print(f"[Watchdog] health probe error: {e}")
            healthy = False

        if healthy:
            if _watchdog_state["fails"]:
                print("[Watchdog] egress recovered; counter reset.")
            _watchdog_state["fails"] = 0
            continue

        _watchdog_state["fails"] += 1
        fails = _watchdog_state["fails"]
        print(f"[Watchdog] egress down ({fails}/{WATCHDOG_THRESHOLD})")

        if fails < WATCHDOG_THRESHOLD:
            continue

        elapsed = time.time() - _watchdog_state["last_rotate"]
        if elapsed < WATCHDOG_COOLDOWN and fails < (WATCHDOG_THRESHOLD * 2):
            print(f"[Watchdog] pool exhausted recently ({int(WATCHDOG_COOLDOWN - elapsed)}s left); waiting before retry.")
            continue

        _watchdog_state["last_rotate"] = time.time()
        _watchdog_state["fails"] = 0
        with lock:
            print("[Watchdog] tunnel dead -> auto-rotating to next clean node")
            try:
                rotator.rotate()
            except Exception as e:
                print(f"[Watchdog] rotate failed: {e}")

def run_server():
    server_address = ("0.0.0.0", PORT)
    with QuietRotatorServer(server_address, RotatorHandler) as httpd:
        print(f"[*] VPN Rotator Micro-Daemon listening on port {PORT}...")
        threading.Thread(target=watchdog_loop, daemon=True).start()
        print(f"[*] Watchdog armed: every {WATCHDOG_INTERVAL}s, rotate after {WATCHDOG_THRESHOLD} consecutive failures")
        httpd.serve_forever()

if __name__ == "__main__":
    run_server()
