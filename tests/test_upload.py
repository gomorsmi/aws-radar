"""Unit tests for the Pump onboarding upload path (stdlib-only, no network)."""

import os
import sys
import urllib.error

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import upload  # noqa: E402  (root-level module)

COLS = ["AccountID", "Service", "Name", "ID", "Type/Size", "Status", "Region", "Extra"]


def _rows():
    return [
        {"AccountID": "123", "Service": "EC2", "Name": "web", "ID": "i-1",
         "Type/Size": "t3.micro", "Status": "running", "Region": "us-east-1",
         "Extra": "10.0.0.1", "_ARN": "arn:aws:ec2:...", "_tags": {}},
    ]


def test_rows_to_csv_string_matches_columns_and_drops_internal_keys():
    out = upload.rows_to_csv_string(_rows(), cols=COLS)
    lines = out.strip().splitlines()
    assert lines[0] == ",".join(COLS)
    assert lines[1].startswith("123,EC2,web,i-1")
    assert "_ARN" not in out and "arn:aws:ec2" not in out  # internal keys ignored


def test_upload_inventory_happy_path(monkeypatch):
    calls = []

    def fake_post(url, token, payload, timeout):
        calls.append(("POST", url, token, payload))
        if url.endswith("/create_upload"):
            return {"upload_url": "https://s3.example/put?sig=abc",
                    "key": "onboarding/7/run-xyz.csv", "run_id": "run-xyz"}
        return {}

    def fake_put(url, body, timeout, content_type="text/csv"):
        calls.append(("PUT", url, len(body)))
        return 200

    monkeypatch.setattr(upload, "_post_json", fake_post)
    monkeypatch.setattr(upload, "_put_bytes", fake_put)

    csv_text = upload.rows_to_csv_string(_rows(), cols=COLS)
    run_id = upload.upload_inventory(csv_text, pump_url="https://api.pump.co/",
                                     pump_token="onb_tok_1", account_id="123")

    assert run_id == "run-xyz"
    kinds = [c[0] for c in calls]
    assert kinds == ["POST", "PUT", "POST"]  # create -> put -> complete, in order
    assert calls[0][2] == "onb_tok_1"  # token sent on create_upload
    assert calls[1][1] == "https://s3.example/put?sig=abc"  # PUT hits the presigned URL


def test_upload_inventory_maps_401_to_clear_error(monkeypatch):
    def raise_401(url, token, payload, timeout):
        raise urllib.error.HTTPError(url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(upload, "_post_json", raise_401)
    with pytest.raises(upload.UploadError, match="401"):
        upload.upload_inventory("h\n1\n", pump_url="https://api.pump.co", pump_token="bad")
