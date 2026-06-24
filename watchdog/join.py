"""
watchdog/join.py — pair this machine with Kai's watchdog network.

Run once per machine. Get a join code from Kai (a logged-in user mints one via
POST /api/watchdog/join-code, e.g. from Kai's settings UI), then redeem it here:

    python join.py http://kai-host:7860 <join-code> --label "office-pc"

This registers the machine, and Kai hands back a unique device_id + device_key
pair — saved to watchdog_config.json so every scanner script on this machine
can reuse the same identity. Re-running with a used/expired code will fail;
get a fresh one from Kai.
"""

import argparse
import sys

import requests

from common import DEFAULT_CONFIG_PATH, default_pc_label, save_config


def main():
    parser = argparse.ArgumentParser(description="Pair this machine with Kai's watchdog network")
    parser.add_argument("server", help="Kai's base URL, e.g. http://localhost:7860")
    parser.add_argument("join_code", help="single-use code minted by a logged-in Kai session")
    parser.add_argument("--label", default=None, help="friendly name for this machine (default: hostname)")
    args = parser.parse_args()

    label = args.label or default_pc_label()
    server_url = args.server.rstrip("/")

    resp = requests.post(
        f"{server_url}/api/watchdog/register",
        json={"join_code": args.join_code, "label": label},
        timeout=15,
    )
    if resp.status_code != 200:
        print(f"[watchdog] registration failed ({resp.status_code}): {resp.text}")
        sys.exit(1)

    data = resp.json()
    save_config({
        "server_url": server_url,
        "device_id": data["device_id"],
        "device_key": data["device_key"],
        "pc_label": label,
    })
    print(f"[watchdog] paired as '{label}' (device_id={data['device_id']})")
    print(f"[watchdog] identity saved to {DEFAULT_CONFIG_PATH}")
    print("[watchdog] you can now run any scanner script in this folder, e.g.:")
    print("           python test_ping.py")


if __name__ == "__main__":
    main()
