"""
Offline tests for the direct-IP access (curl --resolve) helpers in
digitalbeef.py. No network required:

    python test_ip_pins.py
"""

from __future__ import annotations

import sys

import digitalbeef as db

failures: list[str] = []


def check(label, got, want):
    if got == want:
        print(f"  ok    {label}")
    else:
        print(f"  FAIL  {label}\n          got  {got!r}\n          want {want!r}")
        failures.append(label)


def main() -> int:
    print("parse_ip_pins")
    check("host=ip",
          db.parse_ip_pins(["chianina.digitalbeef.com=1.2.3.4"]),
          {"chianina.digitalbeef.com": ["1.2.3.4"]})
    check("host:ip form",
          db.parse_ip_pins(["shorthorn.digitalbeef.com:5.6.7.8"]),
          {"shorthorn.digitalbeef.com": ["5.6.7.8"]})
    check("multi-IP, tried in order",
          db.parse_ip_pins(["h=1.1.1.1,2.2.2.2"]),
          {"h": ["1.1.1.1", "2.2.2.2"]})
    check("host lower-cased",
          db.parse_ip_pins(["CHIANINA.DigitalBeef.com=9.9.9.9"]),
          {"chianina.digitalbeef.com": ["9.9.9.9"]})
    check("one string, ';'-separated",
          db.parse_ip_pins("a=1.1.1.1; b=2.2.2.2"),
          {"a": ["1.1.1.1"], "b": ["2.2.2.2"]})
    check("'default' expands to the built-in map",
          db.parse_ip_pins(["default"]),
          {h.lower(): list(ips) for h, ips in db.DEFAULT_IP_PINS.items()})
    check("blank tokens ignored",
          db.parse_ip_pins(["", "  ", "a=1.1.1.1"]),
          {"a": ["1.1.1.1"]})

    got_error = False
    try:
        db.parse_ip_pins(["nonsense-without-an-ip"])
    except ValueError:
        got_error = True
    check("bad spec raises ValueError", got_error, True)

    print("\n_create_connection_pinned")
    # Stub the real resolver so nothing touches the network; it just records the
    # address it was handed and, for the failover case, refuses the first IP.
    seen: list[tuple] = []
    refuse = {"1.1.1.1"}

    def fake_orig(address, *a, **kw):
        seen.append(address)
        if address[0] in refuse:
            raise OSError("connection refused")
        return ("socket-to", address)

    saved_orig = db._orig_create_connection
    saved_pins = dict(db._active_pins)
    try:
        db._orig_create_connection = fake_orig
        db._active_pins = {"pinned.example.com": ["1.1.1.1", "2.2.2.2"]}

        seen.clear()
        result = db._create_connection_pinned(("pinned.example.com", 443))
        check("dials the pinned IP, not the hostname", seen[-1], ("2.2.2.2", 443))
        check("keeps the original port", result, ("socket-to", ("2.2.2.2", 443)))
        check("first IP tried before failover", seen[0], ("1.1.1.1", 443))

        seen.clear()
        db._create_connection_pinned(("unpinned.example.com", 443))
        check("unpinned host passes straight through",
              seen[-1], ("unpinned.example.com", 443))
    finally:
        db._orig_create_connection = saved_orig
        db._active_pins = saved_pins

    print("\npin_host_ips (additive + idempotent)")
    real_cc = db.urllib3_connection.create_connection
    saved_orig = db._orig_create_connection
    saved_pins = dict(db._active_pins)
    try:
        db._orig_create_connection = None
        db._active_pins = {}
        db.pin_host_ips({"h.example.com": ["1.1.1.1"]})
        db.pin_host_ips({"h.example.com": ["1.1.1.1", "2.2.2.2"]})  # dupe + new
        check("merges without duplicating",
              db._active_pins, {"h.example.com": ["1.1.1.1", "2.2.2.2"]})
        check("resolver installed exactly once",
              db.urllib3_connection.create_connection is db._create_connection_pinned,
              True)
    finally:
        db.urllib3_connection.create_connection = real_cc
        db._orig_create_connection = saved_orig
        db._active_pins = saved_pins

    print()
    if failures:
        print(f"{len(failures)} FAILED: {', '.join(failures)}")
        return 1
    print("all IP-pin checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
