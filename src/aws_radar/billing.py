#!/usr/bin/env python3
"""
Cost Explorer collection for aws-radar.

Pulls daily unblended cost grouped by AWS service for a trailing window
(90 days by default) via ``ce:GetCostAndUsage`` and writes one CSV row per
(date, service) pair.

Cost Explorer is a global service reachable only through the us-east-1
endpoint, so the region flags used elsewhere in aws-radar do not apply here.

With ``--forecasting`` it also projects monthly spend for the current and
following months via ``ce:GetCostForecast``.

Required IAM:
    ce:GetCostAndUsage
    ce:GetCostForecast   (for --forecasting)

Note: Cost Explorer bills $0.01 per paginated API request.

Usage:
    aws-radar billing --output billing.csv
    aws-radar billing --days 30 --metric AmortizedCost
    aws-radar billing --forecasting --forecast-months 12
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
# Forecasting
# ─────────────────────────────────────────────

FORECAST_COLS = ["AccountID", "Month", "Projected", "LowerBound", "UpperBound",
                 "ActualToDate", "Currency"]

# GetCostForecast spells its metrics differently from GetCostAndUsage.
FORECAST_METRICS = {
    "UnblendedCost":    "UNBLENDED_COST",
    "AmortizedCost":    "AMORTIZED_COST",
    "BlendedCost":      "BLENDED_COST",
    "NetUnblendedCost": "NET_UNBLENDED_COST",
    "NetAmortizedCost": "NET_AMORTIZED_COST",
    "UsageQuantity":    "USAGE_QUANTITY",
}

DEFAULT_FORECAST_MONTHS = 6
MAX_FORECAST_MONTHS = 18        # Cost Explorer's limit at MONTHLY granularity
FORECAST_CONFIDENCE = 80        # prediction interval, percent


def _add_months(d, n):
    """First day of the month *n* months after *d*'s month."""
    y, m = divmod(d.month - 1 + n, 12)
    return date(d.year + y, m + 1, 1)


def forecast_window(months=DEFAULT_FORECAST_MONTHS, today=None):
    """Return ``(month_start, start, end)`` ISO dates for a *months* forecast.

    The horizon is *months* calendar months starting with the current one.
    Cost Explorer only forecasts from today onward, so the current month is
    split: ``month_start → start`` is already-incurred spend (actuals) and
    ``start → end`` is forecast. ``end`` is exclusive.
    """
    today = today or date.today()
    month_start = today.replace(day=1)
    return (month_start.isoformat(), today.isoformat(),
            _add_months(today, months).isoformat())


def _report_forecast_error(e, permission):
    if isinstance(e, ClientError):
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "AccessDeniedException"):
            print(f"  [!] Cost Explorer access denied — need {permission} "
                  "(and Cost Explorer enabled in the payer account)")
        elif code == "DataUnavailableException":
            print("  [!] Cost Explorer doesn't have enough cost history to forecast yet")
        else:
            print(f"  [!] Cost Explorer error ({code}): {e}")
    else:
        print(f"  [!] Cost Explorer unreachable: {type(e).__name__}")


def fetch_monthly_forecast(session, account_id, months=DEFAULT_FORECAST_MONTHS,
                           metric="UnblendedCost", today=None):
    """Return ``[{AccountID, Month, Projected, LowerBound, UpperBound, …}, …]``.

    One row per calendar month. The current month's figures are
    actual-to-date plus the forecast for the rest of the month, so they are
    comparable with the full months that follow. Bounds are the
    ``FORECAST_CONFIDENCE``% prediction interval.
    """
    month_start, start, end = forecast_window(months, today)
    ce = session.client("ce", region_name="us-east-1")

    actual, unit = 0.0, None
    if start != month_start:
        try:
            resp = ce.get_cost_and_usage(
                TimePeriod={"Start": month_start, "End": start},
                Granularity="MONTHLY",
                Metrics=[metric],
            )
        except (ClientError, EndpointConnectionError, BotoCoreError) as e:
            _report_forecast_error(e, "ce:GetCostAndUsage")
            return []
        for period in resp.get("ResultsByTime", []):
            amt = period.get("Total", {}).get(metric, {})
            actual += float(amt.get("Amount", 0) or 0)
            unit = unit or amt.get("Unit")

    try:
        resp = ce.get_cost_forecast(
            TimePeriod={"Start": start, "End": end},
            Metric=FORECAST_METRICS[metric],
            Granularity="MONTHLY",
            PredictionIntervalLevel=FORECAST_CONFIDENCE,
        )
    except (ClientError, EndpointConnectionError, BotoCoreError) as e:
        _report_forecast_error(e, "ce:GetCostForecast")
        return []

    unit = resp.get("Total", {}).get("Unit") or unit or "USD"
    current = month_start[:7]
    rows = []
    for period in resp.get("ForecastResultsByTime", []):
        month = period["TimePeriod"]["Start"][:7]
        to_date = actual if month == current else 0.0
        mean = float(period.get("MeanValue", 0) or 0)
        # Bounds are omitted when Cost Explorer can't compute an interval.
        low = float(period.get("PredictionIntervalLowerBound", mean) or 0)
        high = float(period.get("PredictionIntervalUpperBound", mean) or 0)
        rows.append({
            "AccountID":    account_id,
            "Month":        month,
            "Projected":    f"{to_date + mean:.2f}",
            "LowerBound":   f"{to_date + low:.2f}",
            "UpperBound":   f"{to_date + high:.2f}",
            "ActualToDate": f"{to_date:.2f}",
            "Currency":     unit,
        })

    print(f"  {len(rows)} month(s) forecast over {start} → {end}")
    return rows


def forecast_title(months):
    return (f"Projected monthly spend AWS · Forecast horizon: "
            f"{months} month{'' if months == 1 else 's'}")


def _month_label(month):
    return date.fromisoformat(month + "-01").strftime("%b %Y")


def display_forecast(rows, title):
    if not rows:
        print("\nNo forecast data.\n")
        return

    currency = rows[0]["Currency"]
    total = sum(float(r["Projected"]) for r in rows)

    def basis(r):
        return "actual to date + forecast" if float(r["ActualToDate"]) else "forecast"

    if RICH_AVAILABLE:
        tbl = Table(title=title, box=box.ROUNDED, header_style="bold cyan",
                    title_style="bold white on blue")
        tbl.add_column("Month")
        tbl.add_column(f"Projected ({currency})", justify="right")
        tbl.add_column(f"{FORECAST_CONFIDENCE}% range", justify="right")
        tbl.add_column("Basis", style="dim")
        for r in rows:
            tbl.add_row(_month_label(r["Month"]), f"{float(r['Projected']):,.2f}",
                        f"{float(r['LowerBound']):,.2f} – {float(r['UpperBound']):,.2f}",
                        basis(r))
        console.print()
        console.print(tbl)
        console.print(f"\n[bold]Total over horizon: {total:,.2f} {currency}[/bold]\n")
    else:
        print(f"\n{'='*10} {title} {'='*10}")
        for r in rows:
            print(f"{_month_label(r['Month']):<9}  {float(r['Projected']):>12,.2f} {currency}  "
                  f"({float(r['LowerBound']):,.2f} – {float(r['UpperBound']):,.2f})  {basis(r)}")
        print(f"\nTotal over horizon: {total:,.2f} {currency}\n")


def export_forecast_csv(rows, path):
    if not rows:
        print("No forecast to export.")
        return
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FORECAST_COLS, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} forecast rows to {path}")


def run_forecast(session, account_id, months=DEFAULT_FORECAST_MONTHS,
                 metric="UnblendedCost", output=None, show_table=True):
    """Fetch, optionally display, and optionally export the monthly forecast."""
    print(f"\nFetching Cost Explorer forecast — next {months} month(s) …")
    rows = fetch_monthly_forecast(session, account_id, months=months, metric=metric)
    if show_table:
        display_forecast(rows, forecast_title(months))
    if output:
        export_forecast_csv(rows, output)
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
    parser.add_argument("--forecasting", action="store_true",
                        help="Also forecast projected monthly spend")
    parser.add_argument("--forecast-months", type=int, metavar="N",
                        default=DEFAULT_FORECAST_MONTHS,
                        choices=range(1, MAX_FORECAST_MONTHS + 1),
                        help=f"Forecast horizon in months, 1–{MAX_FORECAST_MONTHS} "
                             f"(default: {DEFAULT_FORECAST_MONTHS})")
    parser.add_argument("--forecast-output", metavar="FILE.csv", default="forecast.csv",
                        help="Forecast CSV path (default: forecast.csv)")
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
    if args.forecasting:
        run_forecast(session, account_id, months=args.forecast_months,
                     metric=args.metric, output=args.forecast_output)


if __name__ == "__main__":
    main()
