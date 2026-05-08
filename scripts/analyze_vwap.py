"""VWAP above/below entry analysis."""
import re
import sys
import glob

sys.stdout.reconfigure(encoding="utf-8")

ABOVE = "\u4e0a"  # 上
BELOW = "\u4e0b"  # 下

entries = {}
results = []

for logfile in sorted(glob.glob("logs/bot_2026*.log")):
    with open(logfile, encoding="utf-8") as f:
        lines = f.readlines()

    for i, line in enumerate(lines):
        if "vwap=" in line and "-> ENTRY" in line and "__main__" in line:
            m = re.search("__main__: " + r"\[(\w+)\]" + r".*vwap=(\d+\.\d+)\((.+?)\)", line)
            if m:
                sym = m.group(1)
                vwap = float(m.group(2))
                direction = m.group(3)
                for j in range(i + 1, min(i + 10, len(lines))):
                    pat = re.escape("[") + sym + re.escape("]") + r" ENTRY LONG \d+ shares @ " + re.escape("$") + r"(\d+\.\d+) \(order=(FJ\w+)\)"
                    m2 = re.search(pat, lines[j])
                    if m2:
                        oid = m2.group(2)
                        entries[oid] = {
                            "symbol": sym,
                            "vwap": vwap,
                            "dir": direction,
                            "entry": float(m2.group(1)),
                        }
                        break

    used_oids = set()
    for line in lines:
        D = r"(\d+\.?\d*)"
        for pat in [
            "EXIT_SYNC COMPLETE: " + r"(FJ\S+) (\w+)" + " entry=" + re.escape("$") + D + " exit=" + re.escape("$") + D + " pnl=" + re.escape("$") + r"([-\d.]+)",
            "EXIT COMPLETE: " + r"(FJ\S+) (\w+)" + " entry=" + re.escape("$") + D + " exit=" + re.escape("$") + D + " pnl=" + re.escape("$") + r"([-\d.]+)",
        ]:
            m = re.search(pat, line)
            if m:
                oid = m.group(1)
                if oid in entries and oid not in used_oids:
                    used_oids.add(oid)
                    e = entries[oid]
                    pnl = float(m.group(5))
                    results.append({
                        "oid": oid,
                        "symbol": e["symbol"],
                        "vwap": e["vwap"],
                        "dir": e["dir"],
                        "entry": e["entry"],
                        "exit": float(m.group(4)),
                        "pnl": pnl,
                    })
                break

print(f"VWAP entries: {len(entries)}")
print(f"Matched exits: {len(results)}")

if results:
    above = [r for r in results if ABOVE in r["dir"]]
    below = [r for r in results if BELOW in r["dir"]]

    print(f"\n=== ABOVE VWAP ({len(above)} trades) ===")
    if above:
        a_w = sum(1 for r in above if r["pnl"] > 0)
        a_pnl = sum(r["pnl"] for r in above)
        print(f"  Win rate: {a_w}/{len(above)} ({a_w/len(above)*100:.0f}%)")
        print(f"  Total PnL: ${a_pnl:+.2f}  Avg: ${a_pnl/len(above):+.2f}")
        for r in above:
            tag = "W" if r["pnl"] > 0 else "L"
            print(f"    {r['symbol']:>6} entry=${r['entry']:.2f} vwap=${r['vwap']:.2f} exit=${r['exit']:.2f} pnl=${r['pnl']:+.2f} [{tag}]")

    print(f"\n=== BELOW VWAP ({len(below)} trades) ===")
    if below:
        b_w = sum(1 for r in below if r["pnl"] > 0)
        b_pnl = sum(r["pnl"] for r in below)
        print(f"  Win rate: {b_w}/{len(below)} ({b_w/len(below)*100:.0f}%)")
        print(f"  Total PnL: ${b_pnl:+.2f}  Avg: ${b_pnl/len(below):+.2f}")
        for r in below:
            tag = "W" if r["pnl"] > 0 else "L"
            print(f"    {r['symbol']:>6} entry=${r['entry']:.2f} vwap=${r['vwap']:.2f} exit=${r['exit']:.2f} pnl=${r['pnl']:+.2f} [{tag}]")
    else:
        print("  (none)")

    print(f"\n=== SUMMARY ===")
    a_pnl = sum(r["pnl"] for r in above) if above else 0
    b_pnl = sum(r["pnl"] for r in below) if below else 0
    a_w = sum(1 for r in above if r["pnl"] > 0)
    b_w = sum(1 for r in below if r["pnl"] > 0)
    print(f"  ABOVE: {len(above)} trades, wins={a_w}, PnL=${a_pnl:+.2f}")
    print(f"  BELOW: {len(below)} trades, wins={b_w}, PnL=${b_pnl:+.2f}")
