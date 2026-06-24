"""
watchdog/test_ping.py — the easy-trigger test scanner.

Not a real monitor — it exists to prove the whole pipeline works end to end
(join → scan → report → queue → surfaced in chat) before writing scanners that
wait on real conditions like low disk space or high temps. Its trigger is
deliberately trivial: it fires on its very first check, every run.

    python test_ping.py                  # one-shot: fires once and exits
    python test_ping.py --loop           # fires once per --interval, forever
"""

import argparse
import datetime

import common

SCRIPT_ID = "test_ping"


def check_once(config_path):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    result = common.send_event(
        script_id=SCRIPT_ID,
        severity="info",
        message=f"test_ping fired at {now} — pipeline check, not a real issue.",
        suggestion="No action needed — this just confirms the watchdog pipeline is wired up end to end.",
        config_path=config_path,
    )
    print(f"[watchdog] reported test ping ({now}): {result}")


def main():
    parser = common.scanner_arg_parser(__doc__)
    parser.add_argument("--loop", action="store_true", help="keep firing every --interval seconds")
    args = parser.parse_args()

    if args.loop:
        common.run_loop(lambda: check_once(args.config), args.interval)
    else:
        check_once(args.config)


if __name__ == "__main__":
    main()
