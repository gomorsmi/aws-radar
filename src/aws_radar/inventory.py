#!/usr/bin/env python3
"""
AWS Resource Inventory Script
Lists all running/active services across your AWS account:
EC2, RDS, Lambda, ECS, EKS, ElastiCache, OpenSearch, DynamoDB, S3, and more.

Requirements:
    pip install boto3 rich

Usage:
    python aws_inventory.py                        # uses default AWS profile/region
    python aws_inventory.py --region us-west-2
    python aws_inventory.py --profile my-profile
    python aws_inventory.py --all-regions
    python aws_inventory.py --export inventory.csv
"""

import argparse
import csv
import sys
import threading
from datetime import datetime

try:
    import boto3
    from botocore.config import Config
    from botocore.exceptions import (
        ClientError, NoCredentialsError, EndpointConnectionError, BotoCoreError,
    )
except ImportError:
    sys.exit(
        "\nError: boto3 is not installed in this Python environment.\n\n"
        "Install it into the same environment as aws-radar:\n"
        "    python3 -m pip install --user boto3 rich\n\n"
        "On AWS CloudShell the system ships its own boto3, so a --user install of\n"
        "aws-radar may skip it; the command above puts boto3 alongside aws-radar.\n"
        "Alternatively, install everything in an isolated virtual environment:\n"
        "    python3 -m venv ~/aws-radar-venv\n"
        "    ~/aws-radar-venv/bin/pip install aws-radar\n"
    )

try:
    from rich.console import Console
    from rich.table import Table
    from rich import box
    RICH_AVAILABLE = True
except ImportError:
    RICH_AVAILABLE = False

console = Console() if RICH_AVAILABLE else None

COLS = ["AccountID", "Service", "Name", "ID", "Type/Size", "Status", "Region", "Extra"]


# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def get_tag(tags, key="Name"):
    if not tags:
        return "-"
    for t in tags:
        if t.get("Key") == key:
            return t.get("Value", "-")
    return "-"

def safe_call(fn, *args, default=None, **kwargs):
    try:
        return fn(*args, **kwargs)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "UnauthorizedOperation", "AccessDeniedException"):
            print(f"  [!] Access denied: {fn.__self__._service_model.service_name} – skipping")
        else:
            print(f"  [!] Error ({code}): {e}")
        return default
    except EndpointConnectionError:
        # Service has no endpoint in this region (e.g. Lex V2 in an unsupported
        # region) – the hostname won't resolve. Skip rather than crash the run.
        print(f"  [!] {fn.__self__._service_model.service_name} not available in this region – skipping")
        return default
    except BotoCoreError as e:
        # Any other connectivity/SDK-level failure (timeouts, etc.) – skip this call.
        print(f"  [!] {type(e).__name__} on {fn.__self__._service_model.service_name} – skipping")
        return default

def row(account_id, service, name, rid, type_size, status, region, extra, arn=None):
    return {
        "AccountID": account_id,
        "Service":   service,
        "Name":      name,
        "ID":        rid,
        "Type/Size": type_size,
        "Status":    status,
        "Region":    region,
        "Extra":     extra,
        # Internal join key for --tags (matched against the Resource Groups
        # Tagging API). Not part of COLS, so it is never displayed/exported.
        "_ARN":      arn,
    }


# ─────────────────────────────────────────────
# Collectors
# ─────────────────────────────────────────────

def collect_ec2(session, region, account_id):
    ec2 = session.client("ec2", region_name=region)
    rows = []
    resp = safe_call(ec2.describe_instances,
                     Filters=[{"Name": "instance-state-name", "Values": ["running", "stopped", "pending"]}],
                     default={})
    for r in resp.get("Reservations", []):
        for i in r["Instances"]:
            rows.append(row(account_id, "EC2",
                get_tag(i.get("Tags")), i["InstanceId"], i["InstanceType"],
                i["State"]["Name"], region,
                i.get("PublicIpAddress", i.get("PrivateIpAddress", "-")),
                arn=f"arn:aws:ec2:{region}:{account_id}:instance/{i['InstanceId']}"))
    return rows


def collect_rds(session, region, account_id):
    rds = session.client("rds", region_name=region)
    rows = []
    resp = safe_call(rds.describe_db_instances, default={})
    for db in resp.get("DBInstances", []):
        rows.append(row(account_id, "RDS",
            db["DBInstanceIdentifier"], db["DBInstanceIdentifier"],
            db["DBInstanceClass"], db["DBInstanceStatus"], region,
            f"{db['Engine']} {db.get('EngineVersion', '')}",
            arn=db.get("DBInstanceArn")))
    resp2 = safe_call(rds.describe_db_clusters, default={})
    for cl in resp2.get("DBClusters", []):
        rows.append(row(account_id, "RDS Cluster",
            cl["DBClusterIdentifier"], cl["DBClusterIdentifier"],
            f"{len(cl.get('DBClusterMembers', []))} members", cl["Status"], region,
            f"{cl['Engine']} {cl.get('EngineVersion', '')}",
            arn=cl.get("DBClusterArn")))
    return rows


def collect_lambda(session, region, account_id):
    lmb = session.client("lambda", region_name=region)
    rows = []
    try:
        paginator = lmb.get_paginator("list_functions")
        for page in paginator.paginate():
            for fn in page["Functions"]:
                runtime = fn.get("Runtime", "-")
                memory = fn.get("MemorySize")
                if memory is None:
                    type_size = runtime
                else:
                    type_size = f"{runtime} / {memory}MB"
                last_modified = fn.get("LastModified", "-")
                if isinstance(last_modified, str):
                    last_modified = last_modified[:10]
                else:
                    last_modified = "-"
                rows.append(row(account_id, "Lambda",
                    fn["FunctionName"], fn["FunctionArn"].split(":")[-1],
                    type_size,
                    fn.get("State", "Active"), region,
                    f"Last modified: {last_modified}",
                    arn=fn.get("FunctionArn")))
    except ClientError as e:
        code = e.response["Error"]["Code"]
        if code in ("AccessDenied", "UnauthorizedOperation", "AccessDeniedException"):
            print(f"  [!] Lambda access denied in {region} – skipping")
        else:
            print(f"  [!] Lambda error in {region}: {code} – {e.response['Error']['Message']}")
    except Exception as e:
        print(f"  [!] Unexpected Lambda error in {region}: {e}")
    return rows


def collect_ecs(session, region, account_id):
    ecs = session.client("ecs", region_name=region)
    rows = []
    clusters = safe_call(ecs.list_clusters, default={}).get("clusterArns", [])
    if clusters:
        detail = safe_call(ecs.describe_clusters, clusters=clusters, default={})
        for cl in detail.get("clusters", []):
            rows.append(row(account_id, "ECS Cluster",
                cl["clusterName"], cl["clusterArn"].split("/")[-1],
                f"{cl.get('registeredContainerInstancesCount', 0)} nodes",
                cl["status"], region,
                f"Running tasks: {cl.get('runningTasksCount', 0)}",
                arn=cl.get("clusterArn")))
    for carn in clusters:
        cname = carn.split("/")[-1]
        svcs = safe_call(ecs.list_services, cluster=carn, default={}).get("serviceArns", [])
        if svcs:
            svc_detail = safe_call(ecs.describe_services, cluster=carn, services=svcs, default={})
            for svc in svc_detail.get("services", []):
                rows.append(row(account_id, "ECS Service",
                    svc["serviceName"], svc["serviceArn"].split("/")[-1],
                    f"{svc.get('desiredCount', 0)} desired", svc["status"], region,
                    f"Running: {svc.get('runningCount', 0)} / Cluster: {cname}",
                    arn=svc.get("serviceArn")))
    return rows


def collect_eks(session, region, account_id):
    eks = session.client("eks", region_name=region)
    rows = []
    clusters = safe_call(eks.list_clusters, default={}).get("clusters", [])
    for name in clusters:
        cl = safe_call(eks.describe_cluster, name=name, default={}).get("cluster", {})
        rows.append(row(account_id, "EKS",
            cl.get("name", name), cl.get("name", name),
            cl.get("version", "-"), cl.get("status", "-"), region,
            (cl.get("endpoint", "-")[:50] if cl.get("endpoint") else "-"),
            arn=cl.get("arn")))
    return rows


def collect_elasticache(session, region, account_id):
    ec = session.client("elasticache", region_name=region)
    rows = []
    resp = safe_call(ec.describe_cache_clusters, ShowCacheNodeInfo=True, default={})
    for cl in resp.get("CacheClusters", []):
        rows.append(row(account_id, "ElastiCache",
            cl["CacheClusterId"], cl["CacheClusterId"],
            cl["CacheNodeType"], cl["CacheClusterStatus"], region,
            f"{cl['Engine']} {cl.get('EngineVersion', '')}",
            arn=cl.get("ARN")))
    resp2 = safe_call(ec.describe_replication_groups, default={})
    for rg in resp2.get("ReplicationGroups", []):
        rows.append(row(account_id, "ElastiCache RG",
            rg["ReplicationGroupId"], rg["ReplicationGroupId"],
            f"{len(rg.get('MemberClusters', []))} members", rg["Status"], region,
            rg.get("Description", "-")[:40],
            arn=rg.get("ARN")))
    return rows


def collect_dynamodb(session, region, account_id):
    ddb = session.client("dynamodb", region_name=region)
    rows = []
    paginator = ddb.get_paginator("list_tables")
    try:
        for page in paginator.paginate():
            for tbl in page["TableNames"]:
                detail = safe_call(ddb.describe_table, TableName=tbl, default={}).get("Table", {})
                rows.append(row(account_id, "DynamoDB",
                    tbl, tbl,
                    str(detail.get("TableSizeBytes", "-")),
                    detail.get("TableStatus", "-"), region,
                    f"Items: {detail.get('ItemCount', '-')}",
                    arn=detail.get("TableArn")))
    except ClientError as e:
        print(f"  [!] DynamoDB error: {e}")
    return rows


def collect_s3(session, account_id):
    s3 = session.client("s3")
    rows = []
    resp = safe_call(s3.list_buckets, default={})
    for b in resp.get("Buckets", []):
        rows.append(row(account_id, "S3",
            b["Name"], b["Name"], "-", "active", "global",
            f"Created: {b['CreationDate'].strftime('%Y-%m-%d')}",
            arn=f"arn:aws:s3:::{b['Name']}"))
    return rows


def collect_opensearch(session, region, account_id):
    os_client = session.client("opensearch", region_name=region)
    rows = []
    resp = safe_call(os_client.list_domain_names, default={})
    for d in resp.get("DomainNames", []):
        detail = safe_call(os_client.describe_domain, DomainName=d["DomainName"], default={}).get("DomainStatus", {})
        rows.append(row(account_id, "OpenSearch",
            d["DomainName"], d["DomainName"],
            detail.get("ClusterConfig", {}).get("InstanceType", "-"),
            "active" if detail.get("Created") else "creating", region,
            f"Engine: {detail.get('EngineVersion', '-')}",
            arn=detail.get("ARN")))
    return rows


def collect_sqs(session, region, account_id):
    sqs = session.client("sqs", region_name=region)
    rows = []
    resp = safe_call(sqs.list_queues, default={})
    for url in resp.get("QueueUrls", []):
        name = url.split("/")[-1]
        rows.append(row(account_id, "SQS",
            name, name,
            "FIFO" if name.endswith(".fifo") else "Standard",
            "active", region, url,
            arn=f"arn:aws:sqs:{region}:{account_id}:{name}"))
    return rows


def collect_sns(session, region, account_id):
    sns = session.client("sns", region_name=region)
    rows = []
    paginator = sns.get_paginator("list_topics")
    try:
        for page in paginator.paginate():
            for t in page["Topics"]:
                arn = t["TopicArn"]
                name = arn.split(":")[-1]
                rows.append(row(account_id, "SNS",
                    name, name, "-", "active", region, arn, arn=arn))
    except ClientError:
        pass
    return rows


def collect_alb(session, region, account_id):
    elb = session.client("elbv2", region_name=region)
    rows = []
    resp = safe_call(elb.describe_load_balancers, default={})
    for lb in resp.get("LoadBalancers", []):
        rows.append(row(account_id, "Load Balancer",
            lb["LoadBalancerName"], lb["LoadBalancerArn"].split("/")[-2],
            lb["Type"].upper(), lb["State"]["Code"], region,
            lb.get("DNSName", "-")[:50],
            arn=lb.get("LoadBalancerArn")))
    return rows


# ─────────────────────────────────────────────
# AI / ML Service Collectors
# ─────────────────────────────────────────────

# Bedrock's control-plane list calls have been observed hanging well past the
# 60s botocore default. Bound each request so an abandoned call (see
# run_collector) actually dies instead of retrying in the background.
BEDROCK_CONFIG = Config(connect_timeout=5, read_timeout=5,
                        retries={"max_attempts": 2})


def collect_bedrock(session, region, account_id):
    """Amazon Bedrock — foundation model access."""
    rows = []
    try:
        br = session.client("bedrock", region_name=region, config=BEDROCK_CONFIG)
        # Custom model fine-tunes
        resp = safe_call(br.list_custom_models, default={})
        for m in resp.get("modelSummaries", []):
            rows.append(row(account_id, "Bedrock Model",
                m["modelName"], m["modelArn"].split("/")[-1],
                m.get("baseModelId", "-"), m.get("modelStatus", "active"),
                region, f"Base: {m.get('baseModelId','-')}",
                arn=m.get("modelArn")))
        # Provisioned throughput
        resp2 = safe_call(br.list_provisioned_model_throughputs, default={})
        for pt in resp2.get("provisionedModelSummaries", []):
            rows.append(row(account_id, "Bedrock Throughput",
                pt["provisionedModelName"], pt["provisionedModelArn"].split("/")[-1],
                f"{pt.get('desiredModelUnits','-')} units",
                pt.get("status", "active"), region,
                pt.get("foundationModelArn", "-").split("/")[-1],
                arn=pt.get("provisionedModelArn")))
        # Inference profiles
        resp3 = safe_call(br.list_inference_profiles, default={})
        for ip in resp3.get("inferenceProfileSummaries", []):
            rows.append(row(account_id, "Bedrock Profile",
                ip["inferenceProfileName"], ip["inferenceProfileId"],
                ip.get("type", "-"), ip.get("status", "ACTIVE"),
                region, ip.get("description", "-")[:40],
                arn=ip.get("inferenceProfileArn")))
    except Exception:
        pass
    return rows


def collect_sagemaker(session, region, account_id):
    """SageMaker — endpoints, training jobs, notebooks, pipelines."""
    sm = session.client("sagemaker", region_name=region)
    rows = []

    # Endpoints (deployed models)
    resp = safe_call(sm.list_endpoints, StatusEquals="InService", default={})
    for ep in resp.get("Endpoints", []):
        rows.append(row(account_id, "SageMaker Endpoint",
            ep["EndpointName"], ep["EndpointName"],
            "-", ep["EndpointStatus"], region,
            f"Created: {ep['CreationTime'].strftime('%Y-%m-%d')}",
            arn=ep.get("EndpointArn")))

    # Notebook instances
    resp2 = safe_call(sm.list_notebook_instances, default={})
    for nb in resp2.get("NotebookInstances", []):
        rows.append(row(account_id, "SageMaker Notebook",
            nb["NotebookInstanceName"], nb["NotebookInstanceName"],
            nb.get("InstanceType", "-"), nb["NotebookInstanceStatus"],
            region, nb.get("Url", "-"),
            arn=nb.get("NotebookInstanceArn")))

    # Training jobs (last 20 non-completed)
    resp3 = safe_call(sm.list_training_jobs,
                      StatusEquals="InProgress", MaxResults=20, default={})
    for tj in resp3.get("TrainingJobSummaries", []):
        rows.append(row(account_id, "SageMaker Training",
            tj["TrainingJobName"], tj["TrainingJobName"],
            "-", tj["TrainingJobStatus"], region,
            f"Created: {tj['CreationTime'].strftime('%Y-%m-%d')}",
            arn=tj.get("TrainingJobArn")))

    # Pipelines
    resp4 = safe_call(sm.list_pipelines, default={})
    for pl in resp4.get("PipelineSummaries", []):
        rows.append(row(account_id, "SageMaker Pipeline",
            pl["PipelineName"], pl["PipelineName"],
            "-", pl.get("PipelineStatus", "active"), region,
            pl.get("PipelineDescription", "-")[:40],
            arn=pl.get("PipelineArn")))

    # Feature groups
    resp5 = safe_call(sm.list_feature_groups, default={})
    for fg in resp5.get("FeatureGroupSummaries", []):
        rows.append(row(account_id, "SageMaker FeatureGroup",
            fg["FeatureGroupName"], fg["FeatureGroupName"],
            "-", fg.get("FeatureGroupStatus", "active"), region,
            f"Created: {fg['CreationTime'].strftime('%Y-%m-%d')}",
            arn=fg.get("FeatureGroupArn")))

    return rows


def collect_rekognition(session, region, account_id):
    """Amazon Rekognition — custom collections and projects."""
    rek = session.client("rekognition", region_name=region)
    rows = []

    resp = safe_call(rek.list_collections, default={})
    for col_id in resp.get("CollectionIds", []):
        rows.append(row(account_id, "Rekognition Collection",
            col_id, col_id, "-", "active", region, "-",
            arn=f"arn:aws:rekognition:{region}:{account_id}:collection/{col_id}"))

    resp2 = safe_call(rek.describe_projects, default={})
    for proj in resp2.get("ProjectDescriptions", []):
        name = proj["ProjectArn"].split("/")[-1]
        rows.append(row(account_id, "Rekognition Project",
            name, name, "-", proj.get("Status", "active"), region,
            f"Created: {proj['CreationTimestamp'].strftime('%Y-%m-%d')}",
            arn=proj.get("ProjectArn")))

    return rows


def collect_textract(session, region, account_id):
    """Amazon Textract — adapter versions (custom models)."""
    rows = []
    try:
        tx = session.client("textract", region_name=region)
        resp = safe_call(tx.list_adapters, default={})
        for adapter in resp.get("Adapters", []):
            rows.append(row(account_id, "Textract Adapter",
                adapter["AdapterName"], adapter["AdapterId"],
                "-", "active", region,
                f"Created: {adapter['CreationTime'].strftime('%Y-%m-%d')}",
                arn=f"arn:aws:textract:{region}:{account_id}:adapter/{adapter['AdapterId']}"))
    except Exception:
        pass
    return rows


def collect_comprehend(session, region, account_id):
    """Amazon Comprehend — custom classifiers and entity recognizers."""
    comp = session.client("comprehend", region_name=region)
    rows = []

    resp = safe_call(comp.list_document_classifiers, default={})
    for clf in resp.get("DocumentClassifierPropertiesList", []):
        name = clf["DocumentClassifierArn"].split("/")[-1]
        rows.append(row(account_id, "Comprehend Classifier",
            name, name, "-", clf.get("Status", "active"), region,
            clf.get("LanguageCode", "-"),
            arn=clf.get("DocumentClassifierArn")))

    resp2 = safe_call(comp.list_entity_recognizers, default={})
    for er in resp2.get("EntityRecognizerPropertiesList", []):
        name = er["EntityRecognizerArn"].split("/")[-1]
        rows.append(row(account_id, "Comprehend Recognizer",
            name, name, "-", er.get("Status", "active"), region,
            er.get("LanguageCode", "-"),
            arn=er.get("EntityRecognizerArn")))

    return rows


def collect_lex(session, region, account_id):
    """Amazon Lex v2 — bots and bot aliases."""
    lex = session.client("lexv2-models", region_name=region)
    rows = []

    resp = safe_call(lex.list_bots, default={})
    for bot in resp.get("botSummaries", []):
        rows.append(row(account_id, "Lex Bot",
            bot["botName"], bot["botId"],
            "-", bot.get("botStatus", "Available"), region,
            f"Updated: {bot['lastUpdatedDateTime'].strftime('%Y-%m-%d')}",
            arn=f"arn:aws:lex:{region}:{account_id}:bot/{bot['botId']}"))

    return rows


def collect_kendra(session, region, account_id):
    """Amazon Kendra — enterprise search indexes."""
    rows = []
    try:
        kendra = session.client("kendra", region_name=region)
        resp = safe_call(kendra.list_indices, default={})
        for idx in resp.get("IndexConfigurationSummaryItems", []):
            rows.append(row(account_id, "Kendra Index",
                idx["Name"], idx["Id"],
                idx.get("Edition", "-"), idx.get("Status", "active"),
                region, f"Updated: {idx['UpdatedAt'].strftime('%Y-%m-%d')}",
                arn=f"arn:aws:kendra:{region}:{account_id}:index/{idx['Id']}"))
    except Exception:
        pass
    return rows


def collect_transcribe(session, region, account_id):
    """Amazon Transcribe — custom vocabularies and language models."""
    trans = session.client("transcribe", region_name=region)
    rows = []

    resp = safe_call(trans.list_vocabularies, default={})
    for v in resp.get("Vocabularies", []):
        rows.append(row(account_id, "Transcribe Vocabulary",
            v["VocabularyName"], v["VocabularyName"],
            v.get("LanguageCode", "-"), v.get("VocabularyState", "active"),
            region, f"Modified: {v['LastModifiedTime'].strftime('%Y-%m-%d')}",
            arn=f"arn:aws:transcribe:{region}:{account_id}:vocabulary/{v['VocabularyName']}"))

    resp2 = safe_call(trans.list_language_models, default={})
    for lm in resp2.get("Models", []):
        rows.append(row(account_id, "Transcribe Model",
            lm["ModelName"], lm["ModelName"],
            lm.get("LanguageCode", "-"), lm.get("ModelStatus", "active"),
            region, "-",
            arn=f"arn:aws:transcribe:{region}:{account_id}:language-model/{lm['ModelName']}"))

    return rows


def collect_polly(session, region, account_id):
    """Amazon Polly — custom lexicons."""
    rows = []
    try:
        polly = session.client("polly", region_name=region)
        resp = safe_call(polly.list_lexicons, default={})
        for lex in resp.get("Lexicons", []):
            rows.append(row(account_id, "Polly Lexicon",
                lex["Name"], lex["Name"],
                "-", "active", region,
                f"Modified: {lex['Attributes']['LastModified'].strftime('%Y-%m-%d')}",
                arn=lex.get("Attributes", {}).get("LexiconArn")))
    except Exception:
        pass
    return rows


def collect_translate(session, region, account_id):
    """Amazon Translate — custom terminologies."""
    rows = []
    try:
        tr = session.client("translate", region_name=region)
        resp = safe_call(tr.list_terminologies, default={})
        for t in resp.get("TerminologyPropertiesList", []):
            rows.append(row(account_id, "Translate Terminology",
                t["Name"], t["Name"],
                "-", "active", region,
                f"Terms: {t.get('TermCount', '-')}",
                arn=t.get("Arn")))
    except Exception:
        pass
    return rows


def collect_forecast(session, region, account_id):
    """Amazon Forecast — datasets, predictors, forecasts."""
    rows = []
    try:
        fc = session.client("forecast", region_name=region)
        resp = safe_call(fc.list_predictors, default={})
        for p in resp.get("Predictors", []):
            rows.append(row(account_id, "Forecast Predictor",
                p["PredictorName"], p["PredictorArn"].split("/")[-1],
                "-", p.get("Status", "active"), region,
                f"Created: {p['CreationTime'].strftime('%Y-%m-%d')}",
                arn=p.get("PredictorArn")))
    except Exception:
        pass
    return rows


def collect_personalize(session, region, account_id):
    """Amazon Personalize — campaigns and solutions."""
    rows = []
    try:
        per = session.client("personalize", region_name=region)
        resp = safe_call(per.list_campaigns, default={})
        for c in resp.get("campaigns", []):
            rows.append(row(account_id, "Personalize Campaign",
                c["name"], c["campaignArn"].split("/")[-1],
                "-", c.get("status", "active"), region,
                f"Updated: {c['lastUpdatedDateTime'].strftime('%Y-%m-%d')}",
                arn=c.get("campaignArn")))
    except Exception:
        pass
    return rows


# ─────────────────────────────────────────────
# Display & Export
# ─────────────────────────────────────────────

def display_table(rows, title="AWS Resource Inventory", extra_cols=None):
    if not rows:
        print("\nNo resources found.\n")
        return

    columns = COLS + list(extra_cols or [])
    status_color = {
        "running": "green", "active": "green", "available": "green",
        "stopped": "red", "failed": "red",
        "pending": "yellow", "creating": "yellow", "modifying": "yellow",
    }

    if RICH_AVAILABLE:
        tbl = Table(title=title, box=box.ROUNDED, show_lines=False,
                    header_style="bold cyan", title_style="bold white on blue")
        for col in columns:
            tbl.add_column(col, overflow="fold")
        for r in rows:
            cells = []
            for c in columns:
                val = str(r.get(c, ""))
                if c == "Status":
                    val = f"[{status_color.get(val.lower(), 'white')}]{val}[/]"
                cells.append(val)
            tbl.add_row(*cells)
        console.print()
        console.print(tbl)
        console.print(f"\n[bold]Total resources: {len(rows)}[/bold]\n")
    else:
        widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0)) for c in columns}
        sep = "+-" + "-+-".join("-" * widths[c] for c in columns) + "-+"
        fmt = "| " + " | ".join(f"{{:<{widths[c]}}}" for c in columns) + " |"
        print(f"\n{'='*10} {title} {'='*10}")
        print(sep)
        print(fmt.format(*columns))
        print(sep)
        for r in rows:
            print(fmt.format(*[str(r.get(c, "")) for c in columns]))
        print(sep)
        print(f"\nTotal resources: {len(rows)}\n")


def export_csv(rows, path, extra_cols=None):
    if not rows:
        print("Nothing to export.")
        return
    cols = COLS + list(extra_cols or [])
    with open(path, "w", newline="") as f:
        # extrasaction="ignore" drops internal keys (_ARN, _tags) that aren't columns.
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    print(f"Exported {len(rows)} rows to {path}")


def export_csv_wide(rows, path):
    """Write a CSV with one ``tag:<Key>`` column per distinct tag key (sparse)."""
    if not rows:
        print("Nothing to export.")
        return
    keys = sorted({k for r in rows for k in r.get("_tags", {})})
    cols = COLS + [f"tag:{k}" for k in keys]
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            tags = r.get("_tags", {})
            w.writerow([str(r.get(c, "")) for c in COLS] + [tags.get(k, "") for k in keys])
    print(f"Exported {len(rows)} rows × {len(keys)} tag column(s) to {path}")


def attach_tags(rows, session, regions, account_id,
                want_cost_alloc=False, include_aws=False):
    """Fetch tags in bulk and merge them onto each row.

    Adds ``_tags`` (dict) plus the display columns ``Tags`` and, when
    *want_cost_alloc*, ``CostAllocTags`` to every row.
    """
    from aws_radar import tags as tagmod

    print("\nFetching resource tags …")
    arn_tags = {}
    for region in regions:
        arn_tags.update(tagmod.fetch_region_tags(session, region))

    # S3 buckets are global — the regional Tagging API won't return them.
    for r in rows:
        if r["Service"] == "S3" and r.get("_ARN") not in arn_tags:
            bt = tagmod.fetch_s3_bucket_tags(session, r["ID"])
            if bt:
                arn_tags[r["_ARN"]] = bt

    cost_keys = tagmod.fetch_cost_allocation_keys(session) if want_cost_alloc else set()

    for r in rows:
        raw = tagmod.filter_tags(arn_tags.get(r.get("_ARN") or "", {}), include_aws=include_aws)
        r["_tags"] = raw
        r["Tags"] = tagmod.tags_to_string(raw)
        if want_cost_alloc:
            r["CostAllocTags"] = tagmod.tags_to_string(
                {k: v for k, v in raw.items() if k in cost_keys})

    tagged = sum(1 for r in rows if r.get("_tags"))
    print(f"  {tagged}/{len(rows)} resources have tags")
    return rows


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def get_all_regions(session):
    ec2 = session.client("ec2", region_name="us-east-1")
    resp = ec2.describe_regions(Filters=[{"Name": "opt-in-status", "Values": ["opt-in-not-required", "opted-in"]}])
    return [r["RegionName"] for r in resp["Regions"]]


# Every partition botocore knows about. get_available_regions() defaults to
# "aws" alone, which would make every service look absent to a GovCloud or
# China caller and skip the entire scan.
_PARTITIONS = ("aws", "aws-cn", "aws-us-gov",
               "aws-iso", "aws-iso-b", "aws-iso-e", "aws-iso-f")

_SERVICE_REGIONS = {}   # boto3 service id → set of regions with an endpoint


def service_regions(session, service):
    """Regions where *service* has an endpoint, unioned across all partitions."""
    if service not in _SERVICE_REGIONS:
        regs = set()
        for part in _PARTITIONS:
            try:
                regs |= set(session.get_available_regions(service, partition_name=part))
            except Exception:
                pass
        _SERVICE_REGIONS[service] = regs
    return _SERVICE_REGIONS[service]


def service_available(session, service, region):
    """Whether it's worth calling *service* in *region*.

    Calling a service in a region where it has no endpoint costs ~9s: the
    hostname doesn't resolve and botocore retries before giving up. Across a
    full --all-regions scan that is ~39 minutes of pure waiting, which is what
    made AI/ML collection too expensive to enable by default.

    Fails open — an empty region set (unknown or brand-new service, or endpoint
    data older than the service) means "scan it anyway". A stale bundled
    endpoint list can then only cost time, never hide a resource; safe_call's
    EndpointConnectionError handling remains the backstop.
    """
    regs = service_regions(session, service)
    return not regs or region in regs


# Wall-clock budget per collector, keyed by boto3 service id. A service absent
# from this map runs unbounded, which is the right default for collectors that
# legitimately take a while on large accounts (paginating Lambda, DynamoDB, …).
COLLECTOR_TIMEOUTS = {"bedrock": 10}


def run_collector(fn, session, region, account_id, timeout=None):
    """Run one collector. Returns ``(rows, note)``; *note* is None on success.

    A collector must never take the run down with it, so both a hang and an
    unexpected exception degrade to "no rows, carry on". When *timeout* is set
    the work runs on a daemon thread and is abandoned once the budget is spent
    — daemon so a stuck call can't block interpreter exit, and the per-client
    botocore timeouts (see BEDROCK_CONFIG) keep the orphan short-lived.
    """
    if timeout is None:
        try:
            return fn(session, region, account_id), None
        except Exception as e:
            return [], f"error: {type(e).__name__} – skipped"

    box = {}

    def worker():
        try:
            box["rows"] = fn(session, region, account_id)
        except Exception as e:
            box["error"] = e

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    t.join(timeout)

    if t.is_alive():
        return [], f"timed out after {timeout}s – skipped"
    if "error" in box:
        return [], f"error: {type(box['error']).__name__} – skipped"
    return box.get("rows", []), None


# (label, boto3 service id, collector) — the service id drives the endpoint
# pre-check in run_inventory.
CORE_COLLECTORS = [
    ("EC2 instances",     "ec2",         collect_ec2),
    ("RDS databases",     "rds",         collect_rds),
    ("Lambda functions",  "lambda",      collect_lambda),
    ("ECS clusters/svcs", "ecs",         collect_ecs),
    ("EKS clusters",      "eks",         collect_eks),
    ("ElastiCache",       "elasticache", collect_elasticache),
    ("DynamoDB tables",   "dynamodb",    collect_dynamodb),
    ("OpenSearch",        "opensearch",  collect_opensearch),
    ("SQS queues",        "sqs",         collect_sqs),
    ("SNS topics",        "sns",         collect_sns),
    ("Load Balancers",    "elbv2",       collect_alb),
]

AI_COLLECTORS = [
    ("Bedrock models/throughput",  "bedrock",      collect_bedrock),
    ("SageMaker",                  "sagemaker",    collect_sagemaker),
    ("Rekognition",                "rekognition",  collect_rekognition),
    ("Textract adapters",          "textract",     collect_textract),
    ("Comprehend",                 "comprehend",   collect_comprehend),
    ("Lex bots",                   "lexv2-models", collect_lex),
    ("Kendra indexes",             "kendra",       collect_kendra),
    ("Transcribe",                 "transcribe",   collect_transcribe),
    ("Polly lexicons",             "polly",        collect_polly),
    ("Translate terminologies",    "translate",    collect_translate),
    ("Forecast predictors",        "forecast",     collect_forecast),
    ("Personalize campaigns",      "personalize",  collect_personalize),
]


def run_inventory(regions, session, account_id, include_ai=True,
                  include_tags=False, want_cost_alloc=False, include_aws_tags=False):
    all_rows = []
    print(f"\nScanning {len(regions)} region(s): {', '.join(regions)}")
    if not include_ai:
        print("  AI/ML services: SKIPPED (--no-ai)")
    if include_tags:
        print("  Resource tags: ENABLED")
    print()

    print("Collecting S3 buckets …")
    all_rows += collect_s3(session, account_id)

    collectors = CORE_COLLECTORS + (AI_COLLECTORS if include_ai else [])

    for region in regions:
        print(f"\n── Region: {region} ──")
        for label, service, fn in collectors:
            print(f"  Collecting {label} …", end=" ", flush=True)
            if not service_available(session, service, region):
                print(f"skipped (no endpoint in {region})")
                continue
            results, note = run_collector(
                fn, session, region, account_id,
                timeout=COLLECTOR_TIMEOUTS.get(service))
            print(note or f"{len(results)} found")
            all_rows += results

    if not any(r for r in all_rows if r.get("Region") != "global"):
        print("\n⚠️  No resources found in the scanned region(s).")
        print("   Tips:")
        print("   • Try --all-regions to scan every region")
        print("   • Your resources may be in a different region, e.g. --region eu-west-1")
        print("   • Check your IAM permissions include ReadOnlyAccess")

    if include_tags and all_rows:
        attach_tags(all_rows, session, regions, account_id,
                    want_cost_alloc=want_cost_alloc, include_aws=include_aws_tags)

    return all_rows


def main():
    parser = argparse.ArgumentParser(description="AWS Resource Inventory")
    parser.add_argument("--region",      default=None,  help="AWS region (default: boto3 default)")
    parser.add_argument("--profile",     default=None,  help="AWS SSO/named profile")
    parser.add_argument("--all-regions", action="store_true", help="Scan all enabled regions")
    parser.add_argument("--ai", action=argparse.BooleanOptionalAction, default=True,
                        help="Scan AI/ML services (Bedrock, SageMaker, Rekognition, Lex, "
                             "Kendra, …). On by default; pass --no-ai to skip them")
    parser.add_argument("--tags",        action="store_true",
                        help="Fetch resource tags and add a 'Tags' column")
    parser.add_argument("--tags-wide",   action="store_true",
                        help="Also write a CSV with one column per tag key (implies --tags)")
    parser.add_argument("--cost-allocation-tags", action="store_true",
                        help="Add a 'CostAllocTags' column with each resource's billing-activated tags (implies --tags)")
    parser.add_argument("--include-aws-tags", action="store_true",
                        help="Include aws:-prefixed system tags (excluded by default)")
    parser.add_argument("--billing",     action="store_true",
                        help="Also pull Cost Explorer daily cost by service and write a CSV")
    parser.add_argument("--billing-days", type=int, default=90,
                        help="Billing window in days (default: 90)")
    parser.add_argument("--billing-metric", default="UnblendedCost",
                        help="Cost Explorer metric (default: UnblendedCost)")
    parser.add_argument("--billing-output", metavar="FILE.csv", default="billing.csv",
                        help="Billing CSV path (default: billing.csv)")
    parser.add_argument("--include-zero", action="store_true",
                        help="Keep zero-cost service/day buckets in the billing CSV")
    parser.add_argument("--export",      metavar="FILE.csv", help="Export results to CSV")
    args = parser.parse_args()

    try:
        session = boto3.Session(profile_name=args.profile)
        sts = session.client("sts")
        identity = sts.get_caller_identity()
        account_id = identity["Account"]
        user = identity.get("Arn", "unknown")
        print(f"\n✓ Authenticated as: {user}")
        print(f"  Account ID : {account_id}")
    except NoCredentialsError:
        print("\n✗ No AWS credentials found.")
        print("  Configure with: aws configure  OR  set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n")
        sys.exit(1)
    except ClientError as e:
        print(f"\n✗ Auth error: {e}\n")
        sys.exit(1)

    if args.all_regions:
        regions = get_all_regions(session)
    else:
        region = args.region or session.region_name or "us-east-1"
        regions = [region]

    want_tags = args.tags or args.tags_wide or args.cost_allocation_tags

    start = datetime.now()
    rows = run_inventory(regions, session, account_id, include_ai=args.ai,
                         include_tags=want_tags,
                         want_cost_alloc=args.cost_allocation_tags,
                         include_aws_tags=args.include_aws_tags)
    elapsed = (datetime.now() - start).total_seconds()

    extra_cols = []
    if want_tags:
        extra_cols.append("Tags")
    if args.cost_allocation_tags:
        extra_cols.append("CostAllocTags")

    title = f"AWS Inventory – Account {account_id} – {datetime.now().strftime('%Y-%m-%d %H:%M')} ({elapsed:.1f}s)"
    display_table(rows, title=title, extra_cols=extra_cols)

    if args.export:
        export_csv(rows, args.export, extra_cols=extra_cols)
        if args.tags_wide:
            import os
            base, ext = os.path.splitext(args.export)
            wide_path = f"{base}-wide{ext or '.csv'}"
            export_csv_wide(rows, wide_path)

    if args.billing:
        from aws_radar.billing import run_billing
        run_billing(session, account_id,
                    days=args.billing_days,
                    metric=args.billing_metric,
                    output=args.billing_output,
                    include_zero=args.include_zero)


if __name__ == "__main__":
    main()
