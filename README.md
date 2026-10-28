# aws-radar

**AWS resource inventory scanner + Cost Explorer reporter + draw.io architecture diagram generator.**

Scans your AWS account(s) via SSO or IAM credentials and produces:
- A CSV inventory of every running resource
- A CSV of daily Cost Explorer spend by service (`--billing`)
- A `.drawio` file you can open in [diagrams.net](https://app.diagrams.net/) — with AWS icons, region containers, and service connections auto-wired

## Installation 

```bash
pip install aws-radar
```

## Quick start

```bash
# One-shot: inventory + diagram for specific region
aws-radar run
# One-shot: inventory + diagram for all region
aws-radar run --all-regions 

# Or step by step:
aws-radar inventory --all-regions --export inventory.csv
aws-radar diagram   --input inventory.csv --output architecture.drawio

# Daily cost by service for the last 90 days
aws-radar billing --profile my-sso-profile --output billing.csv
```
Then open `architecture.drawio` at [app.diagrams.net](https://app.diagrams.net/).

## AWS SSO usage

```bash
# Configure SSO once
aws configure sso

# Login before each session
aws sso login --profile my-profile

# Run
aws-radar run --profile my-profile --all-regions
```

## Multi-account
```bash
for profile in prod staging dev; do
  aws sso login --profile $profile
  aws-radar run --profile $profile --csv ${profile}.csv --output ${profile}.drawio
done
```


### Troubleshooting

**`ModuleNotFoundError: No module named 'boto3'` (AWS CloudShell)**

CloudShell ships its own system `boto3`, so a `pip install --user aws-radar` can skip
installing `boto3` into your user site — leaving it unimportable at runtime. Install the
dependencies into the same user site:

```bash
python3 -m pip install --user boto3 rich
```

Or, cleanest, use an isolated virtual environment:
```bash
python3 -m venv ~/aws-radar-venv
~/aws-radar-venv/bin/pip install aws-radar
~/aws-radar-venv/bin/aws-radar run
```

## Command-line options

The `inventory` command accepts the following options:

| Option | Description |
| --- | --- |
| `--region REGION` | AWS region (default: boto3 default) |
| `--profile PROFILE` | AWS SSO/named profile |
| `--all-regions` | Scan all enabled regions |
| `--ai` | Also scan AI/ML services (Bedrock, SageMaker, Rekognition, Lex, Kendra, …) |
| `--tags` | Fetch resource tags and add a `Tags` column (`key=value;…`) |
| `--tags-wide` | Also write a second CSV with one `tag:<Key>` column per tag key (implies `--tags`) |
| `--cost-allocation-tags` | Add a `CostAllocTags` column showing each resource's billing-activated tags (implies `--tags`) |
| `--include-aws-tags` | Include `aws:`-prefixed system tags (excluded by default) |
| `--billing` | Also pull Cost Explorer daily cost by service and write a CSV |
| `--billing-days N` | Billing window in days (default: 90) |
| `--billing-metric METRIC` | Cost Explorer metric (default: `UnblendedCost`) |
| `--billing-output FILE.csv` | Billing CSV path (default: `billing.csv`) |
| `--include-zero` | Keep zero-cost service/day buckets in the billing CSV |
| `--export FILE.csv` | Export results to CSV |

### Billing

`--billing` pulls Cost Explorer for the trailing 90 days at **daily**
granularity, grouped by AWS service, and writes one CSV row per (day, service):

```bash
aws-radar inventory --all-regions --export inventory.csv --billing
```

```csv
AccountID,Date,Service,Amount,Currency
123456789012,2026-05-21,Amazon Elastic Compute Cloud - Compute,12.500000,USD
123456789012,2026-05-21,Amazon Simple Storage Service,1.100000,USD
```

If you only want the cost report and not a resource scan, use the standalone
sub-command:

```bash
aws-radar billing --profile my-profile --output billing.csv
aws-radar billing --days 30 --metric AmortizedCost
```

A summary table of the top 20 services by spend is printed alongside the CSV.

Cost Explorer emits a bucket for every service the account has ever touched, so
zero-cost days are dropped by default — pass `--include-zero` to keep them. Note
that Cost Explorer is a global service reachable only through `us-east-1` (the
region flags don't apply to it), it charges **$0.01 per paginated API request**,
and it must be enabled in the payer account before it returns data.

### Tags

`--tags` fetches tags in bulk per region via the Resource Groups Tagging API
(one call per region) and joins them onto each resource by ARN, so you get a
serialized `Tags` column:

```bash
aws-radar inventory --all-regions --tags --export inventory.csv
```

Add `--tags-wide` to also emit `inventory-wide.csv` with one `tag:<Key>` column
per distinct key — handy for filtering/pivoting in a spreadsheet. Use
`--cost-allocation-tags` to highlight which of a resource's tags are activated
for cost allocation in Billing (read once from Cost Explorer).

S3 buckets are global, so their tags are fetched separately via
`GetBucketTagging`. Resources that aren't taggable (or have no tags) get an
empty `Tags` cell.


## Python API

```python
import boto3
from aws_radar.inventory import run_inventory
from aws_radar.drawio import build_drawio

session = boto3.Session(profile_name="my-profile")
rows = run_inventory(["us-east-1", "eu-west-1"], session, account_id="123456789012")

mxfile = build_drawio(rows, account_id="123456789012")
```

```python
from aws_radar.billing import fetch_daily_cost_by_service, totals_by_service

cost_rows = fetch_daily_cost_by_service(session, "123456789012", days=90)
for service, total in totals_by_service(cost_rows)[:10]:
    print(f"{service}: {total:,.2f}")
```

## Supported services

EC2 · RDS / Aurora · Lambda · ECS · EKS · ElastiCache · DynamoDB · S3 · OpenSearch · SQS · SNS · ALB / NLB

## Required IAM permissions

Attach the AWS-managed **`ReadOnlyAccess`** policy, or grant these specific actions:

```
ec2:Describe* · rds:Describe* · lambda:ListFunctions
ecs:List*/Describe* · eks:List*/Describe*
elasticache:Describe* · dynamodb:ListTables/DescribeTable
s3:ListAllMyBuckets · opensearch:List*/Describe*
sqs:ListQueues · sns:ListTopics
elasticloadbalancing:DescribeLoadBalancers
sts:GetCallerIdentity
```

For `--tags` you also need (all included in `ReadOnlyAccess`):

```
tag:GetResources · s3:GetBucketTagging · ce:ListCostAllocationTags
```

For `--billing` / `aws-radar billing`:

```
ce:GetCostAndUsage
```

## License

MIT
