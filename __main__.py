"""
Entry point for:
    python -m aws_radar
    aws-radar      (after pip install)
"""

import argparse
import sys


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
    inv.add_argument("--ai",          action="store_true", help="Also scan AI/ML services (Bedrock, SageMaker, Rekognition, Lex, Kendra, …)")
    inv.add_argument("--tags",        action="store_true", help="Fetch resource tags and add a 'Tags' column")
    inv.add_argument("--tags-wide",   action="store_true", help="Also write a CSV with one column per tag key (implies --tags)")
    inv.add_argument("--cost-allocation-tags", action="store_true", help="Add a 'CostAllocTags' column with billing-activated tags (implies --tags)")
    inv.add_argument("--include-aws-tags", action="store_true", help="Include aws:-prefixed system tags (excluded by default)")
    inv.add_argument("--export",      metavar="FILE.csv", help="Export results to CSV")

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
    run.add_argument("--ai",     action="store_true", help="Also scan AI/ML services")
    run.add_argument("--tags",   action="store_true", help="Fetch resource tags and add a 'Tags' column")
    run.add_argument("--tags-wide", action="store_true", help="Also write a CSV with one column per tag key (implies --tags)")
    run.add_argument("--cost-allocation-tags", action="store_true", help="Add a 'CostAllocTags' column with billing-activated tags (implies --tags)")
    run.add_argument("--include-aws-tags", action="store_true", help="Include aws:-prefixed system tags (excluded by default)")
    run.add_argument("--csv",    default="inventory.csv",      help="Intermediate CSV path")
    run.add_argument("--output", default="architecture.drawio", help="Output .drawio file")

    args = parser.parse_args()

    if args.command == "inventory":
        from aws_radar.inventory import main as inv_main
        sys.argv = _rebuild_argv("inventory", args)
        inv_main()

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
        if args.ai:          inv_args += ["--ai"]
        if args.tags:                 inv_args += ["--tags"]
        if args.tags_wide:            inv_args += ["--tags-wide"]
        if args.cost_allocation_tags: inv_args += ["--cost-allocation-tags"]
        if args.include_aws_tags:     inv_args += ["--include-aws-tags"]
        inv_args += ["--export", args.csv]
        sys.argv = inv_args
        inv_main()

        # Step 2 — diagram
        print("\n── Step 2/2: Generating diagram ──")
        sys.argv = ["aws-radar", "--input", args.csv, "--output", args.output]
        dia_main()

        print(f"\n✓ Done! Open {args.output} at https://app.diagrams.net/")


def _rebuild_argv(cmd, args):
    argv = ["aws-radar"]
    d = vars(args)
    for k, v in d.items():
        if k == "command":
            continue
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            if v:
                argv.append(flag)
        elif v is not None:
            argv += [flag, str(v)]
    return argv


if __name__ == "__main__":
    main()
