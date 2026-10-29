"""
Entry point for:
    python -m aws_radar
    aws-radar      (after pip install)
"""

import argparse
import sys

# Mirrors aws_radar.billing.METRICS. Duplicated here so `--help` works without
# boto3 installed — importing billing pulls in boto3 at module scope.
# tests/test_aws_radar.py keeps the two lists in sync.
METRICS = [
    "UnblendedCost", "AmortizedCost", "BlendedCost",
    "NetUnblendedCost", "NetAmortizedCost", "UsageQuantity",
]

# Mirrors 1..aws_radar.billing.MAX_FORECAST_MONTHS, for the same reason.
FORECAST_MONTHS = range(1, 19)


def main():
    parser = argparse.ArgumentParser(
        prog="aws-radar",
        description="AWS resource inventory & draw.io architecture diagram generator",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # ── inventory sub-command ──
    inv = sub.add_parser("inventory", help="Scan AWS account and list all resources")
    inv.add_argument("--region",      default=None,  help="AWS region (default: boto3 default)")
    inv.add_argument("--profile",     default=None,  help="AWS SSO/named profile")
    inv.add_argument("--role-arn",    default=None,  help="IAM role ARN to assume")
    inv.add_argument("--all-regions", action="store_true", help="Scan all enabled regions")
    inv.add_argument("--ai", action=argparse.BooleanOptionalAction, default=True,
                     help="Scan AI/ML services (Bedrock, SageMaker, Rekognition, Lex, Kendra, …). On by default; pass --no-ai to skip them")
    inv.add_argument("--tags",        action="store_true", help="Fetch resource tags and add a 'Tags' column")
    inv.add_argument("--tags-wide",   action="store_true", help="Also write a CSV with one column per tag key (implies --tags)")
    inv.add_argument("--cost-allocation-tags", action="store_true", help="Add a 'CostAllocTags' column with billing-activated tags (implies --tags)")
    inv.add_argument("--include-aws-tags", action="store_true", help="Include aws:-prefixed system tags (excluded by default)")
    inv.add_argument("--billing",     action="store_true", help="Also pull Cost Explorer daily cost by service and write a CSV")
    inv.add_argument("--billing-days", type=int, default=None, help="Billing window in days (default: 90)")
    inv.add_argument("--billing-metric", default=None, choices=METRICS, help="Cost Explorer metric (default: UnblendedCost)")
    inv.add_argument("--billing-output", metavar="FILE.csv", default=None, help="Billing CSV path (default: billing.csv)")
    inv.add_argument("--include-zero", action="store_true", help="Keep zero-cost service/day buckets in the billing CSV")
    inv.add_argument("--forecasting", action="store_true", help="Also forecast projected monthly spend with Cost Explorer")
    inv.add_argument("--forecast-months", type=int, metavar="N", default=None, choices=FORECAST_MONTHS, help="Forecast horizon in months, 1–18 (default: 6)")
    inv.add_argument("--forecast-output", metavar="FILE.csv", default=None, help="Forecast CSV path (default: forecast.csv)")
    inv.add_argument("--export",      metavar="FILE.csv", help="Export results to CSV")

    # ── billing sub-command ──
    bil = sub.add_parser("billing", help="Pull Cost Explorer daily cost by service to CSV")
    bil.add_argument("--profile", default=None, help="AWS SSO/named profile")
    bil.add_argument("--days",    type=int, default=90, help="Trailing window in days (default: 90)")
    bil.add_argument("--metric",  default="UnblendedCost", choices=METRICS, help="Cost Explorer metric (default: UnblendedCost)")
    bil.add_argument("--include-zero", action="store_true", help="Keep service/day buckets with zero cost")
    bil.add_argument("--output",  metavar="FILE.csv", default="billing.csv", help="CSV output path (default: billing.csv)")
    bil.add_argument("--forecasting", action="store_true", help="Also forecast projected monthly spend")
    bil.add_argument("--forecast-months", type=int, metavar="N", default=6, choices=FORECAST_MONTHS, help="Forecast horizon in months, 1–18 (default: 6)")
    bil.add_argument("--forecast-output", metavar="FILE.csv", default="forecast.csv", help="Forecast CSV path (default: forecast.csv)")

    # ── gpu sub-command ──
    gpu = sub.add_parser("gpu", help="GPU fleet utilisation, idle spend and savings recommendations")
    gpu.add_argument("--profile",     default=None, help="AWS SSO/named profile")
    gpu.add_argument("--region",      default=None, help="AWS region (default: boto3 default)")
    gpu.add_argument("--all-regions", action="store_true", help="Scan all enabled regions")
    gpu.add_argument("--days",        type=int, default=30, help="Lookback window in days (default: 30)")
    gpu.add_argument("--price",       action="append", metavar="GPU=USD", help="$ per GPU-hour for a GPU type, e.g. L4=0.80 (repeatable)")
    gpu.add_argument("--attention",   action="store_true", help="Check EKS GPU pods for FlashAttention (needs aws-radar[k8s] and a kubeconfig)")
    gpu.add_argument("--kube-context", default=None, help="kubeconfig context for --attention (default: current context)")
    gpu.add_argument("--output",      metavar="FILE.json", default=None, help="Write the full report as JSON")

    # ── diagram sub-command ──
    dia = sub.add_parser("diagram", help="Generate draw.io diagram from inventory CSV")
    dia.add_argument("--input",  required=True,          help="CSV produced by 'inventory'")
    dia.add_argument("--output", default="architecture.drawio", help="Output .drawio file")

    # ── all-in-one sub-command ──
    run = sub.add_parser("run", help="Inventory + diagram in one shot")
    run.add_argument("--region",      default=None)
    run.add_argument("--profile",     default=None)
    run.add_argument("--role-arn",    default=None)
    run.add_argument("--all-regions", action="store_true")
    run.add_argument("--ai", action=argparse.BooleanOptionalAction, default=True,
                     help="Scan AI/ML services (on by default; --no-ai to skip)")
    run.add_argument("--tags",   action="store_true", help="Fetch resource tags and add a 'Tags' column")
    run.add_argument("--tags-wide", action="store_true", help="Also write a CSV with one column per tag key (implies --tags)")
    run.add_argument("--cost-allocation-tags", action="store_true", help="Add a 'CostAllocTags' column with billing-activated tags (implies --tags)")
    run.add_argument("--include-aws-tags", action="store_true", help="Include aws:-prefixed system tags (excluded by default)")
    run.add_argument("--billing", action="store_true", help="Also pull Cost Explorer daily cost by service and write a CSV")
    run.add_argument("--billing-days", type=int, default=None, help="Billing window in days (default: 90)")
    run.add_argument("--billing-metric", default=None, choices=METRICS, help="Cost Explorer metric (default: UnblendedCost)")
    run.add_argument("--billing-output", metavar="FILE.csv", default=None, help="Billing CSV path (default: billing.csv)")
    run.add_argument("--include-zero", action="store_true", help="Keep zero-cost service/day buckets in the billing CSV")
    run.add_argument("--forecasting", action="store_true", help="Also forecast projected monthly spend with Cost Explorer")
    run.add_argument("--forecast-months", type=int, metavar="N", default=None, choices=FORECAST_MONTHS, help="Forecast horizon in months, 1–18 (default: 6)")
    run.add_argument("--forecast-output", metavar="FILE.csv", default=None, help="Forecast CSV path (default: forecast.csv)")
    run.add_argument("--csv",   default="inventory.csv",      help="Intermediate CSV path")
    run.add_argument("--output", default="architecture.drawio", help="Output .drawio file")

    args = parser.parse_args()

    if args.command == "inventory":
        from aws_radar.inventory import main as inv_main
        sys.argv = _rebuild_argv("inventory", args)
        inv_main()

    elif args.command == "billing":
        from aws_radar.billing import main as bil_main
        sys.argv = _rebuild_argv("billing", args)
        bil_main()

    elif args.command == "gpu":
        from aws_radar.gpu import main as gpu_main
        sys.argv = _rebuild_argv("gpu", args)
        gpu_main()

    elif args.command == "diagram":
        from aws_radar.drawio import main as dia_main
        sys.argv = _rebuild_argv("diagram", args)
        dia_main()

    elif args.command == "run":
        import csv as _csv
        from aws_radar.inventory import main as inv_main
        from aws_radar.drawio import main as dia_main

        # Step 1 — inventory
        print("── Step 1/2: Running inventory ──")
        inv_args = ["aws-radar"]
        if args.profile:     inv_args += ["--profile", args.profile]
        if args.role_arn:    inv_args += ["--role-arn", args.role_arn]
        if args.region:      inv_args += ["--region", args.region]
        if args.all_regions: inv_args += ["--all-regions"]
        # Ternary, not `if args.ai:` — a bare if would emit nothing for --no-ai
        # and let inventory.py's own default (True) decide instead.
        inv_args += ["--ai"] if args.ai else ["--no-ai"]
        if args.tags:                 inv_args += ["--tags"]
        if args.tags_wide:            inv_args += ["--tags-wide"]
        if args.cost_allocation_tags: inv_args += ["--cost-allocation-tags"]
        if args.include_aws_tags:     inv_args += ["--include-aws-tags"]
        if args.billing:              inv_args += ["--billing"]
        if args.billing_days is not None:   inv_args += ["--billing-days", str(args.billing_days)]
        if args.billing_metric:             inv_args += ["--billing-metric", args.billing_metric]
        if args.billing_output:             inv_args += ["--billing-output", args.billing_output]
        if args.include_zero:               inv_args += ["--include-zero"]
        if args.forecasting:                inv_args += ["--forecasting"]
        if args.forecast_months is not None: inv_args += ["--forecast-months", str(args.forecast_months)]
        if args.forecast_output:            inv_args += ["--forecast-output", args.forecast_output]
        inv_args += ["--export", args.csv]
        sys.argv = inv_args
        inv_main()

        # Step 2 — diagram
        print("\n── Step 2/2: Generating diagram ──")
        sys.argv = ["aws-radar", "--input", args.csv, "--output", args.output]
        dia_main()

        print(f"\n✓ Done! Open {args.output} at https://app.diagrams.net/")


# Dests whose False value is meaningful and so must be forwarded explicitly.
# inventory.py re-parses this argv with its own parser and its own defaults, so
# a flag omitted here is decided by that parser, not by the user. That is only
# correct for plain store_true flags, which default to False on both sides.
NEGATABLE = {"inventory": {"ai"}, "run": {"ai"}}


def _rebuild_argv(cmd, args):
    argv = ["aws-radar"]
    negatable = NEGATABLE.get(cmd, frozenset())
    d = vars(args)
    for k, v in d.items():
        if k == "command":
            continue
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            if v:
                argv.append(flag)
            elif k in negatable:
                argv.append("--no-" + k.replace("_", "-"))
        elif isinstance(v, list):           # action="append": repeat the flag
            for item in v:
                argv += [flag, str(item)]
        elif v is not None:
            argv += [flag, str(v)]
    return argv


if __name__ == "__main__":
    main()
