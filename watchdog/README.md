# Kai Watchdog Agents

Small standalone scripts that watch a machine for trouble and ping Kai when
something's worth a look — a timestamp, which scanner raised it, and a
suggestion for what to check. Kai surfaces pending reports the next time you
talk to her, the same way she delivers her own welcome-back notes.

These scripts are self-contained: stdlib + `requests` only, no `kai` package,
no models. They're meant to run on *any* PC in the house, including ones that
don't have Kai installed.

## Getting this folder onto a new machine

Pick whichever is easier:

- **Copy it directly** — this `watchdog/` folder has no hidden dependency on
  the rest of the repo, so you can zip it, drop it on a thumb drive, clone a
  slim mirror repo, whatever. Just bring the whole folder.
- **Download it from Kai** — if the new machine can reach Kai over the
  network, grab the bundle straight from her:

  ```
  curl http://<kai-host>:<port>/watchdog/download -o watchdog-agent.zip
  unzip watchdog-agent.zip
  ```

  This always matches the protocol the running server expects, so there's
  nothing to keep in sync.

## Setup

1. Install the one dependency: `pip install requests`
2. Get a join code from Kai — ask her (a logged-in session can mint one via
   `POST /api/watchdog/join-code`), or have whoever runs Kai generate one for you.
3. Pair this machine (run once):

   ```
   python join.py http://<kai-host>:<port> <join-code> --label "office-pc"
   ```

   This registers the machine and saves its identity to
   `watchdog_config.json`. Every scanner script below shares it.
4. Run a scanner:

   ```
   python test_ping.py            # confirms the pipeline works end to end
   python test_ping.py --loop     # or keep it running and re-check periodically
   ```

## Writing a new scanner

Use `test_ping.py` as the template. The shape is always:

```python
import common

def check_once(config_path):
    if <your trigger condition>:
        common.send_event(
            script_id="my_scanner",
            severity="warning",        # "info" | "warning" | "alert"
            message="what you found",
            suggestion="what Kai should look into",
            config_path=config_path,
        )

def main():
    parser = common.scanner_arg_parser(__doc__)
    args = parser.parse_args()
    common.run_loop(lambda: check_once(args.config), args.interval)
```

`common.send_event` handles signing the report with this machine's paired
identity and posting it to Kai — Kai checks it against her device registry
before queueing anything, so an unpaired or revoked machine can't spam her.
