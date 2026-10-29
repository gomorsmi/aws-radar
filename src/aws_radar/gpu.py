#!/usr/bin/env python3
"""
GPU fleet savings for aws-radar — the AWS port of gcp-radar's gpu module.

  KPIs        -> GPU spend, % busy, idle GPU-hours / idle spend, fleet-mix savings
  per type    -> allocated / requested / busy GPU-hours, cost per allocated vs busy hour, markup
  recs        -> stop idle instances, shrink EKS node groups, downshift GPU type, share GPUs
  attention   -> FlashAttention check on EKS GPU pods (--attention)

Data sources (read-only):
  * EC2 DescribeInstances / DescribeInstanceTypes  -> GPU instances, GPU model / count / memory
  * Cost Explorer, EC2 compute by instance type    -> cost (whole instance: GPU + vCPU + RAM),
                                                      split across instances by running hours
  * CloudWatch AWS/EC2 CPUUtilization              -> uptime (allocated GPU-hours)
  * CloudWatch CWAgent nvidia_smi_utilization_gpu  -> GPU busy % (CloudWatch agent on the instance)
  * CloudWatch ContainerInsights node_/pod_gpu_*   -> EKS GPU busy %, requests, memory
                                                      (Container Insights with enhanced observability)
  * Kubernetes API, optional                       -> pod specs for the attention check

Instances without GPU metrics have unknown utilisation and get no
utilisation-based recommendation, rather than being reported as idle.

Required IAM:
    ec2:DescribeInstances  ec2:DescribeInstanceTypes  ce:GetCostAndUsage
    cloudwatch:GetMetricData  cloudwatch:ListMetrics

Usage:
    aws-radar gpu --region us-east-1
    aws-radar gpu --all-regions --days 14 --output gpu.json
    aws-radar gpu --price L4=0.80 --price T4=0.53
    aws-radar gpu --attention --kube-context my-eks-cluster
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone

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

DEFAULT_DAYS = 30
HOURS_PER_MONTH = 730
IDLE_BUSY_PCT = 2.0           # below this average busy % an instance counts as idle
DOWNSHIFT_MAX_BUSY = 40.0     # workload must be under this busy % ...
DOWNSHIFT_MEM_HEADROOM = 0.8  # ... and peak memory under 80% of the smaller GPU's memory
SHARE_MAX_BUSY = 15.0         # a whole GPU this idle is a time-sharing / MIG candidate
CI_INTERVAL_S = 60            # Container Insights emits one datapoint per pod per minute

EC2_COMPUTE = "Amazon Elastic Compute Cloud - Compute"
COST_METRIC = "AmortizedCost"  # spreads RI / Savings Plan fees over the usage they cover

GPU_MEM_GB = {"T4": 16, "L4": 24, "A10G": 24, "L40S": 48, "A100": 40, "A100-80": 80,
              "H100": 80, "H200": 141, "V100": 16}
# Cheapest-first ladder (on-demand $/GPU-hour) used for downshift recommendations
LADDER = ["T4", "L4", "A10G", "L40S", "A100", "A100-80", "H100", "H200"]

POOL_TAGS = ("eks:nodegroup-name", "karpenter.sh/nodepool")

# (namespace, metric) pairs reporting GPU busy % per instance
GPU_UTIL_METRICS = [("CWAgent", "nvidia_smi_utilization_gpu"),
                    ("ContainerInsights", "node_gpu_utilization")]


@dataclass
class GpuResource:
    name: str
    kind: str                 # "instance" | "nodepool" | "workload"
    location: str             # AZ for instances / node pools, cluster for workloads
    gpu_type: str
    gpus: int                 # GPUs per instance/node (workload: GPUs requested per pod)
    instance_type: str = ""
    nodes: int = 1
    instance_ids: list = field(default_factory=list)
    alloc_hours: float = 0.0
    requested_hours: float = 0.0
    busy_hours: float = 0.0   # sum(busy%/100 * GPU-hours)
    util_measured: bool = False
    peak_mem_gb: float = 0.0
    cost: float = 0.0

    @property
    def busy_pct(self) -> float:
        return 100 * self.busy_hours / self.alloc_hours if self.alloc_hours else 0.0

    @property
    def idle_hours(self) -> float:
        return max(self.alloc_hours - self.busy_hours, 0.0)


def _warn(what, e):
    if isinstance(e, ClientError):
        code = e.response["Error"]["Code"]
        print(f"  [!] {what} skipped ({code})")
    else:
        print(f"  [!] {what} skipped ({type(e).__name__})")


# ---------------------------------------------------------------- inventory
def gpu_model(name: str, mem_mib: int) -> str:
    """Normalise an EC2 GpuInfo name. p4de's A100s are the 80 GB part."""
    if name == "A100" and mem_mib >= 80 * 1024:
        return "A100-80"
    return name


def gpu_specs(ec2, instance_types) -> dict[str, tuple[str, int]]:
    """``{instance_type: (gpu_model, gpu_count)}`` for the GPU types among *instance_types*."""
    out = {}
    types = sorted(instance_types)
    for i in range(0, len(types), 100):
        for page in ec2.get_paginator("describe_instance_types").paginate(
                InstanceTypes=types[i:i + 100]):
            for t in page["InstanceTypes"]:
                gpus = t.get("GpuInfo", {}).get("Gpus") or []
                if gpus:
                    g = gpus[0]
                    mem = g.get("MemoryInfo", {}).get("SizeInMiB", 0)
                    out[t["InstanceType"]] = (gpu_model(g["Name"], mem),
                                              sum(x.get("Count", 0) for x in gpus))
    return out


def list_gpu_instances(session, region) -> dict[str, GpuResource]:
    """Standalone GPU instances + EKS GPU nodes rolled up to their node group / node pool."""
    ec2 = session.client("ec2", region_name=region)
    instances = []
    for page in ec2.get_paginator("describe_instances").paginate(Filters=[{
            "Name": "instance-state-name",
            "Values": ["pending", "running", "stopping", "stopped"]}]):
        for res in page["Reservations"]:
            instances += res["Instances"]

    specs = gpu_specs(ec2, {i["InstanceType"] for i in instances}) if instances else {}
    out: dict[str, GpuResource] = {}
    for inst in instances:
        itype = inst["InstanceType"]
        if itype not in specs:
            continue
        model, count = specs[itype]
        tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
        pool = next((tags[k] for k in POOL_TAGS if tags.get(k)), None)
        az = inst.get("Placement", {}).get("AvailabilityZone", region)
        iid = inst["InstanceId"]
        if pool:
            cluster = tags.get("eks:cluster-name")
            name = f"{cluster}/{pool}" if cluster else pool
            key, kind = f"{name}|{itype}", "nodepool"
        else:
            name = f"{tags['Name']} ({iid})" if tags.get("Name") else iid
            key, kind = iid, "instance"
        if key in out:
            out[key].nodes += 1
            out[key].instance_ids.append(iid)
        else:
            out[key] = GpuResource(name, kind, az, model, count, instance_type=itype,
                                   instance_ids=[iid])
    return out


# ---------------------------------------------------------------- CloudWatch
def _stat(namespace, metric, dims, stat):
    return {"Metric": {"Namespace": namespace, "MetricName": metric, "Dimensions": dims},
            "Period": 3600, "Stat": stat}


def _metric_data(cw, stats: dict, start, end) -> dict[str, list]:
    """``{query_id: [hourly values]}`` for ``{query_id: MetricStat}``."""
    out = defaultdict(list)
    items = list(stats.items())
    for i in range(0, len(items), 500):       # GetMetricData limit per request
        queries = [{"Id": qid, "MetricStat": ms, "ReturnData": True}
                   for qid, ms in items[i:i + 500]]
        token = None
        while True:
            kwargs = {"MetricDataQueries": queries, "StartTime": start, "EndTime": end}
            if token:
                kwargs["NextToken"] = token
            resp = cw.get_metric_data(**kwargs)
            for r in resp.get("MetricDataResults", []):
                out[r["Id"]] += r.get("Values", [])
            token = resp.get("NextToken")
            if not token:
                break
    return out


def _series_by(cw, namespace, metric, stat, start, end, key, exact=None, only=None):
    """``{key: [hourly values]}`` over every series of *metric* that has the *key* dims.

    *key* is a tuple of dimension names joined with "/" to form the key.
    *exact* keeps only series with exactly that dimension set, to skip
    CloudWatch's pre-aggregated rollups. *only* filters keys.
    """
    stats, keys = {}, {}
    for page in cw.get_paginator("list_metrics").paginate(
            Namespace=namespace, MetricName=metric, Dimensions=[{"Name": key[-1]}]):
        for m in page.get("Metrics", []):
            d = {x["Name"]: x["Value"] for x in m["Dimensions"]}
            if exact is not None and set(d) != exact:
                continue
            k = "/".join(d.get(n, "") for n in key)
            if only is not None and k not in only:
                continue
            qid = f"q{len(stats)}"
            stats[qid] = _stat(namespace, metric, m["Dimensions"], stat)
            keys[qid] = k
    out = defaultdict(list)
    for qid, vals in _metric_data(cw, stats, start, end).items():
        out[keys[qid]] += vals
    return out


def collect_utilization(cw, resources, start, end) -> None:
    """Allocated, requested and busy GPU-hours per instance / node pool."""
    resources = list(resources)
    by_iid ={iid: r for r in resources for iid in r.instance_ids}
    only = set(by_iid)

    # Allocated GPU-hours = hours with any CPUUtilization datapoint * GPUs per instance
    stats = {f"q{n}": _stat("AWS/EC2", "CPUUtilization",
                            [{"Name": "InstanceId", "Value": iid}], "SampleCount")
             for n, iid in enumerate(by_iid)}
    ids = dict(zip(stats, by_iid))
    for qid, vals in _metric_data(cw, stats, start, end).items():
        r = by_iid[ids[qid]]
        r.alloc_hours += len(vals) * r.gpus

    # Busy %: the CloudWatch agent's NVML metric or Container Insights, averaged
    # over every GPU and hour reported for the instance
    util = defaultdict(list)                  # keyed by id(resource): dataclasses aren't hashable
    for namespace, metric in GPU_UTIL_METRICS:
        for iid, vals in _series_by(cw, namespace, metric, "Average", start, end,
                                    ("InstanceId",), only=only).items():
            util[id(by_iid[iid])] += vals
    for r in resources:
        vals = util.get(id(r))
        if vals:
            r.util_measured = True
            r.busy_hours = r.alloc_hours * (sum(vals) / len(vals)) / 100

    # Requested GPU-hours on EKS nodes: hourly mean of GPUs held by running pods
    used = _series_by(cw, "ContainerInsights", "node_gpu_usage_total", "Average",
                      start, end, ("InstanceId",),
                      exact={"ClusterName", "InstanceId", "NodeName"}, only=only)
    for iid, vals in used.items():
        by_iid[iid].requested_hours += sum(vals)


def collect_eks_workloads(cw, start, end) -> dict[str, GpuResource]:
    """Per-pod-group requests, busy % and peak memory from Container Insights.

    Container Insights' ``PodName`` dimension is the pod name without its
    generated suffix, so replicas of one Deployment share a series.
    """
    key = ("ClusterName", "Namespace", "PodName")
    dims = set(key)
    ci = "ContainerInsights"
    req_sum = _series_by(cw, ci, "pod_gpu_request", "Sum", start, end, key, exact=dims)
    req_avg = _series_by(cw, ci, "pod_gpu_request", "Average", start, end, key, exact=dims)
    busy = _series_by(cw, ci, "pod_gpu_utilization", "Average", start, end, key, exact=dims)
    mem = _series_by(cw, ci, "pod_gpu_memory_used", "Maximum", start, end, key, exact=dims)
    out = {}
    for k, sums in req_sum.items():
        cluster, ns, pod = k.split("/", 2)
        # Sum of per-minute samples / samples-per-hour = GPU-hours for that hour
        hours = sum(sums) * CI_INTERVAL_S / 3600
        w = GpuResource(f"{ns}/{pod}", "workload", cluster, "?",
                        round(max(req_avg.get(k) or [1])) or 1)
        w.requested_hours = w.alloc_hours = hours
        if busy.get(k):
            w.util_measured = True
            w.busy_hours = hours * (sum(busy[k]) / len(busy[k])) / 100
        w.peak_mem_gb = max(mem.get(k) or [0]) / 1e9
        out[k] = w
    return out


# ---------------------------------------------------------------- cost
def collect_costs(session, resources, regions, start, end) -> None:
    """Cost Explorer EC2 compute cost per GPU instance type, split by instance-hours.

    Cost Explorer has no per-instance cost without resource-level data, so
    each type's cost is shared across that type's instances in proportion to
    their running hours. Spend from GPU instances terminated during the window
    lands on the surviving instances of the same type.
    """
    types = sorted({r.instance_type for r in resources})
    if not types:
        return
    ce = session.client("ce", region_name="us-east-1")
    kwargs = {
        "TimePeriod": {"Start": start.date().isoformat(), "End": end.date().isoformat()},
        "Granularity": "MONTHLY",
        "Metrics": [COST_METRIC],
        "Filter": {"And": [
            {"Dimensions": {"Key": "SERVICE", "Values": [EC2_COMPUTE]}},
            {"Dimensions": {"Key": "INSTANCE_TYPE", "Values": types}},
            {"Dimensions": {"Key": "REGION", "Values": sorted(regions)}},
        ]},
        "GroupBy": [{"Type": "DIMENSION", "Key": "INSTANCE_TYPE"}],
    }
    cost = defaultdict(float)
    while True:
        resp = ce.get_cost_and_usage(**kwargs)
        for period in resp.get("ResultsByTime", []):
            for grp in period.get("Groups", []):
                cost[grp["Keys"][0]] += float(grp["Metrics"][COST_METRIC]["Amount"] or 0)
        if not resp.get("NextPageToken"):
            break
        kwargs["NextPageToken"] = resp["NextPageToken"]

    by_type = defaultdict(list)
    for r in resources:
        by_type[r.instance_type].append(r)
    for itype, rs in by_type.items():
        hours = [r.alloc_hours / r.gpus if r.gpus else 0 for r in rs]
        total = sum(hours)
        for r, h in zip(rs, hours):
            # No uptime data at all: fall back to an even split by node count
            share = h / total if total else r.nodes / sum(x.nodes for x in rs)
            r.cost = cost.get(itype, 0.0) * share


# ---------------------------------------------------------------- analysis
def unit_costs(resources) -> dict[str, float]:
    """Observed $/allocated GPU-hour per GPU type (override with --price)."""
    cost, hours = defaultdict(float), defaultdict(float)
    for r in resources:
        cost[r.gpu_type] += r.cost
        hours[r.gpu_type] += r.alloc_hours
    return {t: cost[t] / hours[t] for t in cost if hours[t]}


def summarize(resources) -> dict:
    by = defaultdict(lambda: defaultdict(float))
    for r in resources:
        b = by[r.gpu_type]
        b["gpus"] += r.gpus * r.nodes
        for f in ("cost", "alloc_hours", "requested_hours", "busy_hours"):
            b[f] += getattr(r, f)
    for t, b in by.items():
        b["busy_pct"] = 100 * b["busy_hours"] / b["alloc_hours"] if b["alloc_hours"] else 0
        b["idle_hours"] = b["alloc_hours"] - b["busy_hours"]
        b["cost_per_alloc_hr"] = b["cost"] / b["alloc_hours"] if b["alloc_hours"] else 0
        b["cost_per_busy_hr"] = b["cost"] / b["busy_hours"] if b["busy_hours"] else 0
        b["markup"] = b["cost_per_busy_hr"] / b["cost_per_alloc_hr"] if b["cost_per_alloc_hr"] else 0
        b["idle_spend"] = b["idle_hours"] * b["cost_per_alloc_hr"]
    return {t: dict(b) for t, b in by.items()}


def recommend(resources, workloads, price: dict[str, float]) -> list[dict]:
    recs = []
    for r in resources:
        hourly = price.get(r.gpu_type, 0) * r.gpus
        # 1. Idle standalone instance -> stop
        if (r.kind == "instance" and r.util_measured and r.alloc_hours
                and r.busy_pct < IDLE_BUSY_PCT):
            recs.append(_rec(r, f"{r.gpus}x {r.gpu_type} -> stopped", "Stop instance", "Low",
                             hourly * HOURS_PER_MONTH))
        # 2. Node pool allocated well above what pods request -> shrink
        if r.kind == "nodepool" and r.alloc_hours and r.requested_hours:
            needed = max(1, -(-r.requested_hours // (r.alloc_hours / r.nodes)))  # ceil
            if needed < r.nodes:
                recs.append(_rec(r, f"{r.nodes} -> {int(needed)} nodes",
                                 "Shrink node group / autoscale", "Low",
                                 (r.nodes - needed) * hourly * HOURS_PER_MONTH))
    for w in workloads:
        if not w.util_measured:
            continue
        # 3. Low busy % + memory fits a cheaper GPU -> downshift type
        cur = w.gpu_type if w.gpu_type in LADDER else None
        moved = False
        if cur and w.busy_pct < DOWNSHIFT_MAX_BUSY:
            for cand in LADDER[:LADDER.index(cur)]:
                if w.peak_mem_gb < GPU_MEM_GB[cand] * DOWNSHIFT_MEM_HEADROOM and cand in price:
                    new_gpus = 1
                    saving = (price.get(cur, 0) * w.gpus - price[cand] * new_gpus) * HOURS_PER_MONTH
                    if saving > 0:
                        recs.append(_rec(w, f"{w.gpus}x {cur} per pod -> {new_gpus}x {cand} per pod",
                                         f"Move to {cand}", "Medium", saving))
                        moved = True
                    break
        # 4. Requests a whole GPU but barely uses it, and no cheaper GPU fits -> time-sharing / MIG
        if not moved and w.busy_pct < SHARE_MAX_BUSY and w.gpus == 1:
            recs.append(_rec(w, "1 GPU -> shared (time-slicing / MIG)", "Share GPU", "Medium",
                             0.5 * price.get(w.gpu_type, 0) * HOURS_PER_MONTH))
    return sorted(recs, key=lambda x: -x["savings_mo"])


def _rec(r, change, action, risk, saving):
    return {"resource": r.name, "kind": r.kind, "location": r.location, "change": change,
            "action": action, "risk": risk, "busy_pct": round(r.busy_pct, 1),
            "savings_mo": round(saving, 2)}


# ---------------------------------------------------------------- attention / FlashAttention
# Standard attention materialises the full S = QK^T score matrix in HBM (seq^2 per head);
# FlashAttention tiles it through on-chip SRAM with an online softmax and never writes S.
# Kernel support by GPU: FA3 = Hopper, FA2 = Ampere/Ada/Hopper, pre-Ampere = xformers/SDPA only.
FLASH_SUPPORT = {"H100": "FA3", "H200": "FA3", "A100": "FA2", "A100-80": "FA2",
                 "A10G": "FA2", "L4": "FA2", "L40S": "FA2", "T4": None, "V100": None}

INFERENCE_IMAGES = ("vllm", "sglang", "text-generation-inference", "tgi", "tensorrt-llm", "triton")
# Settings that force a non-flash attention path (engine defaults are flash on Ampere+)
NON_FLASH_FLAGS = {
    "VLLM_ATTENTION_BACKEND": {"XFORMERS", "TORCH_SDPA"},   # vLLM env override
    "--attention-backend": {"triton", "torch_native"},       # SGLang flag
    "attn_implementation": {"eager"},                        # HF transformers
    "USE_FLASH_ATTENTION": {"false", "0"},                   # TGI
}
CONTEXT_FLAGS = ("--max-model-len", "--context-length", "--max-input-length", "MAX_INPUT_TOKENS")


def score_matrix_bytes(seq_len: int, heads: int = 1, batch: int = 1, dtype_bytes: int = 2) -> int:
    """HBM bytes standard attention writes for S in ONE layer. 8192 tokens, 1 head, fp16 = 128 MiB."""
    return seq_len * seq_len * dtype_bytes * heads * batch


def detect_attention(container) -> dict:
    """Classify one container spec (kubernetes V1Container) -> flash / non-flash / unknown."""
    image = (container.image or "").lower()
    tokens = list(container.args or []) + list(container.command or [])
    env = {e.name: (e.value or "") for e in (container.env or [])}
    flat = {**env}
    for i, t in enumerate(tokens):
        if "=" in t:
            k, v = t.split("=", 1)
            flat[k] = v
        elif t.startswith("--") and i + 1 < len(tokens):
            flat[t] = tokens[i + 1]
    context = next((int(flat[k]) for k in CONTEXT_FLAGS if str(flat.get(k, "")).isdigit()), None)
    for key, bad in NON_FLASH_FLAGS.items():
        if str(flat.get(key, "")).lower() in {b.lower() for b in bad}:
            return {"status": "non-flash", "reason": f"{key}={flat[key]}", "context": context}
    if any(x in image for x in INFERENCE_IMAGES):
        return {"status": "flash-default", "reason": f"engine image {image.split('/')[-1]}", "context": context}
    return {"status": "unknown", "reason": "custom image; check attn_implementation in code", "context": context}


def collect_pod_specs(kube_api) -> dict[str, list]:
    """GPU containers per ``namespace/controller``. kube_api = kubernetes.client.CoreV1Api()."""
    out = defaultdict(list)
    for pod in kube_api.list_pod_for_all_namespaces(field_selector="status.phase=Running").items:
        gpu_containers = [c for c in pod.spec.containers
                          if c.resources and c.resources.limits and "nvidia.com/gpu" in c.resources.limits]
        if not gpu_containers:
            continue
        owner = (pod.metadata.owner_references or [None])[0]
        name = owner.name if owner else pod.metadata.name
        if owner and owner.kind == "ReplicaSet":
            name = name.rsplit("-", 1)[0]  # strip pod-template hash -> Deployment name
        out[f"{pod.metadata.namespace}/{name}"] += gpu_containers
    return out


def attention_findings(workloads, pod_specs: dict[str, list]) -> list[dict]:
    """Findings are throughput/memory levers, reported separately from $ fleet-mix savings."""
    findings = []
    for w in workloads:
        for c in pod_specs.get(w.name, []):
            a = detect_attention(c)
            support = FLASH_SUPPORT.get(w.gpu_type)
            hbm = (f"~{score_matrix_bytes(a['context']) / 2**20:,.0f} MiB of scores per head per layer"
                   if a["context"] else "seq^2 x 2 B per head per layer")
            if a["status"] == "non-flash" and support:
                findings.append({"resource": w.name, "gpu": w.gpu_type, "finding": "FlashAttention disabled",
                                 "evidence": a["reason"], "action": f"Remove override; {support} is supported",
                                 "impact": f"stops writing {hbm} to HBM; frees KV-cache room for larger batches",
                                 "risk": "Low"})
            elif a["status"] != "unknown" and w.gpu_type in FLASH_SUPPORT and support is None:
                findings.append({"resource": w.name, "gpu": w.gpu_type, "finding": "No FA2 kernels on this GPU",
                                 "evidence": f"{w.gpu_type} is pre-Ampere; {a['reason']}",
                                 "action": "Move inference to L4 (FA2) or consolidate",
                                 "impact": f"long prompts pay {hbm}; pair with fleet-mix recs", "risk": "Medium"})
            elif a["status"] == "unknown" and w.busy_pct < DOWNSHIFT_MAX_BUSY:
                findings.append({"resource": w.name, "gpu": w.gpu_type, "finding": "Attention path unverified",
                                 "evidence": a["reason"],
                                 "action": "Confirm attn_implementation='flash_attention_2' or 'sdpa'",
                                 "impact": "eager attention is HBM-bound; may explain low busy %", "risk": "Low"})
            break  # one finding per workload
    return findings


# ---------------------------------------------------------------- entry point
def run(session, regions, days: int = DEFAULT_DAYS, price_overrides: dict | None = None,
        kube_api=None, now=None) -> dict:
    """kube_api: optional kubernetes.client.CoreV1Api() for the FlashAttention check."""
    end = now or datetime.now(timezone.utc)
    start = end - timedelta(days=days)

    res: dict[str, GpuResource] = {}
    workloads: dict[str, GpuResource] = {}
    for region in regions:
        try:
            found = list_gpu_instances(session, region)
        except (ClientError, EndpointConnectionError, BotoCoreError) as e:
            _warn(f"{region}: EC2 inventory", e)
            continue
        res.update(found)
        cw = session.client("cloudwatch", region_name=region)
        try:
            if found:
                collect_utilization(cw, found.values(), start, end)
            workloads.update(collect_eks_workloads(cw, start, end))
        except (ClientError, EndpointConnectionError, BotoCoreError) as e:
            _warn(f"{region}: CloudWatch GPU metrics", e)
        if found or workloads:
            print(f"  {region}: {len(found)} GPU instance(s)/node pool(s)")

    try:
        collect_costs(session, res.values(), regions, start, end)
    except (ClientError, EndpointConnectionError, BotoCoreError) as e:
        _warn("Cost Explorer GPU spend", e)

    # Workload GPU type: Container Insights pod metrics don't carry the node,
    # so take the node-pool type when there's one GPU type in the EKS fleet.
    types = {r.gpu_type for r in res.values() if r.kind == "nodepool"}
    for w in workloads.values():
        if len(types) == 1:
            w.gpu_type = next(iter(types))
    price = {**unit_costs(res.values()), **(price_overrides or {})}
    recs = recommend(res.values(), workloads.values(), price)
    attention = attention_findings(workloads.values(), collect_pod_specs(kube_api)) if kube_api else []
    by_type = summarize(res.values())
    total_alloc = sum(b["alloc_hours"] for b in by_type.values())
    total_busy = sum(b["busy_hours"] for b in by_type.values())
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat(), "days": days},
        "kpis": {
            "gpu_spend": round(sum(b["cost"] for b in by_type.values()), 2),
            "gpus": int(sum(b["gpus"] for b in by_type.values())),
            "busy_pct": round(100 * total_busy / total_alloc, 1) if total_alloc else 0,
            "idle_gpu_hours": round(total_alloc - total_busy),
            "idle_spend": round(sum(b["idle_spend"] for b in by_type.values()), 2),
            "fleet_mix_savings_mo": round(sum(r["savings_mo"] for r in recs), 2),
            "recommendations": len(recs),
            "attention_findings": len(attention),
            "unmeasured": sum(1 for r in res.values() if not r.util_measured),
        },
        "by_type": by_type,
        "resources": [asdict(r) for r in res.values()],
        "workloads": [asdict(w) for w in workloads.values()],
        "recommendations": recs,
        "attention": attention,
    }


# ---------------------------------------------------------------- display
def _table(title, columns, rows):
    if RICH_AVAILABLE:
        tbl = Table(title=title, box=box.ROUNDED, header_style="bold cyan",
                    title_style="bold white on blue")
        for name, justify in columns:
            tbl.add_column(name, justify=justify, overflow="fold")
        for r in rows:
            tbl.add_row(*r)
        console.print()
        console.print(tbl)
    else:
        print(f"\n{'='*10} {title} {'='*10}")
        print("  ".join(name for name, _ in columns))
        for r in rows:
            print("  ".join(r))


def display_report(report):
    k = report["kpis"]
    if not k["gpus"] and not report["workloads"]:
        print("\nNo GPU instances found.\n")
        return
    days = report["window"]["days"]
    _table(f"GPU fleet – last {days} days", [("GPU spend", "right"), ("GPUs", "right"),
                                            ("Busy", "right"), ("Idle GPU-hrs", "right"),
                                            ("Idle spend", "right"), ("Savings / mo", "right")],
           [[f"${k['gpu_spend']:,.2f}", str(k["gpus"]), f"{k['busy_pct']:.1f}%",
             f"{k['idle_gpu_hours']:,}", f"${k['idle_spend']:,.2f}",
             f"${k['fleet_mix_savings_mo']:,.2f}"]])
    if k["unmeasured"]:
        print(f"  {k['unmeasured']} instance(s)/node pool(s) report no GPU utilisation — install the "
              "CloudWatch agent's NVIDIA metrics or Container Insights to measure them")

    _table("By GPU type", [("GPU", "left"), ("GPUs", "right"), ("Alloc GPU-hrs", "right"),
                           ("Req GPU-hrs", "right"), ("Busy GPU-hrs", "right"), ("Busy", "right"),
                           ("$/alloc hr", "right"), ("$/busy hr", "right"), ("Markup", "right")],
           [[t, f"{b['gpus']:.0f}", f"{b['alloc_hours']:,.0f}", f"{b['requested_hours']:,.0f}",
             f"{b['busy_hours']:,.0f}", f"{b['busy_pct']:.1f}%", f"{b['cost_per_alloc_hr']:,.2f}",
             f"{b['cost_per_busy_hr']:,.2f}", f"{b['markup']:.1f}x"]
            for t, b in sorted(report["by_type"].items(), key=lambda kv: -kv[1]["cost"])])

    if report["recommendations"]:
        _table("Recommendations", [("Resource", "left"), ("Action", "left"), ("Change", "left"),
                                   ("Busy", "right"), ("Risk", "left"), ("Savings / mo", "right")],
               [[r["resource"], r["action"], r["change"], f"{r['busy_pct']:.1f}%", r["risk"],
                 f"${r['savings_mo']:,.2f}"] for r in report["recommendations"]])
    if report["attention"]:
        _table("Attention findings", [("Workload", "left"), ("GPU", "left"), ("Finding", "left"),
                                      ("Evidence", "left"), ("Action", "left")],
               [[f["resource"], f["gpu"], f["finding"], f["evidence"], f["action"]]
                for f in report["attention"]])
    print()


def parse_prices(pairs) -> dict[str, float]:
    """``["L4=0.80", "T4=0.53"]`` -> ``{"L4": 0.8, "T4": 0.53}`` ($ per GPU-hour)."""
    out = {}
    for p in pairs or []:
        gpu, _, value = p.partition("=")
        try:
            out[gpu.strip()] = float(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"--price expects GPU=USD_PER_GPU_HOUR, got {p!r}") from None
    return out


def _kube_api(context):
    try:
        from kubernetes import client, config
    except ImportError:
        sys.exit("\n--attention needs the Kubernetes client:\n"
                 "    python3 -m pip install 'aws-radar[k8s]'\n")
    config.load_kube_config(context=context)
    return client.CoreV1Api()


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="AWS GPU fleet utilisation and savings")
    parser.add_argument("--profile", default=None, help="AWS SSO/named profile")
    parser.add_argument("--region", default=None, help="AWS region (default: boto3 default)")
    parser.add_argument("--all-regions", action="store_true", help="Scan all enabled regions")
    parser.add_argument("--days", type=int, default=DEFAULT_DAYS,
                        help=f"Lookback window in days (default: {DEFAULT_DAYS})")
    parser.add_argument("--price", action="append", metavar="GPU=USD",
                        help="$ per GPU-hour for a GPU type, e.g. L4=0.80 (repeatable). "
                             "Needed to price downshifts to types you don't run")
    parser.add_argument("--attention", action="store_true",
                        help="Check EKS GPU pods for FlashAttention (needs aws-radar[k8s] and a kubeconfig)")
    parser.add_argument("--kube-context", default=None,
                        help="kubeconfig context for --attention (default: current context)")
    parser.add_argument("--output", metavar="FILE.json", default=None,
                        help="Write the full report as JSON")
    args = parser.parse_args()

    try:
        prices = parse_prices(args.price)
    except argparse.ArgumentTypeError as e:
        parser.error(str(e))

    try:
        session = boto3.Session(profile_name=args.profile)
        identity = session.client("sts").get_caller_identity()
        print(f"\n✓ Authenticated as: {identity.get('Arn', 'unknown')}")
        print(f"  Account ID : {identity['Account']}")
    except NoCredentialsError:
        print("\n✗ No AWS credentials found.")
        print("  Configure with: aws configure  OR  set AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY\n")
        sys.exit(1)
    except ClientError as e:
        print(f"\n✗ Auth error: {e}\n")
        sys.exit(1)

    if args.all_regions:
        from aws_radar.inventory import get_all_regions
        regions = get_all_regions(session)
    else:
        regions = [args.region or session.region_name or "us-east-1"]

    kube_api = _kube_api(args.kube_context) if args.attention else None

    print(f"\nScanning GPU fleet — last {args.days} days …")
    report = run(session, regions, days=args.days, price_overrides=prices, kube_api=kube_api)
    display_report(report)
    if args.output:
        with open(args.output, "w") as f:
            json.dump(report, f, indent=2, default=str)
        print(f"Wrote GPU report to {args.output}")


if __name__ == "__main__":
    main()
