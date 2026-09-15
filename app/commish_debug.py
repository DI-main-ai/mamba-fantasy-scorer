import html
import json
import os
import urllib.parse
from datetime import datetime
from typing import Any, Dict, List, Tuple
from zoneinfo import ZoneInfo

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse

from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import _scalar_map, _walk_values_for_key


commish_debug_router = APIRouter(tags=["commish-debug"])
CENTRAL_TZ = ZoneInfo("America/Chicago")

SENSITIVE_KEY_PARTS = {
    "access_token",
    "refresh_token",
    "client_secret",
    "authorization",
    "oauth",
    "email",
    "guid",
}

INTERESTING_KEY_PARTS = (
    "faab",
    "budget",
    "amount",
    "source",
    "destination",
    "team",
    "type",
    "status",
    "timestamp",
    "description",
    "note",
    "change",
    "value",
    "transaction",
)


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _sanitize(node: Any) -> Any:
    if isinstance(node, list):
        return [_sanitize(item) for item in node]
    if isinstance(node, dict):
        cleaned: Dict[str, Any] = {}
        for key, value in node.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                cleaned[key_text] = "[redacted]"
            else:
                cleaned[key_text] = _sanitize(value)
        return cleaned
    if isinstance(node, (str, int, float, bool)) or node is None:
        return node
    return str(node)


def _collect_scalar_paths(node: Any, prefix: str = "") -> List[Tuple[str, Any]]:
    found: List[Tuple[str, Any]] = []
    if isinstance(node, list):
        for index, item in enumerate(node):
            path = f"{prefix}[{index}]" if prefix else f"[{index}]"
            found.extend(_collect_scalar_paths(item, path))
        return found
    if isinstance(node, dict):
        for key, value in node.items():
            key_text = str(key)
            path = f"{prefix}.{key_text}" if prefix else key_text
            if _is_sensitive_key(key_text):
                continue
            if isinstance(value, (dict, list)):
                found.extend(_collect_scalar_paths(value, path))
            elif isinstance(value, (str, int, float, bool)) or value is None:
                found.append((path, value))
    return found


def _format_time(epoch: Any) -> str:
    try:
        value = float(epoch)
    except (TypeError, ValueError):
        return "Unknown time"
    if value <= 0:
        return "Unknown time"
    return datetime.fromtimestamp(value, tz=CENTRAL_TZ).strftime("%b %-d, %-I:%M:%S %p")


def _current_league_key() -> str:
    league_key = os.getenv("YAHOO_LEAGUE_KEY", "").strip()
    if not league_key:
        raise HTTPException(status_code=503, detail="YAHOO_LEAGUE_KEY is not configured.")
    return league_key


def _commish_resources(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    seen = set()
    for resource in _walk_values_for_key(payload, "transaction"):
        fields = _scalar_map(resource)
        if str(fields.get("type") or "").lower() != "commish":
            continue
        key = str(fields.get("transaction_key") or fields.get("transaction_id") or "")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        scalar_paths = _collect_scalar_paths(resource)
        interesting = [
            (path, value)
            for path, value in scalar_paths
            if any(part in path.lower() for part in INTERESTING_KEY_PARTS)
        ]
        records.append(
            {
                "key": key or "Unknown transaction",
                "transaction_id": fields.get("transaction_id"),
                "timestamp": fields.get("timestamp"),
                "time_label": _format_time(fields.get("timestamp")),
                "status": fields.get("status"),
                "interesting": interesting,
                "all_scalars": scalar_paths,
                "raw": _sanitize(resource),
            }
        )
    records.sort(key=lambda item: float(item.get("timestamp") or 0), reverse=True)
    return records


@commish_debug_router.get("/debug/commish", response_class=HTMLResponse)
@commish_debug_router.get("/debug/commish-transactions", response_class=HTMLResponse)
def commish_transaction_debug(request: Request):
    league_key = _current_league_key()
    encoded_key = urllib.parse.quote(league_key, safe=".-_")
    payload = _fantasy_get(
        request,
        f"league/{encoded_key}/transactions;types=commish;count=200",
    )
    records = _commish_resources(payload)

    cards: List[str] = []
    for index, record in enumerate(records, start=1):
        interesting_rows = "".join(
            f"<tr><td>{html.escape(str(path))}</td><td>{html.escape(str(value))}</td></tr>"
            for path, value in record["interesting"]
        ) or '<tr><td colspan="2">No FAAB/team/amount-like scalar fields found.</td></tr>'

        all_rows = "".join(
            f"<tr><td>{html.escape(str(path))}</td><td>{html.escape(str(value))}</td></tr>"
            for path, value in record["all_scalars"]
        ) or '<tr><td colspan="2">No scalar fields found.</td></tr>'

        raw_json = html.escape(json.dumps(record["raw"], indent=2, ensure_ascii=False))
        cards.append(
            f"""
            <section class="card">
              <div class="card-number">Commissioner transaction {index}</div>
              <h2>{html.escape(record['time_label'])}</h2>
              <div class="meta"><strong>Transaction key:</strong> {html.escape(str(record['key']))}</div>
              <div class="meta"><strong>Status:</strong> {html.escape(str(record.get('status') or '—'))}</div>

              <h3>Likely useful fields</h3>
              <div class="table-wrap"><table><tbody>{interesting_rows}</tbody></table></div>

              <details>
                <summary>Show every scalar field</summary>
                <div class="table-wrap"><table><tbody>{all_rows}</tbody></table></div>
              </details>

              <details>
                <summary>Show sanitized raw Yahoo payload</summary>
                <pre>{raw_json}</pre>
              </details>
            </section>
            """
        )

    if not cards:
        cards.append(
            """
            <section class="card">
              <h2>No commissioner transactions returned</h2>
              <p>Yahoo returned zero transactions with type <code>commish</code> for the current league.</p>
            </section>
            """
        )

    body = "".join(cards)
    return HTMLResponse(
        f"""
        <!doctype html>
        <html lang="en">
        <head>
          <meta charset="utf-8">
          <meta name="viewport" content="width=device-width, initial-scale=1">
          <title>Commissioner Transaction Debug - Mamba Fantasy</title>
          <style>
            :root {{ color-scheme: dark; }}
            * {{ box-sizing: border-box; }}
            body {{ margin:0; background:#0d0d0f; color:#f7f7f8; font-family:Inter,Arial,sans-serif; }}
            main {{ width:min(1100px,calc(100% - 24px)); margin:0 auto; padding:28px 0 60px; }}
            a {{ color:#fdb927; }}
            h1 {{ margin:8px 0; font-size:clamp(28px,7vw,44px); }}
            h1 span,h3 {{ color:#fdb927; }}
            .intro {{ color:#aaa9b2; line-height:1.6; margin-bottom:24px; }}
            .warning {{ border:1px solid #4c3b19; background:#18140c; padding:14px; border-radius:12px; color:#d7c99f; margin:18px 0 26px; }}
            .card {{ background:#18171d; border:1px solid #312d39; border-radius:18px; padding:20px; margin:18px 0; overflow:hidden; }}
            .card-number {{ color:#fdb927; text-transform:uppercase; font-size:12px; font-weight:800; letter-spacing:.1em; }}
            .card h2 {{ margin:8px 0 12px; }}
            .meta {{ color:#b9b7c1; margin:6px 0; overflow-wrap:anywhere; }}
            h3 {{ margin:22px 0 10px; font-size:14px; text-transform:uppercase; letter-spacing:.08em; }}
            details {{ margin-top:16px; }}
            summary {{ cursor:pointer; color:#c7a7e8; font-weight:700; }}
            .table-wrap {{ overflow-x:auto; margin-top:10px; }}
            table {{ width:100%; border-collapse:collapse; font-size:12px; }}
            td {{ vertical-align:top; padding:9px 8px; border-bottom:1px solid #302d35; overflow-wrap:anywhere; }}
            td:first-child {{ color:#aaa9b2; width:46%; }}
            pre {{ white-space:pre-wrap; word-break:break-word; font-size:11px; line-height:1.45; background:#0f0f12; padding:14px; border-radius:12px; overflow:auto; }}
            code {{ color:#c7a7e8; }}
          </style>
        </head>
        <body>
          <main>
            <a href="/">← Back to Mamba Fantasy</a>
            <h1>Commissioner <span>Transaction Debug</span></h1>
            <p class="intro">This temporary test page shows the commissioner transaction fields Yahoo is actually returning for your current league. OAuth credentials, GUIDs, and email-like fields are redacted.</p>
            <div class="warning">Screenshot the “Likely useful fields” for the commissioner adjustments that you know were FAAB transfers. If needed, expand “Show every scalar field” for one of those transactions.</div>
            {body}
          </main>
        </body>
        </html>
        """
    )
