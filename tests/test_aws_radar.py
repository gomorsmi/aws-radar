"""
Basic unit tests for aws-radar.
Uses moto to mock AWS APIs — no real credentials needed.

Run:
    pytest
"""

import pytest
import csv
import io
import sys
import threading


# ── inventory helpers ──

def test_get_tag_found():
    from aws_radar.inventory import get_tag
    tags = [{"Key": "Name", "Value": "my-server"}, {"Key": "Env", "Value": "prod"}]
    assert get_tag(tags) == "my-server"


def test_get_tag_missing():
    from aws_radar.inventory import get_tag
    assert get_tag(None) == "-"
    assert get_tag([]) == "-"
    assert get_tag([{"Key": "Env", "Value": "prod"}]) == "-"


def test_row_helper():
    from aws_radar.inventory import row
    r = row("123", "EC2", "web", "i-abc", "t3.medium", "running", "us-east-1", "1.2.3.4")
    assert r["AccountID"] == "123"
    assert r["Service"] == "EC2"
    assert r["Status"] == "running"


def test_collect_lambda_handles_missing_runtime():
    from aws_radar.inventory import collect_lambda

    class FakePaginator:
        def paginate(self):
            return [{
                "Functions": [
                    {
                        "FunctionName": "my-fn",
                        "FunctionArn": "arn:aws:lambda:eu-central-1:123456789012:function:my-fn",
                        "MemorySize": 256,
                        "State": "Active",
                        "LastModified": "2026-05-04T12:34:56.000+0000",
                    }
                ]
            }]

    class FakeLambdaClient:
        def get_paginator(self, name):
            assert name == "list_functions"
            return FakePaginator()

    class FakeSession:
        def client(self, service, region_name=None):
            assert service == "lambda"
            assert region_name == "eu-central-1"
            return FakeLambdaClient()

    rows = collect_lambda(FakeSession(), "eu-central-1", "123456789012")
    assert len(rows) == 1
    assert rows[0]["Type/Size"] == "- / 256MB"
    assert rows[0]["Name"] == "my-fn"
    assert rows[0]["Region"] == "eu-central-1"


def test_safe_call_swallows_endpoint_connection_error():
    """A service with no endpoint in the region must skip, not crash the run."""
    from aws_radar.inventory import safe_call
    from botocore.exceptions import EndpointConnectionError

    class FakeModel:
        service_name = "lexv2-models"

    class FakeClient:
        _service_model = FakeModel()
        def list_bots(self, **kwargs):
            raise EndpointConnectionError(
                endpoint_url="https://models-v2-lex.us-east-2.amazonaws.com/bots/")

    c = FakeClient()
    assert safe_call(c.list_bots, default={}) == {}


# ── tags ──

def test_tags_to_string_sorted():
    from aws_radar.tags import tags_to_string
    assert tags_to_string({"Team": "pay", "Env": "prod"}) == "Env=prod;Team=pay"
    assert tags_to_string({}) == ""


def test_filter_tags_drops_aws_prefix_by_default():
    from aws_radar.tags import filter_tags
    raw = {"Env": "prod", "aws:cloudformation:stack-name": "core"}
    assert filter_tags(raw) == {"Env": "prod"}
    assert filter_tags(raw, include_aws=True) == raw


def test_row_carries_arn_internal_key():
    from aws_radar.inventory import row, COLS
    r = row("1", "EC2", "web", "i-abc", "t3", "running", "us-east-1", "ip",
            arn="arn:aws:ec2:us-east-1:1:instance/i-abc")
    assert r["_ARN"].endswith("instance/i-abc")
    assert "_ARN" not in COLS  # internal join key, never a displayed column


def test_attach_tags_merges_and_filters(monkeypatch):
    from aws_radar import inventory, tags

    arn = "arn:aws:ec2:us-east-1:1:instance/i-abc"
    monkeypatch.setattr(tags, "fetch_region_tags",
                        lambda s, r: {arn: {"Env": "prod", "aws:x": "sys"}})
    monkeypatch.setattr(tags, "fetch_s3_bucket_tags", lambda s, b: {})
    monkeypatch.setattr(tags, "fetch_cost_allocation_keys", lambda s: {"Env"})

    rows = [inventory.row("1", "EC2", "web", "i-abc", "t3", "running",
                          "us-east-1", "ip", arn=arn)]
    inventory.attach_tags(rows, session=None, regions=["us-east-1"],
                          account_id="1", want_cost_alloc=True)

    assert rows[0]["_tags"] == {"Env": "prod"}          # aws: filtered out
    assert rows[0]["Tags"] == "Env=prod"
    assert rows[0]["CostAllocTags"] == "Env=prod"


def test_export_csv_with_tag_columns(tmp_path):
    from aws_radar.inventory import row, export_csv, export_csv_wide
    import csv as _csv

    r = row("1", "EC2", "web", "i-abc", "t3", "running", "us-east-1", "ip",
            arn="arn:aws:ec2:us-east-1:1:instance/i-abc")
    r["_tags"] = {"Env": "prod", "Team": "pay"}
    r["Tags"] = "Env=prod;Team=pay"

    narrow = tmp_path / "out.csv"
    export_csv([r], str(narrow), extra_cols=["Tags"])
    with open(narrow) as f:
        rd = list(_csv.DictReader(f))
    assert rd[0]["Tags"] == "Env=prod;Team=pay"
    assert "_ARN" not in rd[0]  # internal key not leaked to CSV

    wide = tmp_path / "out-wide.csv"
    export_csv_wide([r], str(wide))
    with open(wide) as f:
        rd = list(_csv.DictReader(f))
    assert rd[0]["tag:Env"] == "prod"
    assert rd[0]["tag:Team"] == "pay"


# ── endpoint pre-check (region skipping) ──

class _RegionSession:
    """Stands in for boto3.Session's endpoint-data lookup."""

    def __init__(self, table):
        self.table = table          # {(service, partition): [regions]}
        self.calls = 0

    def get_available_regions(self, service, partition_name="aws"):
        self.calls += 1
        return list(self.table.get((service, partition_name), []))


@pytest.fixture(autouse=True)
def _clear_region_cache():
    from aws_radar import inventory
    inventory._SERVICE_REGIONS.clear()
    yield
    inventory._SERVICE_REGIONS.clear()


def test_service_available_skips_region_without_endpoint():
    from aws_radar.inventory import service_available

    s = _RegionSession({("kendra", "aws"): ["us-east-1", "eu-west-1"]})
    assert service_available(s, "kendra", "us-east-1") is True
    assert service_available(s, "kendra", "eu-north-1") is False


def test_service_available_fails_open_for_unknown_service():
    """Empty endpoint data must mean 'scan anyway', never 'skip silently'."""
    from aws_radar.inventory import service_available

    s = _RegionSession({})
    assert service_available(s, "some-future-service", "us-east-1") is True


def test_service_available_spans_partitions():
    """GovCloud/China regions live outside the default 'aws' partition — a
    lookup limited to 'aws' would skip every collector for those callers."""
    from aws_radar.inventory import service_available

    s = _RegionSession({
        ("bedrock", "aws"):         ["us-east-1"],
        ("bedrock", "aws-us-gov"):  ["us-gov-west-1"],
    })
    assert service_available(s, "bedrock", "us-gov-west-1") is True
    assert service_available(s, "bedrock", "us-east-1") is True
    assert service_available(s, "bedrock", "eu-north-1") is False


def test_service_regions_are_cached():
    from aws_radar.inventory import service_available

    s = _RegionSession({("polly", "aws"): ["us-east-1"]})
    for _ in range(5):
        service_available(s, "polly", "us-east-1")
    assert s.calls == 7   # one lookup per partition, once — not 5x that


def test_collector_tables_carry_a_service_id():
    from aws_radar.inventory import CORE_COLLECTORS, AI_COLLECTORS

    for label, service, fn in CORE_COLLECTORS + AI_COLLECTORS:
        assert isinstance(service, str) and service, f"{label} has no service id"
        assert callable(fn)
    assert len(AI_COLLECTORS) == 12


# ── collector timeouts (a hung service must not stall the run) ──

def test_run_collector_returns_rows_when_fast():
    from aws_radar.inventory import run_collector

    rows, note = run_collector(lambda s, r, a: [{"ok": 1}], None, "us-east-1", "1",
                               timeout=10)
    assert rows == [{"ok": 1}]
    assert note is None


def test_run_collector_abandons_a_hung_collector():
    """The reported symptom: Bedrock hangs and the whole scan stops."""
    import time
    from aws_radar.inventory import run_collector

    started = threading.Event()

    def hangs(session, region, account_id):
        started.set()
        time.sleep(30)
        return [{"never": "reached"}]

    t0 = time.time()
    rows, note = run_collector(hangs, None, "us-east-1", "1", timeout=0.5)
    elapsed = time.time() - t0

    assert started.wait(1), "collector never ran"
    assert rows == []
    assert "timed out after 0.5s" in note
    assert elapsed < 5, f"run_collector blocked for {elapsed:.1f}s"


def test_run_collector_survives_an_exception():
    """'do not fail the run' — an unexpected error degrades to zero rows."""
    from aws_radar.inventory import run_collector

    def boom(session, region, account_id):
        raise RuntimeError("bedrock exploded")

    rows, note = run_collector(boom, None, "us-east-1", "1", timeout=5)
    assert rows == []
    assert "RuntimeError" in note

    rows, note = run_collector(boom, None, "us-east-1", "1", timeout=None)
    assert rows == []
    assert "RuntimeError" in note


def test_run_collector_is_unbounded_without_a_timeout():
    """Collectors with no configured budget must not be truncated."""
    import time
    from aws_radar.inventory import run_collector

    def slowish(session, region, account_id):
        time.sleep(0.3)
        return [{"row": 1}]

    rows, note = run_collector(slowish, None, "us-east-1", "1", timeout=None)
    assert rows == [{"row": 1}]
    assert note is None


def test_bedrock_has_a_timeout_budget_and_others_do_not():
    from aws_radar.inventory import COLLECTOR_TIMEOUTS

    assert COLLECTOR_TIMEOUTS["bedrock"] == 10
    assert "ec2" not in COLLECTOR_TIMEOUTS
    assert "dynamodb" not in COLLECTOR_TIMEOUTS


def test_bedrock_client_gets_bounded_botocore_timeouts():
    """Without this the abandoned thread keeps retrying for minutes."""
    from aws_radar.inventory import collect_bedrock, BEDROCK_CONFIG

    assert BEDROCK_CONFIG.connect_timeout <= 10
    assert BEDROCK_CONFIG.read_timeout <= 10

    seen = {}

    class FakeSession:
        def client(self, service, region_name=None, config=None):
            seen["service"] = service
            seen["config"] = config
            raise RuntimeError("stop here — we only care about the client kwargs")

    collect_bedrock(FakeSession(), "us-east-1", "1")
    assert seen["service"] == "bedrock"
    assert seen["config"] is BEDROCK_CONFIG


def test_hung_collector_does_not_stop_the_scan(monkeypatch, capsys):
    """End-to-end: a hanging Bedrock leaves later collectors running."""
    import time
    from aws_radar import inventory

    def hangs(session, region, account_id):
        time.sleep(30)
        return []

    monkeypatch.setattr(inventory, "collect_s3", lambda s, a: [])
    monkeypatch.setattr(inventory, "CORE_COLLECTORS", [])
    monkeypatch.setattr(inventory, "AI_COLLECTORS", [
        ("Bedrock models/throughput", "bedrock", hangs),
        ("Polly lexicons", "polly",
         lambda s, r, a: [inventory.row("1", "Polly Lexicon", "lex", "lex",
                                        "-", "active", r, "-", arn="arn:x")]),
    ])
    monkeypatch.setattr(inventory, "COLLECTOR_TIMEOUTS", {"bedrock": 0.5})
    monkeypatch.setattr(inventory, "service_available", lambda s, svc, r: True)

    t0 = time.time()
    rows = inventory.run_inventory(["us-east-1"], None, "1")
    elapsed = time.time() - t0

    out = capsys.readouterr().out
    assert "timed out after 0.5s" in out
    assert elapsed < 5, f"scan stalled for {elapsed:.1f}s"
    assert len(rows) == 1 and rows[0]["Service"] == "Polly Lexicon"


# ── --ai default-on / --no-ai opt-out ──

def test_rebuild_argv_emits_explicit_ai_flag_in_both_states():
    """A False-but-meaningful boolean must round-trip. inventory.py re-parses
    this argv with its own default, so an omitted flag means the user's choice
    is silently discarded."""
    import argparse
    from aws_radar.__main__ import _rebuild_argv

    on = argparse.Namespace(command="inventory", ai=True)
    off = argparse.Namespace(command="inventory", ai=False)

    assert _rebuild_argv("inventory", on) == ["aws-radar", "--ai"]
    assert _rebuild_argv("inventory", off) == ["aws-radar", "--no-ai"]


def test_rebuild_argv_omits_plain_store_true_flags_when_false():
    """Only --ai is negatable; --tags and friends still emit nothing."""
    import argparse
    from aws_radar.__main__ import _rebuild_argv

    args = argparse.Namespace(
        command="inventory", region=None, profile=None, role_arn=None,
        all_regions=False, ai=True, tags=False, tags_wide=False,
        cost_allocation_tags=False, include_aws_tags=False, billing=False,
        billing_days=None, billing_metric=None, billing_output=None,
        include_zero=False, export=None,
    )
    assert _rebuild_argv("inventory", args) == ["aws-radar", "--ai"]


def test_rebuild_argv_still_passes_values_and_true_flags():
    import argparse
    from aws_radar.__main__ import _rebuild_argv

    args = argparse.Namespace(command="inventory", ai=False, tags=True,
                              billing_days=30, export="out.csv", region=None)
    assert _rebuild_argv("inventory", args) == [
        "aws-radar", "--no-ai", "--tags", "--billing-days", "30",
        "--export", "out.csv"]


class _FakeSTS:
    def get_caller_identity(self):
        return {"Account": "123456789012",
                "Arn": "arn:aws:iam::123456789012:user/tester"}


class _FakeAWSSession:
    region_name = "us-east-1"

    def __init__(self, profile_name=None):
        self.profile_name = profile_name

    def client(self, service, region_name=None):
        assert service == "sts"
        return _FakeSTS()


class _FakeBoto3:
    Session = _FakeAWSSession


@pytest.fixture
def captured_inventory(monkeypatch):
    """Stub AWS out of inventory.main() but keep its real argparse parser."""
    from aws_radar import inventory

    seen = {}

    def fake_run_inventory(regions, session, account_id, include_ai=True, **kw):
        seen["include_ai"] = include_ai
        return []

    monkeypatch.setattr(inventory, "boto3", _FakeBoto3)
    monkeypatch.setattr(inventory, "run_inventory", fake_run_inventory)
    monkeypatch.setattr(inventory, "display_table", lambda *a, **k: None)
    return seen


@pytest.mark.parametrize("argv, expected", [
    (["aws-radar", "inventory"],            True),    # new default: on
    (["aws-radar", "inventory", "--ai"],    True),    # back-compat no-op
    (["aws-radar", "inventory", "--no-ai"], False),   # opt-out must survive
])
def test_ai_choice_survives_the_two_parser_round_trip(
        monkeypatch, captured_inventory, argv, expected):
    """__main__ parses, re-serializes to sys.argv, inventory.py parses again."""
    from aws_radar.__main__ import main as cli_main

    monkeypatch.setattr(sys, "argv", argv)
    cli_main()
    assert captured_inventory["include_ai"] is expected


def test_inventory_parser_accepts_no_ai_directly(monkeypatch, captured_inventory):
    """Guards drift: __main__ updated but inventory.py forgotten would raise
    SystemExit(2) on 'unrecognized arguments: --no-ai'."""
    from aws_radar.inventory import main as inv_main

    monkeypatch.setattr(sys, "argv", ["aws-radar", "--no-ai"])
    inv_main()
    assert captured_inventory["include_ai"] is False


@pytest.mark.parametrize("flags, present, absent", [
    ([],           "--ai",    "--no-ai"),
    (["--ai"],     "--ai",    "--no-ai"),
    (["--no-ai"],  "--no-ai", "--ai"),
])
def test_run_forwards_ai_choice_to_the_inventory_step(
        monkeypatch, flags, present, absent):
    from aws_radar import inventory, drawio
    from aws_radar.__main__ import main as cli_main

    captured = {}
    monkeypatch.setattr(inventory, "main",
                        lambda: captured.__setitem__("argv", list(sys.argv)))
    monkeypatch.setattr(drawio, "main", lambda: None)
    monkeypatch.setattr(sys, "argv", ["aws-radar", "run"] + flags)

    cli_main()
    assert present in captured["argv"]
    assert absent not in captured["argv"]


def test_run_inventory_defaults_to_including_ai():
    """Public API default must match the CLI default (README documents it)."""
    import inspect
    from aws_radar.inventory import run_inventory

    assert inspect.signature(run_inventory).parameters["include_ai"].default is True


# ── AI resource ARNs (needed for the --tags join) ──

def test_ai_collector_passes_through_response_arn():
    """collect_personalize has the ARN in hand — it must reach the row."""
    from aws_radar.inventory import collect_personalize
    from datetime import datetime

    arn = "arn:aws:personalize:us-east-1:123456789012:campaign/my-campaign"

    class FakeClient:
        def list_campaigns(self, **kw):
            return {"campaigns": [{
                "name": "my-campaign", "campaignArn": arn, "status": "ACTIVE",
                "lastUpdatedDateTime": datetime(2026, 8, 1)}]}

    class FakeSession:
        def client(self, service, region_name=None):
            return FakeClient()

    rows = collect_personalize(FakeSession(), "us-east-1", "123456789012")
    assert len(rows) == 1
    assert rows[0]["_ARN"] == arn


def test_ai_collector_constructs_missing_arn():
    """ListIndices returns no ARN, so collect_kendra must build one."""
    from aws_radar.inventory import collect_kendra
    from datetime import datetime

    class FakeClient:
        def list_indices(self, **kw):
            return {"IndexConfigurationSummaryItems": [{
                "Name": "docs", "Id": "idx-123", "Edition": "DEVELOPER_EDITION",
                "Status": "ACTIVE", "UpdatedAt": datetime(2026, 8, 1)}]}

    class FakeSession:
        def client(self, service, region_name=None):
            return FakeClient()

    rows = collect_kendra(FakeSession(), "eu-west-1", "123456789012")
    assert len(rows) == 1
    assert rows[0]["_ARN"] == "arn:aws:kendra:eu-west-1:123456789012:index/idx-123"


def test_every_ai_collector_call_site_supplies_an_arn():
    """Guard against a new AI collector landing without arn= — its rows would
    silently join to nothing in the tagging API."""
    import ast, inspect
    from aws_radar import inventory

    src = inspect.getsource(inventory)
    tree = ast.parse(src)
    ai_fns = {fn.__name__ for _, _, fn in inventory.AI_COLLECTORS}

    missing = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.FunctionDef) and node.name in ai_fns):
            continue
        for call in ast.walk(node):
            if (isinstance(call, ast.Call)
                    and isinstance(call.func, ast.Name)
                    and call.func.id == "row"):
                if not any(kw.arg == "arn" for kw in call.keywords):
                    missing.append(f"{node.name}:{call.lineno}")
    assert not missing, f"row() without arn= in AI collectors: {missing}"


# ── billing / Cost Explorer ──

def test_date_window_is_trailing_and_end_exclusive():
    from datetime import date
    from aws_radar.billing import date_window

    start, end = date_window(days=90, today=date(2026, 8, 19))
    assert end == "2026-08-19"      # exclusive — covers through 2026-08-18
    assert start == "2026-05-21"


class _FakeCE:
    """Two-page Cost Explorer response with one zero-cost bucket."""

    def __init__(self):
        self.calls = []

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(kwargs)
        if "NextPageToken" not in kwargs:
            return {
                "ResultsByTime": [{
                    "TimePeriod": {"Start": "2026-05-21", "End": "2026-05-22"},
                    "Groups": [
                        {"Keys": ["Amazon Elastic Compute Cloud - Compute"],
                         "Metrics": {"UnblendedCost": {"Amount": "12.5", "Unit": "USD"}}},
                        {"Keys": ["AWS Key Management Service"],
                         "Metrics": {"UnblendedCost": {"Amount": "0", "Unit": "USD"}}},
                    ],
                }],
                "NextPageToken": "page2",
            }
        return {
            "ResultsByTime": [{
                "TimePeriod": {"Start": "2026-05-22", "End": "2026-05-23"},
                "Groups": [
                    {"Keys": ["Amazon Simple Storage Service"],
                     "Metrics": {"UnblendedCost": {"Amount": "3.25", "Unit": "USD"}}},
                ],
            }],
        }


class _FakeSession:
    def __init__(self, ce):
        self._ce = ce

    def client(self, service, region_name=None):
        assert service == "ce"
        assert region_name == "us-east-1"   # Cost Explorer is us-east-1 only
        return self._ce


def test_fetch_daily_cost_paginates_and_drops_zeros():
    from aws_radar.billing import fetch_daily_cost_by_service

    ce = _FakeCE()
    rows = fetch_daily_cost_by_service(_FakeSession(ce), "123456789012", days=90)

    assert len(ce.calls) == 2                       # followed NextPageToken
    assert ce.calls[0]["Granularity"] == "DAILY"
    assert ce.calls[0]["GroupBy"] == [{"Type": "DIMENSION", "Key": "SERVICE"}]
    assert len(rows) == 2                           # zero-cost KMS bucket dropped
    assert rows[0]["Date"] == "2026-05-21"
    assert rows[0]["Service"] == "Amazon Elastic Compute Cloud - Compute"
    assert rows[0]["Amount"] == "12.500000"
    assert rows[0]["AccountID"] == "123456789012"
    assert rows[1]["Service"] == "Amazon Simple Storage Service"


def test_fetch_daily_cost_include_zero_keeps_empty_buckets():
    from aws_radar.billing import fetch_daily_cost_by_service

    rows = fetch_daily_cost_by_service(_FakeSession(_FakeCE()), "1",
                                       days=90, include_zero=True)
    assert len(rows) == 3
    assert any(r["Amount"] == "0.000000" for r in rows)


def test_fetch_daily_cost_returns_partial_rows_on_access_denied():
    from aws_radar.billing import fetch_daily_cost_by_service
    from botocore.exceptions import ClientError

    class DeniedCE:
        def get_cost_and_usage(self, **kwargs):
            raise ClientError(
                {"Error": {"Code": "AccessDeniedException", "Message": "no ce"}},
                "GetCostAndUsage")

    assert fetch_daily_cost_by_service(_FakeSession(DeniedCE()), "1") == []


def test_totals_by_service_sorted_descending():
    from aws_radar.billing import totals_by_service

    rows = [
        {"Service": "EC2", "Amount": "10.0"},
        {"Service": "S3",  "Amount": "5.0"},
        {"Service": "EC2", "Amount": "2.5"},
    ]
    assert totals_by_service(rows) == [("EC2", 12.5), ("S3", 5.0)]


def test_export_billing_csv(tmp_path):
    from aws_radar.billing import export_billing_csv, BILLING_COLS

    rows = [{"AccountID": "1", "Date": "2026-05-21", "Service": "EC2",
             "Amount": "12.500000", "Currency": "USD"}]
    out = tmp_path / "billing.csv"
    export_billing_csv(rows, str(out))

    with open(out) as f:
        reader = csv.DictReader(f)
        read = list(reader)
    assert reader.fieldnames == BILLING_COLS
    assert read[0]["Service"] == "EC2"
    assert read[0]["Amount"] == "12.500000"


def test_cli_metrics_match_billing_metrics():
    """__main__ duplicates the metric list so --help works without boto3."""
    from aws_radar.__main__ import METRICS as cli_metrics
    from aws_radar.billing import METRICS as billing_metrics
    assert cli_metrics == billing_metrics


# ── forecasting ──

def test_forecast_window_spans_calendar_months_from_today():
    from datetime import date
    from aws_radar.billing import forecast_window

    assert forecast_window(6, today=date(2026, 10, 28)) == (
        "2026-10-01", "2026-10-28", "2027-04-01")    # Oct..Mar, end exclusive
    assert forecast_window(1, today=date(2026, 12, 5)) == (
        "2026-12-01", "2026-12-05", "2027-01-01")


class _FakeForecastCE:
    def __init__(self, actual="40", deny=None):
        self.calls = []
        self.actual = actual
        self.deny = deny

    def get_cost_and_usage(self, **kwargs):
        self.calls.append(("usage", kwargs))
        return {"ResultsByTime": [{
            "Total": {"UnblendedCost": {"Amount": self.actual, "Unit": "USD"}}}]}

    def get_cost_forecast(self, **kwargs):
        from botocore.exceptions import ClientError
        self.calls.append(("forecast", kwargs))
        if self.deny:
            raise ClientError({"Error": {"Code": self.deny, "Message": "x"}},
                              "GetCostForecast")
        return {
            "Total": {"Amount": "260", "Unit": "USD"},
            "ForecastResultsByTime": [
                {"TimePeriod": {"Start": "2026-10-28", "End": "2026-11-01"},
                 "MeanValue": "10", "PredictionIntervalLowerBound": "8",
                 "PredictionIntervalUpperBound": "12"},
                {"TimePeriod": {"Start": "2026-11-01", "End": "2026-12-01"},
                 "MeanValue": "100", "PredictionIntervalLowerBound": "90",
                 "PredictionIntervalUpperBound": "115"},
            ],
        }


def test_fetch_monthly_forecast_adds_actuals_to_the_current_month():
    from datetime import date
    from aws_radar.billing import fetch_monthly_forecast

    ce = _FakeForecastCE()
    rows = fetch_monthly_forecast(_FakeSession(ce), "123456789012", months=2,
                                  today=date(2026, 10, 28))

    usage, forecast = ce.calls[0][1], ce.calls[1][1]
    assert usage["TimePeriod"] == {"Start": "2026-10-01", "End": "2026-10-28"}
    assert forecast["TimePeriod"] == {"Start": "2026-10-28", "End": "2026-12-01"}
    assert forecast["Metric"] == "UNBLENDED_COST"
    assert forecast["Granularity"] == "MONTHLY"

    assert [r["Month"] for r in rows] == ["2026-10", "2026-11"]
    # October: 40 already spent + 10 forecast for the remaining days.
    assert (rows[0]["Projected"], rows[0]["LowerBound"], rows[0]["UpperBound"]) == (
        "50.00", "48.00", "52.00")
    assert rows[0]["ActualToDate"] == "40.00"
    # November is pure forecast.
    assert (rows[1]["Projected"], rows[1]["ActualToDate"]) == ("100.00", "0.00")


def test_fetch_monthly_forecast_skips_actuals_on_the_first_of_the_month():
    """Start == End is invalid for GetCostAndUsage, and there is nothing to sum."""
    from datetime import date
    from aws_radar.billing import fetch_monthly_forecast

    ce = _FakeForecastCE()
    fetch_monthly_forecast(_FakeSession(ce), "1", months=6, today=date(2026, 11, 1))
    assert [kind for kind, _ in ce.calls] == ["forecast"]


def test_fetch_monthly_forecast_returns_empty_on_error(capsys):
    from datetime import date
    from aws_radar.billing import fetch_monthly_forecast

    ce = _FakeForecastCE(deny="AccessDeniedException")
    assert fetch_monthly_forecast(_FakeSession(ce), "1", today=date(2026, 10, 28)) == []
    assert "ce:GetCostForecast" in capsys.readouterr().out


def test_every_billing_metric_has_a_forecast_metric():
    from aws_radar.billing import METRICS, FORECAST_METRICS
    assert set(FORECAST_METRICS) == set(METRICS)


def test_cli_forecast_months_match_billing_limit():
    from aws_radar.__main__ import FORECAST_MONTHS
    from aws_radar.billing import MAX_FORECAST_MONTHS
    assert FORECAST_MONTHS == range(1, MAX_FORECAST_MONTHS + 1)


def test_forecast_title():
    from aws_radar.billing import forecast_title
    assert forecast_title(6) == "Projected monthly spend AWS · Forecast horizon: 6 months"
    assert forecast_title(1) == "Projected monthly spend AWS · Forecast horizon: 1 month"


def test_export_forecast_csv(tmp_path):
    from aws_radar.billing import export_forecast_csv, FORECAST_COLS

    rows = [{"AccountID": "1", "Month": "2026-11", "Projected": "100.00",
             "LowerBound": "90.00", "UpperBound": "115.00",
             "ActualToDate": "0.00", "Currency": "USD"}]
    out = tmp_path / "forecast.csv"
    export_forecast_csv(rows, str(out))

    with open(out) as f:
        reader = csv.DictReader(f)
        read = list(reader)
    assert reader.fieldnames == FORECAST_COLS
    assert read[0]["Projected"] == "100.00"


@pytest.mark.parametrize("argv, expected", [
    (["aws-radar", "inventory"], None),
    (["aws-radar", "inventory", "--forecasting"], (6, "forecast.csv")),
    (["aws-radar", "inventory", "--forecasting", "--forecast-months", "12",
      "--forecast-output", "f.csv"], (12, "f.csv")),
])
def test_forecasting_survives_the_two_parser_round_trip(
        monkeypatch, captured_inventory, argv, expected):
    from aws_radar import billing
    from aws_radar.__main__ import main as cli_main

    seen = []
    monkeypatch.setattr(billing, "run_forecast",
                        lambda s, a, months, metric, output: seen.append((months, output)))
    monkeypatch.setattr(sys, "argv", argv)
    cli_main()
    assert seen == ([expected] if expected else [])


def test_forecast_months_rejects_beyond_cost_explorer_limit(monkeypatch):
    from aws_radar.__main__ import main as cli_main

    monkeypatch.setattr(sys, "argv",
                        ["aws-radar", "billing", "--forecasting", "--forecast-months", "19"])
    with pytest.raises(SystemExit):
        cli_main()


def test_run_forwards_forecasting_to_the_inventory_step(monkeypatch):
    from aws_radar import inventory, drawio
    from aws_radar.__main__ import main as cli_main

    captured = {}
    monkeypatch.setattr(inventory, "main",
                        lambda: captured.__setitem__("argv", list(sys.argv)))
    monkeypatch.setattr(drawio, "main", lambda: None)
    monkeypatch.setattr(sys, "argv",
                        ["aws-radar", "run", "--forecasting", "--forecast-months", "3"])

    cli_main()
    argv = captured["argv"]
    assert "--forecasting" in argv
    assert argv[argv.index("--forecast-months") + 1] == "3"


# ── drawio generation ──

def test_build_drawio_returns_xml():
    from aws_radar.drawio import build_drawio
    import xml.etree.ElementTree as ET

    sample = [
        {"AccountID": "123456789012", "Service": "EC2", "Name": "web-01",
         "ID": "i-abc", "Type/Size": "t3.medium", "Status": "running",
         "Region": "us-east-1", "Extra": "1.2.3.4"},
        {"AccountID": "123456789012", "Service": "RDS", "Name": "prod-db",
         "ID": "prod-db", "Type/Size": "db.r6g.large", "Status": "available",
         "Region": "us-east-1", "Extra": "mysql 8.0"},
        {"AccountID": "123456789012", "Service": "S3", "Name": "my-bucket",
         "ID": "my-bucket", "Type/Size": "-", "Status": "active",
         "Region": "global", "Extra": "Created: 2022-01-01"},
    ]

    mxfile = build_drawio(sample, account_id="123456789012")
    assert mxfile.tag == "mxfile"

    xml_str = ET.tostring(mxfile, encoding="unicode")
    assert "ec2" in xml_str
    assert "rds" in xml_str
    assert "us-east-1" in xml_str


def test_build_drawio_empty():
    from aws_radar.drawio import build_drawio
    # Should not raise even with no rows
    mxfile = build_drawio([], account_id="000000000000")
    assert mxfile.tag == "mxfile"


# ── CSV round-trip ──

def test_csv_columns():
    from aws_radar.inventory import COLS
    expected = {"AccountID", "Service", "Name", "ID", "Type/Size", "Status", "Region", "Extra"}
    assert expected == set(COLS)


def test_export_csv(tmp_path):
    from aws_radar.inventory import export_csv, COLS

    rows = [
        {c: f"val_{c}" for c in COLS},
        {c: f"val2_{c}" for c in COLS},
    ]
    out = tmp_path / "test.csv"
    export_csv(rows, str(out))

    with open(out) as f:
        reader = csv.DictReader(f)
        read_rows = list(reader)

    assert len(read_rows) == 2
    assert read_rows[0]["Service"] == "val_Service"
    assert set(reader.fieldnames) == set(COLS)
