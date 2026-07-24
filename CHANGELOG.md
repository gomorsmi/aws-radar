# Changelog

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
