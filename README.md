# aws-radar

**AWS resource inventory scanner + Cost Explorer reporter + draw.io architecture diagram generator.**

Scans your AWS account(s) via SSO or IAM credentials and produces:
- A CSV inventory of every running resource
- A CSV of daily Cost Explorer spend by service (`--billing`)
- A projected monthly spend forecast (`--forecasting`)
- A GPU fleet report: utilisation, idle spend and savings recommendations (`aws-radar gpu`)
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
| `--forecasting` | Also forecast projected monthly spend with Cost Explorer |
| `--forecast-months N` | Forecast horizon in months, 1–18 (default: 6) |
| `--forecast-output FILE.csv` | Forecast CSV path (default: `forecast.csv`) |
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

### Forecasting

`--forecasting` asks Cost Explorer for projected monthly spend over the next 6
months (`--forecast-months N`, up to 18), prints it, and writes `forecast.csv`.
It works with `inventory`, `run` and `billing`, and uses the same metric as
`--billing-metric` / `--metric`:

```bash
aws-radar billing --forecasting
aws-radar inventory --forecasting --forecast-months 12
```

```
          Projected monthly spend AWS · Forecast horizon: 6 months
╭──────────┬─────────────────┬─────────────────────┬───────────────────────────╮
│ Month    │ Projected (USD) │           80% range │ Basis                     │
├──────────┼─────────────────┼─────────────────────┼───────────────────────────┤
│ Oct 2026 │        4,312.55 │ 4,250.10 – 4,380.02 │ actual to date + forecast │
│ Nov 2026 │        4,501.20 │ 4,102.88 – 4,899.51 │ forecast                  │
│ …        │                 │                     │                           │
╰──────────┴─────────────────┴─────────────────────┴───────────────────────────╯
```

The horizon starts with the current month. Cost Explorer only forecasts from
today onward, so the current month is month-to-date actual spend plus the
forecast for its remaining days, which makes it comparable with the full months
after it. The range is Cost Explorer's 80% prediction interval. The CSV has one
row per month:

```csv
AccountID,Month,Projected,LowerBound,UpperBound,ActualToDate,Currency
123456789012,2026-10,4312.55,4250.10,4380.02,3980.40,USD
123456789012,2026-11,4501.20,4102.88,4899.51,0.00,USD
```

A forecast costs two Cost Explorer requests ($0.02), or one on the first of the
month. Cost Explorer needs some cost history before it will forecast; a new
account gets a warning and no rows.

### GPU fleet

`aws-radar gpu` reports how busy your GPU instances and EKS GPU node groups are,
what the idle time costs, and where to save:

```bash
aws-radar gpu --region us-east-1
aws-radar gpu --all-regions --days 14 --output gpu.json
aws-radar gpu --price T4=0.53 --price L4=0.80      # $ per GPU-hour
aws-radar gpu --attention --kube-context my-eks     # needs: pip install 'aws-radar[k8s]'
```

It prints:

- **KPIs**: GPU spend, GPU count, % busy, idle GPU-hours and idle spend, and
  the total of the recommended monthly savings.
- **By GPU type**: allocated, requested and busy GPU-hours, cost per allocated
  vs. busy GPU-hour, and the markup between them.
- **Recommendations**: stop idle instances, shrink over-provisioned EKS node
  groups, move low-utilisation pods to a cheaper GPU that fits their memory, or
  share a barely-used GPU (time-slicing / MIG).
- **Attention findings** (`--attention`): EKS inference pods whose engine has
  FlashAttention switched off, or that run on GPUs without FA2 kernels.

Where the numbers come from:

| Data | Source |
| --- | --- |
| GPU instances, model, count, memory | `ec2:DescribeInstances`, `ec2:DescribeInstanceTypes`; EKS nodes are grouped by their `eks:nodegroup-name` / `karpenter.sh/nodepool` tag |
| Cost | Cost Explorer EC2 compute `AmortizedCost` by instance type, split across instances by running hours |
| Allocated GPU-hours | hours with CloudWatch `AWS/EC2 CPUUtilization` data × GPUs per instance |
| Busy % | CloudWatch agent `nvidia_smi_utilization_gpu`, or Container Insights `node_gpu_utilization` |
| EKS requests, pod busy %, GPU memory | Container Insights with enhanced observability (`node_gpu_usage_total`, `pod_gpu_*`) |

GPU utilisation isn't a default EC2 metric. Without the CloudWatch agent's
NVIDIA metrics or Container Insights, an instance's utilisation is unknown: it
is counted in the `unmeasured` KPI and gets no stop or share recommendation,
rather than being reported as idle.

Downshift savings need a $/GPU-hour for both GPU types. The observed cost of
the types you run is used automatically; use `--price` for types you don't run.
Recommendations can overlap (for example a shrink and a downshift on the same
node group), so treat the savings total as an upper bound.

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

For `--forecasting`:

```
ce:GetCostAndUsage · ce:GetCostForecast
```

For `aws-radar gpu`:

```
ec2:DescribeInstances · ec2:DescribeInstanceTypes · ce:GetCostAndUsage
cloudwatch:GetMetricData · cloudwatch:ListMetrics
```

## License

MIT
