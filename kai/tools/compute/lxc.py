"""
lxc.* tools — manage system containers / VMs via LXD or Incus.

Both LXD (`lxc` client) and Incus (`incus` client) share the same subcommand
grammar (list / info / launch / start / stop / delete), so a single adapter
covers either. We detect whichever client is installed at call time.

Linux only. If no client is found, every tool returns a helpful install hint
instead of an error — the model should relay that, not claim a failure.
"""
from __future__ import annotations
import json
import shutil
import subprocess

from kai.tools.registry import registry
from kai.system.platform import IS_WINDOWS as _IS_WINDOWS

# Default image when the caller doesn't name one. Ubuntu LTS is the safe pick
# that exists on both the LXD (`ubuntu:`) and Incus (`images:`) remotes.
_DEFAULT_IMAGE_LXD = "ubuntu:22.04"
_DEFAULT_IMAGE_INCUS = "images:ubuntu/22.04"

_client_cache: str | None = None
_client_searched = False


def _find_client() -> str | None:
    """Return the container client binary name (`incus` preferred, then `lxc`)."""
    global _client_cache, _client_searched
    if _client_searched:
        return _client_cache
    _client_searched = True
    for name in ("incus", "lxc"):
        if shutil.which(name):
            _client_cache = name
            break
    return _client_cache


def _no_client_msg() -> str:
    return (
        "No container manager found. Kai manages LXD or Incus containers, but "
        "neither is installed.\n"
        "  Install Incus (recommended): sudo apt install incus && sudo incus admin init\n"
        "  Or install LXD:              sudo snap install lxd && sudo lxd init\n"
        "After installing, add your user to the group (incus-admin / lxd) and re-log in."
    )


def _run(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    """Run the client with args. Returns (ok, combined_output)."""
    client = _find_client()
    if not client:
        return False, _no_client_msg()
    try:
        r = subprocess.run(
            [client, *args],
            capture_output=True, text=True, timeout=timeout,
            encoding="utf-8", errors="replace",
        )
        out = (r.stdout or "").strip()
        err = (r.stderr or "").strip()
        if r.returncode != 0:
            detail = err or out or f"exit code {r.returncode}"
            return False, f"`{client} {' '.join(args)}` failed: {detail}"
        return True, out or err
    except subprocess.TimeoutExpired:
        return False, f"`{client} {' '.join(args)}` timed out after {timeout}s."
    except Exception as exc:  # noqa: BLE001
        return False, f"Error running {client}: {exc}"


def _guard() -> str | None:
    """Return an error message if containers can't be managed here, else None."""
    if _IS_WINDOWS:
        return ("Container management is Linux-only (LXD/Incus). "
                "This machine is running Windows.")
    if not _find_client():
        return _no_client_msg()
    return None


def client_available() -> bool:
    """True if a container client is installed (and we're not on Windows)."""
    return not _IS_WINDOWS and _find_client() is not None


def list_instances_data() -> list[dict]:
    """
    Structured instance list for the UI. Returns one dict per instance:
        {name, status, type, ipv4}
    Empty list if no client is installed or the call fails — the UI treats
    "no client" and "no instances" the same way (nothing to show).
    """
    if not client_available():
        return []
    ok, out = _run(["list", "--format", "json"], timeout=20)
    if not ok or not out:
        return []
    try:
        raw = json.loads(out)
    except (ValueError, TypeError):
        return []

    instances = []
    for inst in raw:
        # Pull the first non-loopback IPv4 from the network state, if running.
        ipv4 = ""
        state = inst.get("state") or {}
        for _iface, info in (state.get("network") or {}).items():
            for addr in info.get("addresses", []):
                if addr.get("family") == "inet" and addr.get("address", "").split(".")[0] != "127":
                    ipv4 = addr["address"]
                    break
            if ipv4:
                break
        instances.append({
            "name": inst.get("name", "?"),
            "status": (inst.get("status") or "Unknown"),
            "type": inst.get("type", "container"),
            "ipv4": ipv4,
        })
    return instances


# ── List / inspect ──────────────────────────────────────────────────────────

@registry.tool(
    name="lxc.list",
    description=(
        "List all LXD/Incus system containers and VMs on this machine — their "
        "name, state (RUNNING/STOPPED), type, and IP address. Use this to see "
        "what containers exist before starting, stopping, or deleting one."
    ),
)
def list_instances() -> str:
    err = _guard()
    if err:
        return err
    ok, out = _run(["list"])
    if not ok:
        return out
    return out or "No containers found."


@registry.tool(
    name="lxc.info",
    description=(
        "Show detailed status for one container or VM: state, resource usage "
        "(CPU, memory), network addresses, and snapshots. Provide the instance name."
    ),
    parameters={
        "name": {"type": "string", "description": "Container/VM name.", "required": True},
    },
)
def instance_info(name: str) -> str:
    err = _guard()
    if err:
        return err
    ok, out = _run(["info", name])
    return out if ok else out


# ── Lifecycle ─────────────────────────────────────────────────────────────────

@registry.tool(
    name="lxc.create",
    description=(
        "Create and start a new container (or VM) from an image. Provide a name; "
        "optionally an image (defaults to Ubuntu 22.04) and vm=true for a full "
        "virtual machine instead of a system container. This downloads the image "
        "on first use and may take a minute."
    ),
    parameters={
        "name": {"type": "string", "description": "Name for the new instance.", "required": True},
        "image": {
            "type": "string",
            "description": (
                "Image to launch, e.g. 'ubuntu:22.04' (LXD) or 'images:debian/12' "
                "(Incus). Leave blank for the default Ubuntu LTS."
            ),
        },
        "vm": {
            "type": "boolean",
            "description": "True to create a full virtual machine instead of a container.",
        },
    },
)
def create_instance(name: str, image: str = "", vm: bool = False) -> str:
    err = _guard()
    if err:
        return err
    client = _find_client()
    if not image:
        image = _DEFAULT_IMAGE_INCUS if client == "incus" else _DEFAULT_IMAGE_LXD
    args = ["launch", image, name]
    if vm:
        args.append("--vm")
    ok, out = _run(args, timeout=300)
    if not ok:
        return out
    kind = "VM" if vm else "container"
    return f"Created and started {kind} '{name}' from {image}.\n{out}".strip()


@registry.tool(
    name="lxc.start",
    description="Start a stopped container or VM by name.",
    parameters={
        "name": {"type": "string", "description": "Instance name.", "required": True},
    },
)
def start_instance(name: str) -> str:
    err = _guard()
    if err:
        return err
    ok, out = _run(["start", name])
    return out if not ok else f"Started '{name}'."


@registry.tool(
    name="lxc.stop",
    description=(
        "Stop a running container or VM by name. Set force=true to stop it "
        "immediately without a graceful shutdown."
    ),
    parameters={
        "name": {"type": "string", "description": "Instance name.", "required": True},
        "force": {"type": "boolean", "description": "Force immediate stop."},
    },
)
def stop_instance(name: str, force: bool = False) -> str:
    err = _guard()
    if err:
        return err
    args = ["stop", name]
    if force:
        args.append("--force")
    ok, out = _run(args)
    return out if not ok else f"Stopped '{name}'."


@registry.tool(
    name="lxc.delete",
    description=(
        "Permanently delete a container or VM and its storage. This cannot be "
        "undone. If the instance is running you must pass force=true (it will be "
        "stopped first). Confirm with the user before deleting anything they did "
        "not explicitly ask to remove."
    ),
    parameters={
        "name": {"type": "string", "description": "Instance name to delete.", "required": True},
        "force": {
            "type": "boolean",
            "description": "Required to delete a running instance (stops it first).",
        },
    },
)
def delete_instance(name: str, force: bool = False) -> str:
    err = _guard()
    if err:
        return err
    args = ["delete", name]
    if force:
        args.append("--force")
    ok, out = _run(args)
    if not ok:
        return out
    return f"Deleted '{name}'."
