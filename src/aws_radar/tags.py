"""
Tag collection for aws-radar.

Tags are fetched in bulk per region via the Resource Groups Tagging API
(``resourcegroupstaggingapi:GetResources``) — one paginated sweep returns
``{ARN: {key: value}}`` for nearly every taggable resource, decoupled from the
per-service collectors. S3 buckets (global) are fetched separately via
``get_bucket_tagging``, and cost-allocation tag activation is read once from
Cost Explorer (``ce:ListCostAllocationTags``).

Required IAM (all in ReadOnlyAccess):
    tag:GetResources
    s3:GetBucketTagging
    ce:ListCostAllocationTags
"""

from botocore.exceptions import ClientError, EndpointConnectionError, BotoCoreError


def fetch_region_tags(session, region):
    """Return ``{arn: {key: value}}`` for all taggable resources in *region*."""
    out = {}
    try:
        client = session.client("resourcegroupstaggingapi", region_name=region)
        paginator = client.get_paginator("get_resources")
        for page in paginator.paginate(ResourcesPerPage=100):
            for m in page.get("ResourceTagMappingList", []):
                arn = m.get("ResourceARN")
                if arn:
                    out[arn] = {t["Key"]: t["Value"] for t in m.get("Tags", [])}
    except (ClientError, EndpointConnectionError, BotoCoreError) as e:
        print(f"  [!] Tag fetch failed in {region}: {type(e).__name__} – tags skipped here")
    return out


def fetch_s3_bucket_tags(session, bucket):
    """Return ``{key: value}`` for an S3 bucket (global), or ``{}`` if untagged."""
    try:
        s3 = session.client("s3")
        resp = s3.get_bucket_tagging(Bucket=bucket)
        return {t["Key"]: t["Value"] for t in resp.get("TagSet", [])}
    except ClientError:
        # NoSuchTagSet (no tags) / AccessDenied / wrong region — treat as untagged.
        return {}
    except (EndpointConnectionError, BotoCoreError):
        return {}


def fetch_cost_allocation_keys(session):
    """Return the set of tag keys activated for cost allocation (account-wide).

    Cost-allocation tags are ordinary resource tags that have been *activated*
    in the Billing console so they appear in Cost & Usage Reports. The list is
    account-level (not per resource) and lives in Cost Explorer, which is only
    reachable in us-east-1.
    """
    keys = set()
    try:
        ce = session.client("ce", region_name="us-east-1")
        token = None
        while True:
            kwargs = {"Status": "Active"}
            if token:
                kwargs["NextToken"] = token
            resp = ce.list_cost_allocation_tags(**kwargs)
            for t in resp.get("CostAllocationTags", []):
                keys.add(t["TagKey"])
            token = resp.get("NextToken")
            if not token:
                break
    except (ClientError, EndpointConnectionError, BotoCoreError) as e:
        print(f"  [!] Cost-allocation tags unavailable: {type(e).__name__}")
    return keys


def tags_to_string(tag_map):
    """Serialize ``{k: v}`` to a stable ``k=v;k2=v2`` string (sorted by key)."""
    if not tag_map:
        return ""
    return ";".join(f"{k}={v}" for k, v in sorted(tag_map.items()))


def filter_tags(tag_map, include_aws=False):
    """Drop ``aws:``-prefixed system tags unless *include_aws* is set."""
    if include_aws:
        return dict(tag_map)
    return {k: v for k, v in tag_map.items() if not k.startswith("aws:")}
