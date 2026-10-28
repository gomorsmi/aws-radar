"""
Push an inventory CSV to Pump's onboarding ingest.

Enabled by the ``--pump-token`` / ``--pump-url`` flags: after an inventory run,
the rows are serialized to CSV in memory and uploaded to a Pump-owned S3 bucket
via a presigned PUT that Pump mints on demand. The flow is:

    1. POST {pump_url}/api/v1/data/inventory/create_upload  (X-Pump-Api-Key)
         -> {"upload_url": ..., "key": ..., "run_id": ...}
    2. PUT  <upload_url>   (the CSV bytes, straight to S3 -- never through Pump)
    3. POST {pump_url}/api/v1/data/inventory/complete       (X-Pump-Api-Key)

Only the Python standard library (urllib) is used, so the tool gains no new
dependency; the presigned URL needs no AWS SDK and no Pump credentials on the
client -- authorization travels in the URL itself.
"""

from __future__ import annotations

import csv
import io
import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 30
_API_BASE = "/api/v1/data/inventory"


class UploadError(RuntimeError):
    """Raised when the inventory cannot be pushed to Pump."""


def _default_columns(extra_cols=None):
    # Imported lazily so this module stays importable (and unit-testable) on its
    # own; callers that already know the columns pass them via ``cols=``.
    from aws_radar.inventory import COLS
    return COLS + list(extra_cols or [])


def rows_to_csv_string(rows, extra_cols=None, cols=None):
    """Serialize inventory rows to a CSV string, matching the ``--export`` layout."""
    columns = cols if cols is not None else _default_columns(extra_cols)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=columns, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def _post_json(url, token, payload, timeout):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("X-Pump-Api-Key", token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body else {}


def _put_bytes(url, body, timeout, content_type="text/csv"):
    req = urllib.request.Request(url, data=body, method="PUT")
    if content_type:
        req.add_header("Content-Type", content_type)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return getattr(resp, "status", 200)


def upload_inventory(csv_text, *, pump_url, pump_token, account_id=None, timeout=DEFAULT_TIMEOUT):
    """Push *csv_text* to Pump. Returns the run id on success; raises ``UploadError``."""
    base = pump_url.rstrip("/") + _API_BASE
    body = csv_text.encode("utf-8")
    row_count = max(csv_text.count("\n") - 1, 0)  # rows minus the header line
    try:
        created = _post_json(f"{base}/create_upload", pump_token, {"account_id": account_id}, timeout)
        upload_url = created["upload_url"]
        run_id = created.get("run_id") or created.get("key")
        _put_bytes(upload_url, body, timeout)
        _post_json(f"{base}/complete", pump_token,
                   {"run_id": created.get("run_id"), "row_count": row_count}, timeout)
        return run_id
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:500] if hasattr(e, "read") else ""
        if e.code == 401:
            raise UploadError("Pump rejected the token (401) - it may be invalid, expired, or revoked.") from e
        if e.code == 429:
            raise UploadError("Pump rate limit reached (429) - too many exports this hour; retry later.") from e
        raise UploadError(f"Pump upload failed (HTTP {e.code}): {detail}") from e
    except urllib.error.URLError as e:
        raise UploadError(f"Could not reach Pump at {pump_url}: {e.reason}") from e
    except (KeyError, ValueError) as e:
        raise UploadError(f"Unexpected response from Pump: {e}") from e
