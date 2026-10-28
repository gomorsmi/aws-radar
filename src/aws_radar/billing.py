#!/usr/bin/env python3
"""
Cost Explorer collection for aws-radar.

Pulls daily unblended cost grouped by AWS service for a trailing window
(90 days by default) via ``ce:GetCostAndUsage`` and writes one CSV row per
(date, service) pair.

Cost Explorer is a global service reachable only through the us-east-1
endpoint, so the region flags used elsewhere in aws-radar do not apply here.

Required IAM:
    ce:GetCostAndUsage

Note: Cost Explorer bills $0.01 per paginated API request.

Usage:
    aws-radar billing --output billing.csv
    aws-radar billing --days 30 --metric AmortizedCost
    aws-radar inventory --billing --export inventory.csv
"""

import argparse
import csv
import sys
from datetime import date, timedelta

try:
    import boto3
    from botocore.exceptions import (
        ClientError, NoCredentialsError, EndpointConnectionError, BotoCoreError,
    )
except ImportError:
    sys.exit(
        "\nError: boto3 is not installed in this Python environment.\n\n"
        "Install it into the same environment as aws-radar:\n"
        "    python3 -m pip install --user boto3 rich\n"
    )

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None

BILLING_COLS = ["AccountID", "Date", "Service", "Amount", "Currency"]

METRICS = [
    "UnblendedCost", "AmortizedCost", "BlendedCost",
    "NetUnblendedCost", "NetAmortizedCost", "UsageQuantity",
]

DEFAULT_DAYS = 90


def date_window(days=DEFAULT_DAYS, today=None):
    """Return ``(start, end)`` ISO dates for the trailing *days* window.

    Cost Explorer treats ``End`` as exclusive, so ``end`` is today and the
    window covers the *days* complete days ending yesterday.
    """
    today = today or date.today()
    end = today
    start = end - timedelta(days=days)
    return start.isoformat(), end.isoformat()


def fetch_daily_cost_by_service(session, account_id, days=DEFAULT_DAYS,
                                metric="UnblendedCost", include_zero=False):
    """Return ``[{AccountID, Date, Service, Amount, Currency}, …]``.

    One row per (day, service). Days with a zero amount for a service are
    dropped unless *include_zero* — Cost Explorer emits a bucket for every
    service the account has ever touched, and most of them are 0.
    """
    start, end = date_window(days)
    ce = session.client("ce", region_name="us-east-1")

    rows = []
    zeros = 0
    token = None
    pages = 0
    while True:
        kwargs = {
            "TimePeriod": {"Start": start, "End": end},
            "Granularity": "DAILY",
            "Metrics": [metric],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if token:
            kwargs["NextPageToken"] = token
        try:
            resp = ce.get_cost_and_usage(**kwargs)
        except ClientError as e:
            code = e.response["Error"]["Code"]
            if code in ("AccessDenied", "AccessDeniedException"):
                print("  [!] Cost Explorer access denied — need ce:GetCostAndUsage "
                      "(and Cost Explorer enabled in the payer account)")
            elif code == "DataUnavailableException":
                print("  [!] Cost Explorer has no data for this window yet "
                      "(it takes ~24h to populate after first enablement)")
            else:
                print(f"  [!] Cost Explorer error ({code}): {e}")
            return rows
        except (EndpointConnectionError, BotoCoreError) as e:
            print(f"  [!] Cost Explorer unreachable: {type(e).__name__}")
            return rows

        pages += 1
        for period in resp.get("ResultsByTime", []):
            day = period["TimePeriod"]["Start"]
            for grp in period.get("Groups", []):
                svc = grp["Keys"][0] if grp.get("Keys") else "-"
                amt = grp["Metrics"][metric]
                value = float(amt.get("Amount", 0) or 0)
                if value == 0 and not include_zero:
                    zeros += 1
                    continue
                rows.append({
                    "AccountID": account_id,
                    "Date":      day,
                    "Service":   svc,
                    "Amount":    f"{value:.6f}",
                    "Currency":  amt.get("Unit", "USD"),
                })

        token = resp.get("NextPageToken")
        if not token:
            break

    print(f"  {len(rows)} cost rows over {start} → {end} "
          f"({pages} Cost Explorer request(s))")
    if zeros:
        print(f"  {zeros} zero-cost buckets dropped (use --include-zero to keep them)")
    return rows


def totals_by_service(rows):
    """Aggregate rows into ``[(service, total), …]`` sorted by cost descending."""
    totals = {}
    for r in rows:
        totals[r["Service"]] = totals.get(r["Service"], 0.0) + float(r["Amount"])
    return sorted(totals.items(), key=lambda kv: kv[1], reverse=True)


def display_billing(rows, title="AWS Cost by Service", top=20):
    """Print the *top* services by total spend over the window."""
    if not rows:
        print("\nNo cost data found.\n")
        return

    ranked = totals_by_service(rows)
    grand = sum(t for _, t in ranked)
    currency = rows[0]["Currency"]
    shown = ranked[:top]

    if RICH_AVAILABLE:
        tbl = Table(title=title, box=box.ROUNDED, header_style="bold cyan",
                    title_style="bold white on blue")
        tbl.add_column("Service", overflow="fold")
        tbl.add_column(f"Total ({currency})", justify="right")
        tbl.add_column("Share", justify="right")
        for svc, total in shown:
            pct = (total / grand * 100) if grand else 0
            tbl.add_row(svc, f"{total:,.2f}", f"{pct:.1f}%")
        console.print()
        console.print(tbl)
        if len(ranked) > top:
            console.print(f"[dim]… and {len(ranked) - top} more service(s)[/dim]")
        console.print(f"\n[bold]Total: {grand:,.2f} {currency}[/bold]\n")
    else:
        width = max(len("Service"), max(len(s) for s, _ in shown))
        print(f"\n{'='*10} {title} {'='*10}")
        for svc, total in shown:
            pct = (total / grand * 100) if grand else 0
            print(f"{svc:<{width}}  {total:>12,.2f} {currency}  {pct:>5.1f}%")
        if len(ranked) > top:
            print(f"… and {len(ranked) - top} more service(s)")
        print(f"\nTotal: {grand:,.2f} {currency}\n")


def export_billing_csv(rows, path):
    if not rows:
        print("Nothing to export.")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=BILLING_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} cost rows to {path}")


def run_billing(session, account_id, days=DEFAULT_DAYS, metric="UnblendedCost",
                output=None, include_zero=False, show_table=True):
    """Fetch, optionally display, and optionally export daily cost by service."""
    print(f"\nFetching Cost Explorer data — last {days} days, daily, by service …")
    rows = fetch_daily_cost_by_service(session, account_id, days=days,
                                       metric=metric, include_zero=include_zero)
    if show_table:
        display_billing(rows, title=f"AWS Cost by Service – {metric} – last {days} days")
    if output:
        export_billing_csv(rows, output)
    return rows


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="AWS Cost Explorer — daily cost by service")
    parser.add_argument("--profile", default=None, help="AWS SSO/named profile")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Trailing window in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--metric", default="UnblendedCost", choices=METRICS,
                        help="Cost Explorer metric (default: UnblendedCost)")
    parser.add_argument("--include-zero", action="store_true",
                        help="Keep service/day buckets with zero cost")
    parser.add_argument("--output", metavar="FILE.csv", default="billing.csv",
                        help="CSV output path (default: billing.csv)")
    args = parser.parse_args()

    try:
        session = boto3.Session(profile_name=args.profile)
        identity = session.client("sts").get_caller_identity()
        account_id = identity["Account"]
        print(f"\n✓ Authenticated as: {identity.get('Arn', 'unknown')}")
        print(f"  Account ID : {account_id}")
    except NoCredentialsError:
        print("\n✗ No AWS credentials found.")
        print("  Configure with: aws configure  OR  set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n")
        sys.exit(1)
    except ClientError as e:
        print(f"\n✗ Auth error: {e}\n")
        sys.exit(1)

    run_billing(session, account_id, days=args.days, metric=args.metric,
                output=args.output, include_zero=args.include_zero)


if __name__ == "__main__":
    main()
