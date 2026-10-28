# Changelog

## [1.4.2] - 2026-10-28
### Changed
- Package metadata: the author email in `pyproject.toml` and the copyright holder in `LICENSE` now name Mor Michaeli instead of placeholders.
- **Python 3.10+ is now required.** Python 3.9 is end-of-life and no longer available on GitHub's Ubuntu runners, so it was dropped from `requires-python` and the CI matrix.
### Fixed
- CI now runs on `v*` tag pushes, so the PyPI publish job can actually trigger. Previously it was gated on a tag ref but the workflow only ran on branch pushes.

## [1.4.1] - 2026-08-28
### Fixed
- A scan could hang indefinitely on `Collecting Bedrock models/throughput …`. Bedrock's control-plane list calls are now bounded by a 10-second wall-clock budget: if the budget is spent the collector is abandoned, the run prints `timed out after 10s – skipped`, and the scan continues with the next collector. The Bedrock client also gets explicit botocore connect/read timeouts (5s, 2 attempts) so the abandoned call dies promptly instead of retrying against the 60s default.
- Collectors can no longer take the run down with them. An unexpected exception from any collector now degrades to zero rows plus a note, the same as a timeout.
### Notes
- Timeouts are opt-in per service via `COLLECTOR_TIMEOUTS`, and only Bedrock has one. Collectors that are legitimately slow on large accounts (paginating Lambda, DynamoDB, …) stay unbounded.

## [1.4.0] - 2026-08-20
### Changed
- **AI/ML services are now scanned by default** on both `inventory` and `run`. Previously they required `--ai`. Pass the new `--no-ai` to skip them. `--ai` is still accepted and is now a no-op, so existing scripts keep working.
- `run_inventory()` now defaults to `include_ai=True`, matching the CLI. Library callers relying on the old default should pass `include_ai=False`.
- The startup banner no longer prints `AI/ML services: ENABLED` on every run; it prints `AI/ML services: SKIPPED (--no-ai)` when they are turned off.
### Added
- Collectors are skipped in regions where the service has no endpoint, reported as `Collecting Kendra indexes … skipped (no endpoint in eu-north-1)`. Calling a service in a region where it doesn't exist takes ~9s to fail (the hostname doesn't resolve and botocore retries); across a full `--all-regions` scan that was ~39 minutes of dead waiting, which is what made AI/ML too expensive to enable by default. The check reads botocore's bundled endpoint data, spans all partitions (so GovCloud and China regions resolve correctly), and fails open — a service unknown to your installed boto3 is scanned rather than silently dropped.
- All 12 AI/ML collectors now record resource ARNs, so `--tags`, `--tags-wide`, and `--cost-allocation-tags` work for AI/ML resources instead of returning empty tags. 15 call sites pass through the ARN already in the API response; 6 that return no ARN (Rekognition collections, Textract adapters, Lex bots, Kendra indices, Transcribe vocabularies and language models) construct it from account, region, and name.
- First test coverage for AI/ML scanning and the CLI flag plumbing, including round-trip tests that catch a `--no-ai` being silently dropped between the two argument parsers.

## [1.3.0] - 2026-08-19
### Added
- `--billing` — pull Cost Explorer data for the trailing 90 days at daily granularity, grouped by AWS service, and write it to a CSV (`AccountID,Date,Service,Amount,Currency`). Available on the `inventory` and `run` sub-commands.
- `aws-radar billing` — the same report as a standalone sub-command, for when you only want costs and not a resource scan.
- `--billing-days` / `--days` to change the window, `--billing-metric` / `--metric` to pick the Cost Explorer metric (`UnblendedCost` by default; `AmortizedCost`, `BlendedCost`, `NetUnblendedCost`, `NetAmortizedCost`, and `UsageQuantity` also accepted), `--billing-output` / `--output` for the CSV path, and `--include-zero` to keep zero-cost buckets.
- A summary table of the top 20 services by spend is printed alongside the CSV.
### Fixed
- `aws-radar diagram` crashed with `TypeError: SubElement() got multiple values for argument 'parent'` before writing any output. draw.io cells carry a `parent` attribute, which collided with `ElementTree.SubElement`'s own first parameter; attributes are now passed through the `attrib` dict.

## [1.2.0] - 2026-06-30
### Added
- `--tags` — fetch resource tags in bulk per region via the Resource Groups Tagging API and join them onto each resource by ARN, adding a serialized `Tags` column (`key=value;…`). S3 buckets (global) are fetched separately via `GetBucketTagging`.
- `--tags-wide` — also write a second `*-wide.csv` with one `tag:<Key>` column per distinct tag key (implies `--tags`).
- `--cost-allocation-tags` — add a `CostAllocTags` column showing each resource's billing-activated tags, read once from Cost Explorer (implies `--tags`).
- `--include-aws-tags` — include `aws:`-prefixed system tags (excluded by default).
- All four flags are available on both the `inventory` and `run` sub-commands.
### Changed
- Each resource row now carries its ARN internally for tag joins; collectors capture the real ARN where available and construct it for EC2/S3/SQS.

## [1.1.2] - 2026-06-29
### Fixed
- `safe_call` now handles `EndpointConnectionError` and other `BotoCoreError` connectivity failures, so a service that isn't available in the scanned region (e.g. Lex V2 in `us-east-2`) is skipped instead of crashing the entire inventory run with a traceback.

## [1.1.1] - 2026-06-29
### Added
- Diagram connections for AI/ML services — Bedrock, SageMaker, Rekognition, Textract, Comprehend, Lex, Kendra, Transcribe, Polly, Translate, Forecast, and Personalize are now wired into the architecture (compute → inference, AI/ML → S3) instead of rendering as floating icons.
### Changed
- A missing `boto3` now prints an actionable install hint instead of a raw `ModuleNotFoundError` traceback.
- Documented the AWS CloudShell `boto3` install gotcha in the README.

## [1.1.0] - 2026-06-29
### Changed
- Added the `Intended Audience :: Science/Research` Trove classifier.
- Synced `__version__` in the package with the project version.

## [1.0.1] - 2026-06-03
### Fixed
- `aws-radar run --ai` now forwards the `--ai` flag to the inventory step; previously it was accepted but silently ignored, so AI/ML services were never scanned in the one-shot command.

## [1.0.0] - 2026-06-03
### Added
- `--ai` flag to scan AI/ML services (Bedrock, SageMaker, Rekognition, Textract, Comprehend, Lex, Kendra, Transcribe, Polly, Translate, Forecast, Personalize).
- Command-line options documented in the README.
### Changed
- Marked the project as Production/Stable.

## [0.1.1] - 2026-05-04
### Fixed
- Lambda collection now handles functions that are missing the `Runtime` attribute without crashing.

## [0.1.0] - 2026-04-20
### Added
- `aws-radar inventory` — scan EC2, RDS, Lambda, ECS, EKS, ElastiCache, DynamoDB, S3, OpenSearch, SQS, SNS, ALB
- `aws-radar diagram` — generate draw.io XML from inventory CSV
- `aws-radar run` — one-shot inventory + diagram
- AWS SSO support via `--profile`
- IAM role assumption via `--role-arn`
- Multi-region support via `--all-regions`
- AccountID column in all CSV output
