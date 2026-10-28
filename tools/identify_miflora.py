#!/usr/bin/env python3
"""
identify_miflora.py — walk-test RSSI scanner to match MiFlora BLE sensors
to physical plants.

The HHCCJCY01 has no LED and no remote "identify" command, so we use signal
strength instead: carry the scanning machine up to one sensor at a time and
watch which MAC address spikes to a strong RSSI. The closest sensor dominates.

  RSSI is negative dBm; CLOSER = HIGHER (e.g. -45 is much closer than -85).

--------------------------------------------------------------------------
IMPORTANT — where this runs
--------------------------------------------------------------------------
Run on NATIVE Windows Python, NOT inside WSL. WSL2 has no Bluetooth stack,
so bleak finds no adapter there. On Windows, bleak uses the WinRT BLE API
directly (no admin, no driver install).

  1. Install Python for Windows from python.org (tick "Add to PATH").
  2. In PowerShell / cmd (NOT a WSL shell):
         pip install bleak
         python identify_miflora.py
  3. Copy this file out of WSL first, e.g. from a WSL shell:
         cp tools/identify_miflora.py /mnt/c/Users/<you>/Desktop/

On a real Linux box with BlueZ it also works as-is (needs an unblocked
adapter: `rfkill unblock bluetooth`).

--------------------------------------------------------------------------
Usage
--------------------------------------------------------------------------
  python identify_miflora.py             # INTERACTIVE (default): walk + tag
  python identify_miflora.py --table     # passive live RSSI table, no prompts
  python identify_miflora.py --all       # table of every BLE advertiser
  python identify_miflora.py --near -50  # stricter "next to it" threshold
  python identify_miflora.py --settle 3  # seconds it must stay top before asking

Interactive flow: carry the laptop to ONE sensor. When its signal settles
strong and steady, the script beeps and asks for that sensor's NUMBER. Type
it, Enter, walk to the next one. Press 'q' (or Ctrl-C) to finish; it writes
miflora_assignments.sql with an UPDATE line per sensor:

  UPDATE sensors SET name = '3'   -- peak -46 dBm
    WHERE mac_or_device_id = 'A4:C1:38:AB:CD:EF';

Review it, then load:  psql "$DB_URL" -f miflora_assignments.sql

Note: this fills the DEVICE side only (which MAC is where). The plant's
species / thresholds live in the separate `plants` table, attached later
via the assignment flow.

Physical cross-check: the HHCCJCY01 LED only blinks on power-up (battery
insert) — there is NO remote BLE command to flash it (see
docs/research/r2-miflora-collector.md). So once RSSI points at a sensor,
reseat its battery and watch for the blink to confirm.
"""

import argparse
import asyncio
import os
import re
import sys
import time
from collections import defaultdict, deque

# On Windows, ANSI escapes (screen clear) are ignored unless virtual-terminal
# processing is switched on for this console. Do it once; fall back to cls/clear.
_ANSI_OK = True


def _enable_ansi():
    global _ANSI_OK
    if os.name != "nt":
        return
    try:
        import ctypes

        k = ctypes.windll.kernel32
        h = k.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if k.GetConsoleMode(h, ctypes.byref(mode)):
            # ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004
            k.SetConsoleMode(h, mode.value | 0x0004)
            return
    except Exception:
        pass
    _ANSI_OK = False  # couldn't enable → use os.system('cls') instead


def _clear():
    if _ANSI_OK:
        sys.stdout.write("\033[H\033[2J")
    else:
        os.system("cls" if os.name == "nt" else "clear")

try:
    from bleak import BleakScanner
except ImportError:
    sys.exit(
        "bleak is not installed. On Windows PowerShell run:  pip install bleak\n"
        "(Do NOT run this inside WSL — WSL2 has no Bluetooth adapter.)"
    )

# Xiaomi MiBeacon service-data UUID broadcast by the HHCCJCY01 / "Flower care".
MIFLORA_SVC_UUID = "0000fe95-0000-1000-8000-00805f9b34fb"
MIFLORA_NAME_HINTS = ("flower care", "flower mate", "hhccjcy01", "vegtrug")

# addr -> deque[(timestamp, rssi)]
_history: dict[str, deque] = defaultdict(lambda: deque(maxlen=512))
# addr -> friendly name last seen
_names: dict[str, str] = {}


def _looks_like_miflora(name: str | None, adv) -> bool:
    if adv.service_uuids and MIFLORA_SVC_UUID in [u.lower() for u in adv.service_uuids]:
        return True
    if adv.service_data and any(
        u.lower() == MIFLORA_SVC_UUID for u in adv.service_data
    ):
        return True
    if name:
        n = name.lower()
        if any(h in n for h in MIFLORA_NAME_HINTS):
            return True
    return False


def _on_detect(device, adv):
    name = adv.local_name or device.name
    if name:
        _names[device.address] = name
    _history[device.address].append((time.time(), adv.rssi, _looks_like_miflora(name, adv)))


def _bar(rssi: int) -> str:
    # Map roughly -100..-30 dBm onto a 0..30 char bar.
    span = max(0, min(30, int((rssi + 100) / 70 * 30)))
    return "#" * span + "." * (30 - span)


def _render(window: float, show_all: bool):
    now = time.time()
    rows = []
    for addr, hist in _history.items():
        recent = [(ts, r, mf) for (ts, r, mf) in hist if now - ts <= window]
        if not recent:
            continue
        is_mf = any(mf for (_, _, mf) in recent)
        if not show_all and not is_mf:
            continue
        rssis = [r for (_, r, _) in recent]
        avg = sum(rssis) / len(rssis)
        rows.append((avg, addr, is_mf, len(rssis), max(rssis)))

    rows.sort(reverse=True)  # strongest (closest) first

    _clear()
    tag = "MiFlora only" if not show_all else "ALL BLE devices"
    print(f"  MiFlora walk-test — {tag} — {window:.0f}s window — Ctrl-C to quit")
    print(f"  Closer sensor = higher RSSI (less negative) = longer bar\n")
    print(f"  {'avg':>5} {'peak':>5} {'n':>3}  {'address':<18} bar")
    print("  " + "-" * 66)
    if not rows:
        print("\n  (scanning… hold the laptop right next to a sensor)")
    for avg, addr, is_mf, n, peak in rows:
        mark = "*" if is_mf else " "
        name = _names.get(addr, "")
        label = f"{addr:<18}"
        line = f"{mark} {avg:5.0f} {peak:5.0f} {n:3d}  {label} {_bar(int(avg))}"
        if name and show_all:
            line += f"  {name}"
        print(line)
    print("\n  * = decoded as a MiFlora / Xiaomi flower sensor")
    sys.stdout.flush()


def _emit_sql():
    """Print UPDATE templates for every MiFlora MAC heard, closest peak first."""
    seen = []
    for addr, hist in _history.items():
        mf_rssis = [r for (_, r, mf) in hist if mf]
        if mf_rssis:
            seen.append((max(mf_rssis), addr))
    if not seen:
        print("\n-- no MiFlora sensors were heard; nothing to emit")
        return
    seen.sort(reverse=True)  # strongest peak first
    print("\n-- Fill in name/location per MAC, then paste into psql.")
    print("-- MACs are ordered by strongest signal seen (closest first).")
    for peak, addr in seen:
        print(f"UPDATE sensors SET name = '', location = ''   -- peak {peak:.0f} dBm")
        print(f"  WHERE mac_or_device_id = '{addr}';")


# ---------------------------------------------------------------------------
# Interactive walk-test
# ---------------------------------------------------------------------------

SQL_OUT = "miflora_assignments.sql"


def _miflora_rows(window: float):
    """(avg, mac, peak, n) for MiFlora heard within `window`, strongest first."""
    now = time.time()
    rows = []
    for addr, hist in _history.items():
        recent = [(ts, r) for (ts, r, mf) in hist if mf and now - ts <= window]
        if len(recent) < 2:
            continue
        rssis = [r for (_, r) in recent]
        rows.append((sum(rssis) / len(rssis), addr, max(rssis), len(rssis)))
    rows.sort(reverse=True)
    return rows


def _closest_unassigned(window, exclude):
    """Return (mac, avg, peak) of the strongest MiFlora not in `exclude`, or None."""
    for avg, mac, peak, _ in _miflora_rows(window):
        if mac not in exclude:
            return mac, avg, peak
    return None


def _render_interactive(window, assigned, candidate, near, settling_for):
    _clear()
    print("  MiFlora INTERACTIVE walk-test — Ctrl-C or 'q' to finish\n")
    print(f"  Hold the laptop next to ONE sensor. When it settles (avg ≥ {near:.0f} dBm")
    print(f"  for a moment) you'll be asked for its number.\n")
    print(f"  assigned: {len(assigned)}   |   {'candidate':<18} {'avg':>5} {'peak':>5}")
    print("  " + "-" * 60)
    for avg, mac, peak, n in _miflora_rows(window):
        if mac in assigned:
            tag = f"→ #{assigned[mac][0]}"
        elif candidate and mac == candidate[0]:
            secs = f"settling {settling_for:.0f}s" if settling_for else "near…"
            tag = f"◀ {secs}"
        else:
            tag = ""
        print(f"  {mac:<18} {avg:5.0f} {peak:5.0f}  {_bar(int(avg))}  {tag}")
    if not _miflora_rows(window):
        print("\n  (scanning… no MiFlora heard yet)")
    sys.stdout.flush()


def _start_stdin_reader(loop):
    """Daemon thread feeding typed lines into an asyncio.Queue.

    Runs the whole session so 'q' quits at ANY time, not only at a prompt.
    Daemon => a blocked readline never stops the process from exiting.
    """
    import threading

    q: asyncio.Queue = asyncio.Queue()

    def run():
        try:
            for line in sys.stdin:
                loop.call_soon_threadsafe(q.put_nowait, line.strip())
        except Exception:
            pass

    threading.Thread(target=run, daemon=True).start()
    return q


def _write_sql_file(assigned):
    """assigned: mac -> (label, peak).  Write UPDATEs in assignment order.

    ASCII-only + explicit UTF-8 so Windows' cp1252 default can't crash the
    write (an earlier version died on a '->' arrow in the header).
    """
    lines = [
        "-- MiFlora sensor -> label mapping from interactive walk-test.",
        "-- Review, then: psql ... -f " + SQL_OUT,
        "",
    ]
    for mac, (label, peak) in assigned.items():
        safe = label.replace("'", "''")
        lines.append(f"UPDATE sensors SET name = '{safe}'   -- peak {peak:.0f} dBm")
        lines.append(f"  WHERE mac_or_device_id = '{mac}';")
    body = "\n".join(lines) + "\n"
    with open(SQL_OUT, "w", encoding="utf-8") as f:
        f.write(body)
    return body


# Parse the file we ourselves wrote, to resume across runs.
_SQL_RE = re.compile(
    r"name = '(?P<label>(?:[^']|'')*)'\s*--\s*peak\s*(?P<peak>-?\d+)\s*dBm\s*\n"
    r"\s*WHERE mac_or_device_id = '(?P<mac>[0-9A-Fa-f:]+)';"
)


def _load_sql_file():
    """Read back an existing miflora_assignments.sql -> {mac: (label, peak)}."""
    if not os.path.exists(SQL_OUT):
        return {}
    try:
        text = open(SQL_OUT, encoding="utf-8").read()
    except OSError:
        return {}
    out = {}
    for m in _SQL_RE.finditer(text):
        label = m.group("label").replace("''", "'")  # un-escape
        out[m.group("mac")] = (label, float(m.group("peak")))
    return out


_QUIT_WORDS = ("q", "quit", "exit")


async def _interactive_loop(window, near, settle_secs, assigned):
    # assigned (mac -> (label, peak)) is owned by the caller so a Ctrl-C
    # mid-walk still preserves everything tagged so far.
    snoozed: dict[str, float] = {}               # mac -> snoozed_until_ts
    top_mac, top_since = None, 0.0

    q = _start_stdin_reader(asyncio.get_running_loop())

    print("Scanning… walk up to your first sensor.  (type 'q' + Enter anytime to finish)")
    while True:
        await asyncio.sleep(0.4)
        now = time.time()

        # 'q' must work at ANY time, not only at a settle prompt.
        while not q.empty():
            if q.get_nowait().lower() in _QUIT_WORDS:
                return
            # stray input while walking → ignore

        for m in [m for m, t in snoozed.items() if now >= t]:
            del snoozed[m]

        exclude = set(assigned) | set(snoozed)
        cand = _closest_unassigned(window, exclude)

        settling_for = (now - top_since) if (cand and cand[0] == top_mac) else 0
        _render_interactive(window, assigned, cand, near, settling_for)

        if not (cand and cand[1] >= near):
            top_mac, top_since = None, 0.0
            continue
        if cand[0] != top_mac:
            top_mac, top_since = cand[0], now
            continue
        if now - top_since < settle_secs:
            continue

        # ---- settled: prompt the user ----
        mac, avg, peak = cand
        sys.stdout.write("\a")  # bell
        print(f"\n>>> SETTLED on {mac}  (avg {avg:.0f} dBm)")
        print("    Type this sensor's NUMBER then Enter  (Enter=skip, 'q'=finish):")
        ans = await q.get()

        if ans.lower() in _QUIT_WORDS:
            return
        if ans == "":
            snoozed[mac] = time.time() + 20  # step away, revisit later
            top_mac, top_since = None, 0.0
            continue

        assigned[mac] = (ans, peak)
        _write_sql_file(assigned)  # incremental save — a later crash loses nothing
        print(f"    OK: {mac} -> sensor #{ans}  (saved).  Move to the NEXT sensor.")
        top_mac, top_since = None, 0.0
        await asyncio.sleep(1.2)  # let the user step away before re-detecting


async def main():
    ap = argparse.ArgumentParser(description="RSSI walk-test to ID MiFlora sensors.")
    ap.add_argument("--table", action="store_true", help="passive live table instead of interactive tagging")
    ap.add_argument("--all", action="store_true", help="table mode: show every BLE advertiser (implies --table)")
    ap.add_argument("--window", type=float, default=5.0, help="RSSI averaging window in seconds (default 5)")
    ap.add_argument("--near", type=float, default=-55.0, help="dBm threshold that counts as 'right next to it' (default -55)")
    ap.add_argument("--settle", type=float, default=4.0, help="seconds the closest sensor must stay top before prompting (default 4)")
    ap.add_argument("--sql", action="store_true", help="table mode: print UPDATE template on exit")
    args = ap.parse_args()

    _enable_ansi()
    scanner = BleakScanner(detection_callback=_on_detect, scanning_mode="active")
    await scanner.start()
    interactive = not (args.table or args.all)
    assigned: dict[str, tuple[str, float]] = {}  # mac -> (label, peak)
    try:
        if interactive:
            assigned.update(_load_sql_file())  # resume: skip already-tagged MACs
            if assigned:
                names = ", ".join(f"#{lbl}" for lbl, _ in assigned.values())
                print(f"Resuming — {len(assigned)} already tagged ({names}). They'll be skipped.")
                await asyncio.sleep(1.5)
            await _interactive_loop(args.window, args.near, args.settle, assigned)
        else:
            print("Scanning… (first results appear within a few seconds)")
            while True:
                await asyncio.sleep(1.0)
                _render(args.window, args.all)
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        await scanner.stop()
        print("\nStopped.")
        if interactive:
            if assigned:
                body = _write_sql_file(assigned)
                print(f"\nWrote {len(assigned)} mapping(s) to {SQL_OUT}:\n")
                print(body)
            else:
                print("No sensors tagged — nothing written.")
        elif args.sql:
            _emit_sql()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
