import re
from pathlib import Path
from statistics import mean

report = Path("reports/baseline-18m.txt").read_text()

section = report.split(
    "Trend Following executed trades", 1
)[1].split(
    "Trend Following remaining open position:", 1
)[0]

pattern = re.compile(
    r"Trade (\d+):.*?"
    r"Entry indicators:.*?"
    r"sma20=([\d.]+), sma50=([\d.]+).*?"
    r"Result: pnl=([-\d.]+) USDT, "
    r"return=([-\d.]+)%",
    re.DOTALL,
)

trades = []

for match in pattern.finditer(section):
    number, sma20, sma50, pnl, trade_return = match.groups()

    gap = (float(sma20) - float(sma50)) / float(sma50) * 100

    trades.append({
        "number": int(number),
        "gap": gap,
        "pnl": float(pnl),
        "return": float(trade_return),
    })

if not trades:
    raise SystemExit("No trades found in report")

print(f"Parsed trades: {len(trades)}")
print()
print(f"{'Trade':>6} {'SMA gap %':>12} {'Return %':>12} {'PnL USDT':>12}")

for trade in sorted(trades, key=lambda item: item["gap"]):
    print(
        f"{trade['number']:>6} "
        f"{trade['gap']:>12.5f} "
        f"{trade['return']:>12.4f} "
        f"{trade['pnl']:>12.2f}"
    )

print("\nDescriptive groups (not trading rules):")

for label, predicate in [
    ("Gap < 0.01%", lambda gap: gap < 0.01),
    ("0.01% <= gap < 0.05%", lambda gap: 0.01 <= gap < 0.05),
    ("Gap >= 0.05%", lambda gap: gap >= 0.05),
]:
    group = [trade for trade in trades if predicate(trade["gap"])]

    if not group:
        print(f"{label}: no trades")
        continue

    wins = sum(trade["return"] > 0 for trade in group)

    print(
        f"{label}: "
        f"trades={len(group)}, "
        f"win_rate={wins / len(group) * 100:.1f}%, "
        f"mean_return={mean(trade['return'] for trade in group):.2f}%, "
        f"total_pnl={sum(trade['pnl'] for trade in group):.2f} USDT"
    )
