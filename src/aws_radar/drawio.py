#!/usr/bin/env python3
"""
AWS Inventory → draw.io Diagram Generator

Reads the CSV produced by aws_inventory.py and generates a .drawio file
that you can open directly in draw.io (app.diagrams.net) or the desktop app.

Usage:
    # First generate inventory:
    python aws_inventory.py --all-regions --export inventory.csv

    # Then generate diagram:
    python aws_to_drawio.py --input inventory.csv --output architecture.drawio

    # Open architecture.drawio in draw.io / diagrams.net

Requirements:
    pip install boto3  (only if fetching live; not needed if using CSV)
"""

import csv
import argparse
import re
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from xml.dom import minidom

# ─────────────────────────────────────────────
# draw.io shape mapping per AWS service
# Uses official AWS shape library IDs (mxgraph.aws4.*)
# ─────────────────────────────────────────────

SERVICE_STYLES = {
    "EC2":            "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#ED7100;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.ec2;",
    "RDS":            "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#C7131F;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.rds;",
    "RDS Cluster":    "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#C7131F;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.aurora;",
    "Lambda":         "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#E7157B;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.lambda;",
    "ECS Cluster":    "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#ED7100;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.ecs;",
    "ECS Service":    "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#ED7100;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.ecs;",
    "EKS":            "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#ED7100;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.eks;",
    "ElastiCache":    "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#C7131F;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.elasticache;",
    "ElastiCache RG": "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#C7131F;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.elasticache;",
    "DynamoDB":       "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#C7131F;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.dynamodb;",
    "S3":             "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#3F8624;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.s3;",
    "OpenSearch":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#C7131F;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.opensearch_service;",
    "SQS":            "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#E7157B;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sqs;",
    "SNS":            "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#E7157B;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sns;",
    "Load Balancer":  "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#ED7100;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.elastic_load_balancing;",
    # AI / ML
    "Bedrock Model":          "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.bedrock;",
    "Bedrock Throughput":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.bedrock;",
    "Bedrock Profile":        "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.bedrock;",
    "SageMaker Endpoint":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sagemaker;",
    "SageMaker Notebook":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sagemaker_notebook;",
    "SageMaker Training":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sagemaker;",
    "SageMaker Pipeline":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sagemaker;",
    "SageMaker FeatureGroup": "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.sagemaker;",
    "Rekognition Collection": "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.rekognition;",
    "Rekognition Project":    "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.rekognition;",
    "Textract Adapter":       "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.textract;",
    "Comprehend Classifier":  "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.comprehend;",
    "Comprehend Recognizer":  "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.comprehend;",
    "Lex Bot":                "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.lex;",
    "Kendra Index":           "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.kendra;",
    "Transcribe Vocabulary":  "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.transcribe;",
    "Transcribe Model":       "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.transcribe;",
    "Polly Lexicon":          "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.polly;",
    "Translate Terminology":  "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.translate;",
    "Forecast Predictor":     "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.forecast;",
    "Personalize Campaign":   "outlineConnect=0;fontColor=#232F3E;gradientColor=none;strokeColor=none;fillColor=#01A88D;labelBackgroundColor=#ffffff;align=center;html=1;fontSize=12;fontStyle=0;aspect=fixed;shape=mxgraph.aws4.resourceIcon;resIcon=mxgraph.aws4.personalize;",
}

# Service → category grouping for swim lanes
SERVICE_CATEGORY = {
    "EC2": "Compute", "ECS Cluster": "Compute", "ECS Service": "Compute",
    "EKS": "Compute", "Lambda": "Compute",
    "RDS": "Database", "RDS Cluster": "Database", "DynamoDB": "Database",
    "ElastiCache": "Database", "ElastiCache RG": "Database", "OpenSearch": "Database",
    "S3": "Storage",
    "SQS": "Messaging", "SNS": "Messaging",
    "Load Balancer": "Networking",
    # AI / ML
    "Bedrock Model":           "AI / ML",
    "Bedrock Throughput":      "AI / ML",
    "Bedrock Profile":         "AI / ML",
    "SageMaker Endpoint":      "AI / ML",
    "SageMaker Notebook":      "AI / ML",
    "SageMaker Training":      "AI / ML",
    "SageMaker Pipeline":      "AI / ML",
    "SageMaker FeatureGroup":  "AI / ML",
    "Rekognition Collection":  "AI / ML",
    "Rekognition Project":     "AI / ML",
    "Textract Adapter":        "AI / ML",
    "Comprehend Classifier":   "AI / ML",
    "Comprehend Recognizer":   "AI / ML",
    "Lex Bot":                 "AI / ML",
    "Kendra Index":            "AI / ML",
    "Transcribe Vocabulary":   "AI / ML",
    "Transcribe Model":        "AI / ML",
    "Polly Lexicon":           "AI / ML",
    "Translate Terminology":   "AI / ML",
    "Forecast Predictor":      "AI / ML",
    "Personalize Campaign":    "AI / ML",
}

CATEGORY_COLORS = {
    "Compute":   {"fill": "#dae8fc", "stroke": "#6c8ebf"},
    "Database":  {"fill": "#f8cecc", "stroke": "#b85450"},
    "Storage":   {"fill": "#d5e8d4", "stroke": "#82b366"},
    "Messaging": {"fill": "#fff2cc", "stroke": "#d6b656"},
    "Networking":{"fill": "#e1d5e7", "stroke": "#9673a6"},
    "AI / ML":   {"fill": "#fff2cc", "stroke": "#d6b656"},
}

# Logical connections: service A → service B (directional)
CONNECTIONS = [
    ("Load Balancer", "EC2"),
    ("Load Balancer", "ECS Service"),
    ("Load Balancer", "EKS"),
    ("EC2",           "RDS"),
    ("EC2",           "ElastiCache"),
    ("EC2",           "DynamoDB"),
    ("EC2",           "S3"),
    ("EC2",           "SQS"),
    ("ECS Service",   "RDS"),
    ("ECS Service",   "ElastiCache"),
    ("ECS Service",   "DynamoDB"),
    ("ECS Service",   "S3"),
    ("ECS Service",   "SQS"),
    ("EKS",           "RDS"),
    ("EKS",           "ElastiCache"),
    ("EKS",           "S3"),
    ("Lambda",        "DynamoDB"),
    ("Lambda",        "S3"),
    ("Lambda",        "SQS"),
    ("Lambda",        "SNS"),
    ("Lambda",        "RDS"),
    ("SQS",           "Lambda"),
    ("SNS",           "Lambda"),
    ("SNS",           "SQS"),

    # ── AI / ML ──
    # Application compute invokes AI/ML inference services
    ("Lambda",        "Bedrock Model"),
    ("Lambda",        "SageMaker Endpoint"),
    ("Lambda",        "Rekognition Collection"),
    ("Lambda",        "Textract Adapter"),
    ("Lambda",        "Comprehend Classifier"),
    ("Lambda",        "Kendra Index"),
    ("Lambda",        "Transcribe Model"),
    ("Lambda",        "Polly Lexicon"),
    ("Lambda",        "Translate Terminology"),
    ("Lambda",        "Personalize Campaign"),
    ("EC2",           "Bedrock Model"),
    ("ECS Service",   "Bedrock Model"),
    ("EKS",           "SageMaker Endpoint"),

    # Bedrock provisioning relationships
    ("Bedrock Profile",        "Bedrock Model"),
    ("Bedrock Throughput",     "Bedrock Model"),

    # Lex fulfillment via Lambda
    ("Lex Bot",                "Lambda"),

    # SageMaker training/serving internals
    ("SageMaker Pipeline",     "SageMaker Training"),
    ("SageMaker Training",     "SageMaker FeatureGroup"),
    ("Transcribe Vocabulary",  "Transcribe Model"),

    # AI/ML services read/write data in S3
    ("SageMaker Endpoint",     "S3"),
    ("SageMaker Notebook",     "S3"),
    ("SageMaker Training",     "S3"),
    ("Rekognition Project",    "S3"),
    ("Comprehend Recognizer",  "S3"),
    ("Kendra Index",           "S3"),
    ("Forecast Predictor",     "S3"),
    ("Personalize Campaign",   "S3"),
    ("Transcribe Model",       "S3"),
]

# ─────────────────────────────────────────────
# Layout constants
# ─────────────────────────────────────────────
ICON_W, ICON_H = 60, 60
LABEL_H        = 30
NODE_W         = 80   # total cell width for label
NODE_H         = ICON_H + LABEL_H
PAD_X, PAD_Y   = 30, 40
LANE_PAD       = 20
REGION_HEADER  = 40
CAT_HEADER     = 30


def slugify(text):
    return text.replace(" ", "_").replace("/", "_").replace("-", "_").lower()


def read_csv(path):
    import os
    if not os.path.exists(path):
        print(f"\n✗ File not found: {path}")
        print(f"  Run first: aws-radar inventory --export {path}\n")
        sys.exit(1)
    rows = []
    with open(path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows


def build_structure(rows):
    """
    Returns: {region: {category: [row, ...]}}
    """
    structure = defaultdict(lambda: defaultdict(list))
    for r in rows:
        region = r.get("Region", "global")
        cat = SERVICE_CATEGORY.get(r["Service"], "Other")
        structure[region][cat].append(r)
    return structure


def make_id(prefix, *parts):
    return slugify(f"{prefix}_{'_'.join(str(p) for p in parts)}")


def prettify(elem):
    raw = ET.tostring(elem, encoding="unicode")
    reparsed = minidom.parseString(raw)
    return reparsed.toprettyxml(indent="  ")


# ─────────────────────────────────────────────
# Tag support
# ─────────────────────────────────────────────

# Light, visually-distinct fills for --color-by-tag (draw.io friendly).
TAG_PALETTE = [
    "#d5e8d4", "#dae8fc", "#ffe6cc", "#fff2cc", "#f8cecc", "#e1d5e7",
    "#d0e0e3", "#fce5cd", "#d9d2e9", "#ead1dc", "#cfe2f3", "#d9ead3",
]
UNTAGGED_FILL = "#f5f5f5"   # resource lacks the color-by key
OTHER_FILL    = "#cccccc"   # value beyond the palette size

# Attribute names that would clash with mxCell/object built-ins.
_RESERVED_ATTRS = {
    "label", "id", "placeholders", "parent", "source", "target",
    "style", "vertex", "edge", "connectable", "as", "value",
}


def parse_tags(cell):
    """Parse a serialized ``k=v;k2=v2`` Tags cell into a dict."""
    out = {}
    if not cell:
        return out
    for pair in cell.split(";"):
        if "=" in pair:
            k, v = pair.split("=", 1)
            k = k.strip()
            if k:
                out[k] = v.strip()
    return out


def _sanitize_attr(key):
    """Turn a tag key into a valid, non-clashing XML attribute name."""
    name = re.sub(r"[^0-9A-Za-z_]", "_", key)
    if not name or not re.match(r"[A-Za-z_]", name[0]):
        name = "t_" + name
    if name in _RESERVED_ATTRS:
        name = "tag_" + name
    return name


def _tag_attributes(tag_map):
    """Build ``{attr_name: value}`` for a draw.io <object>, de-duping collisions."""
    attrs = {}
    for k, v in sorted(tag_map.items()):
        name = _sanitize_attr(k)
        base, n = name, 2
        while name in attrs:
            name = f"{base}_{n}"
            n += 1
        attrs[name] = v
    return attrs


def _set_fill(style, color):
    """Override the fillColor in a draw.io style string."""
    if "fillColor=" in style:
        return re.sub(r"fillColor=[^;]*;", f"fillColor={color};", style)
    return style + f"fillColor={color};"


def build_color_map(rows, key):
    """Map each distinct value of tag *key* to a palette color (deterministic)."""
    values = sorted({parse_tags(r.get("Tags", "")).get(key) for r in rows} - {None})
    cmap = {}
    for i, v in enumerate(values):
        cmap[v] = TAG_PALETTE[i] if i < len(TAG_PALETTE) else OTHER_FILL
    return cmap


# ─────────────────────────────────────────────
# XML builder
# ─────────────────────────────────────────────

def _cell(parent_elem, tag, **attrs):
    """``ET.SubElement`` with attributes passed through the ``attrib`` dict.

    draw.io cells carry a ``parent`` attribute naming their container, which
    collides with ``SubElement``'s own first parameter when passed as a
    keyword (``TypeError: got multiple values for argument 'parent'``).
    """
    return ET.SubElement(parent_elem, tag, {k: str(v) for k, v in attrs.items()})


def build_drawio(rows, account_id="unknown", color_by_tag=None, tag_label=None):
    structure = build_structure(rows)
    color_map = build_color_map(rows, color_by_tag) if color_by_tag else {}

    mxfile = ET.Element("mxfile", host="app.diagrams.net", version="21.0.0")
    diagram = ET.SubElement(mxfile, "diagram", name="AWS Architecture", id="aws-arch")
    mxgraph = ET.SubElement(diagram, "mxGraphModel",
        dx="1422", dy="762", grid="1", gridSize="10",
        guides="1", tooltips="1", connect="1", arrows="1",
        fold="1", page="1", pageScale="1",
        pageWidth="1654", pageHeight="1169",
        math="0", shadow="0")
    root = ET.SubElement(mxgraph, "root")
    ET.SubElement(root, "mxCell", id="0")
    _cell(root, "mxCell", id="1", parent="0")

    cell_id = 2
    node_ids = {}   # (service_type) → list of cell ids (for drawing edges)

    # ── account container ──
    acct_id = str(cell_id); cell_id += 1
    _cell(root, "mxCell",
        id=acct_id, value=f"AWS Account: {account_id}",
        style="points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],[1,1],[0.75,1],[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_account;strokeColor=#CD2264;fillColor=#F4B9DA;verticalLabelPosition=top;align=center;verticalAlign=bottom;spacingTop=25;fontStyle=1;fontSize=14;",
        vertex="1", parent="1"
    ).set("mxGeometry", "")

    # We'll come back and set geometry after we know total size
    acct_cell = root[-1]

    cur_rx = PAD_X
    cur_ry = PAD_Y + 60   # space for account label
    max_rx = 0
    total_height = 0

    for region, cats in sorted(structure.items()):
        # ── region container ──
        reg_cell_id = str(cell_id); cell_id += 1
        reg_label = f"Region: {region}"
        reg_style = "points=[[0,0],[0.25,0],[0.5,0],[0.75,0],[1,0],[1,0.25],[1,0.5],[1,0.75],[1,1],[0.75,1],[0.5,1],[0.25,1],[0,1],[0,0.75],[0,0.5],[0,0.25]];shape=mxgraph.aws4.group;grIcon=mxgraph.aws4.group_region;strokeColor=#147EBA;fillColor=#E6F2F8;verticalLabelPosition=top;align=center;verticalAlign=bottom;spacingTop=25;fontStyle=1;fontSize=12;"
        reg_cell = _cell(root, "mxCell",
            id=reg_cell_id, value=reg_label,
            style=reg_style, vertex="1", parent=acct_id)

        reg_x, reg_y = cur_rx, cur_ry
        reg_inner_x, reg_inner_y = LANE_PAD, REGION_HEADER
        reg_max_col_h = 0
        reg_total_w = 0

        for cat, cat_rows in sorted(cats.items()):
            colors = CATEGORY_COLORS.get(cat, {"fill": "#f5f5f5", "stroke": "#666"})
            items = cat_rows
            cols = max(1, min(6, len(items)))
            rows_count = (len(items) + cols - 1) // cols

            cat_w = cols * (NODE_W + PAD_X) + PAD_X
            cat_h = CAT_HEADER + rows_count * (NODE_H + PAD_Y) + PAD_Y

            # ── category swimlane ──
            cat_cell_id = str(cell_id); cell_id += 1
            cat_style = f"swimlane;startSize=30;fillColor={colors['fill']};strokeColor={colors['stroke']};rounded=1;arcSize=4;fontStyle=1;fontSize=11;"
            cat_cell = _cell(root, "mxCell",
                id=cat_cell_id, value=cat,
                style=cat_style, vertex="1", parent=reg_cell_id)
            geo = ET.SubElement(cat_cell, "mxGeometry",
                x=str(reg_inner_x), y=str(reg_inner_y),
                width=str(cat_w), height=str(cat_h))
            geo.set("as", "geometry")

            # ── nodes inside category ──
            for idx, item in enumerate(items):
                col = idx % cols
                row_n = idx // cols
                nx = PAD_X + col * (NODE_W + PAD_X)
                ny = CAT_HEADER + PAD_Y + row_n * (NODE_H + PAD_Y)

                svc = item["Service"]
                style = SERVICE_STYLES.get(svc,
                    "rounded=1;whiteSpace=wrap;html=1;fillColor=#dae8fc;strokeColor=#6c8ebf;")

                tag_map = parse_tags(item.get("Tags", ""))

                node_cell_id = str(cell_id); cell_id += 1
                label = item["Name"][:18] + ("…" if len(item["Name"]) > 18 else "")
                sublabel = item.get("Type/Size", "")[:16]
                display = f"{label}\n{sublabel}" if sublabel and sublabel != "-" else label
                # Layer 3: append a chosen tag's value under the name.
                if tag_label and tag_map.get(tag_label):
                    display = f"{display}\n{tag_label}: {tag_map[tag_label]}"

                # Layer 2: fill by a chosen tag's value (+ gray for missing).
                node_style = style
                if color_by_tag:
                    node_style = _set_fill(style,
                        color_map.get(tag_map.get(color_by_tag), UNTAGGED_FILL))

                # Layer 1: embed all tags as searchable <object> metadata.
                if tag_map:
                    obj = ET.SubElement(root, "object",
                        {"id": node_cell_id, "label": display,
                         **_tag_attributes(tag_map)})
                    node_cell = _cell(obj, "mxCell",
                        style=node_style, vertex="1", parent=cat_cell_id)
                else:
                    node_cell = _cell(root, "mxCell",
                        id=node_cell_id, value=display,
                        style=node_style, vertex="1", parent=cat_cell_id)
                geo2 = ET.SubElement(node_cell, "mxGeometry",
                    x=str(nx), y=str(ny),
                    width=str(ICON_W), height=str(ICON_H))
                geo2.set("as", "geometry")

                # track by service type for edges (id lives on the object when wrapped)
                node_ids.setdefault(svc, []).append(node_cell_id)

            reg_inner_x += cat_w + LANE_PAD
            reg_max_col_h = max(reg_max_col_h, cat_h)
            reg_total_w = reg_inner_x

        reg_w = reg_total_w + LANE_PAD
        reg_h = reg_max_col_h + REGION_HEADER + LANE_PAD * 2

        geo_reg = ET.SubElement(reg_cell, "mxGeometry",
            x=str(reg_x), y=str(reg_y),
            width=str(reg_w), height=str(reg_h))
        geo_reg.set("as", "geometry")

        cur_ry += reg_h + PAD_Y * 2
        max_rx = max(max_rx, reg_x + reg_w)
        total_height = cur_ry

    # ── account container geometry ──
    acct_geo = ET.SubElement(acct_cell, "mxGeometry",
        x="10", y="10",
        width=str(max_rx + PAD_X * 2),
        height=str(total_height + PAD_Y))
    acct_geo.set("as", "geometry")

    # ── edges between service types ──
    edge_style = "rounded=1;orthogonalLoop=1;jettySize=auto;exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;strokeColor=#555555;strokeWidth=1.5;endArrow=block;endFill=1;"
    for src_type, dst_type in CONNECTIONS:
        srcs = node_ids.get(src_type, [])
        dsts = node_ids.get(dst_type, [])
        if not srcs or not dsts:
            continue
        # connect first of each to avoid edge explosion
        edge_id = str(cell_id); cell_id += 1
        edge_cell = _cell(root, "mxCell",
            id=edge_id, value="",
            style=edge_style,
            edge="1", source=srcs[0], target=dsts[0], parent="1")
        ET.SubElement(edge_cell, "mxGeometry", relative="1").set("as", "geometry")

    # ── legend for --color-by-tag ──
    if color_by_tag and color_map:
        entries = list(color_map.items()) + [("(untagged)", UNTAGGED_FILL)]
        legend_x = 10 + (max_rx + PAD_X * 2) + 40
        legend_h = CAT_HEADER + len(entries) * 24 + 10
        legend_id = str(cell_id); cell_id += 1
        legend = _cell(root, "mxCell", id=legend_id,
            value=f"Legend — {color_by_tag}",
            style="swimlane;startSize=26;fillColor=#ffffff;strokeColor=#666666;rounded=1;fontStyle=1;fontSize=11;",
            vertex="1", parent="1")
        lg = ET.SubElement(legend, "mxGeometry",
            x=str(legend_x), y="20", width="200", height=str(legend_h))
        lg.set("as", "geometry")
        for i, (val, color) in enumerate(entries):
            sw_id = str(cell_id); cell_id += 1
            sw = _cell(root, "mxCell", id=sw_id, value=str(val),
                style=f"rounded=0;whiteSpace=wrap;html=1;fillColor={color};strokeColor=#666666;align=left;spacingLeft=6;fontSize=10;",
                vertex="1", parent=legend_id)
            g = ET.SubElement(sw, "mxGeometry",
                x="8", y=str(CAT_HEADER + i * 24), width="184", height="20")
            g.set("as", "geometry")

    return mxfile


# ─────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Convert AWS inventory CSV to draw.io diagram")
    parser.add_argument("--input",   required=True, help="CSV file from aws_inventory.py")
    parser.add_argument("--output",  default="architecture.drawio", help="Output .drawio file")
    parser.add_argument("--color-by-tag", metavar="KEY", default=None,
                        help="Color-code nodes by a tag key and add a legend (needs a CSV built with --tags)")
    parser.add_argument("--tag-label",    metavar="KEY", default=None,
                        help="Show a tag key's value under each node's name (needs a CSV built with --tags)")
    args = parser.parse_args()

    print(f"Reading {args.input} …")
    rows = read_csv(args.input)
    if not rows:
        print("No rows found in CSV.")
        sys.exit(1)

    has_tags = any(r.get("Tags") for r in rows)
    if has_tags:
        print("Tags detected → embedding as node metadata")
    elif args.color_by_tag or args.tag_label:
        print("⚠️  --color-by-tag/--tag-label given but the CSV has no Tags column.")
        print("    Re-run inventory with --tags first.")

    # Detect account ID from first row
    account_id = rows[0].get("AccountID", "unknown")
    print(f"Account ID : {account_id}")
    print(f"Resources  : {len(rows)}")

    # Count by service
    from collections import Counter
    svc_counts = Counter(r["Service"] for r in rows)
    for svc, cnt in sorted(svc_counts.items()):
        print(f"  {svc:<20} {cnt}")

    print(f"\nGenerating draw.io diagram …")
    mxfile = build_drawio(rows, account_id,
                          color_by_tag=args.color_by_tag, tag_label=args.tag_label)

    xml_str = prettify(mxfile)
    with open(args.output, "w", encoding="utf-8") as f:
        f.write(xml_str)

    print(f"✓ Saved: {args.output}")
    print(f"\nOpen it at: https://app.diagrams.net/")
    print(f"  File → Open from → Device → select {args.output}")


if __name__ == "__main__":
    main()
