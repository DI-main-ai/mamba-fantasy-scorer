import asyncio
import json
import os
import time
import urllib.parse
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple
from zoneinfo import ZoneInfo

from fastapi import Request
from starlette.requests import Request as StarletteRequest

from app.yahoo_auth import _fantasy_get
from app.yahoo_dashboard import _extract_team_logo_urls
from app.yahoo_mamba import _first_value_for_key, _scalar_map, _walk_values_for_key
from app.yahoo_shared_auth import _upstash_command, _upstash_config


TRADE_TRACKER_PREFIX = "mamba:trade-tracker:v1"
TRACKER_LOCK_KEY = f"{TRADE_TRACKER_PREFIX}:collector-lock"
CURRENT_STATUS_KEY = f"{TRADE_TRACKER_PREFIX}:current-status"
CURRENT_SEASON_REFRESH_SECONDS = 60
HISTORICAL_CACHE_SECONDS = 3600
FAAB_MATCH_WINDOW_SECONDS = 60 * 60
COMMISH_EVIDENCE_WINDOW_SECONDS = 12 * 60
PENDING_CHANGE_TTL_SECONDS = 20 * 60
CENTRAL_TZ = ZoneInfo("America/Chicago")
EPSILON = 0.0001

_tracker_task: Optional[asyncio.Task] = None


def _server_request() -> StarletteRequest:
    return StarletteRequest(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/internal/trade-tracker",
            "raw_path": b"/internal/trade-tracker",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 0),
            "server": ("127.0.0.1", 443),
            "session": {},
        }
    )


def _state_key(season: int) -> str:
    return f"{TRADE_TRACKER_PREFIX}:{int(season)}:state"


def _snapshot_key(season: int) -> str:
    return f"{TRADE_TRACKER_PREFIX}:{int(season)}:faab-snapshot"


def _associations_key(season: int) -> str:
    return f"{TRADE_TRACKER_PREFIX}:{int(season)}:faab-associations"


def _read_json(key: str) -> Optional[Dict[str, Any]]:
    if _upstash_config() is None:
        return None
    try:
        raw = _upstash_command(["GET", key])
        if raw in (None, ""):
            return None
        value = json.loads(str(raw))
        return value if isinstance(value, dict) else None
    except Exception as exc:
        print(f"WARNING: trade tracker read failed for {key}: {exc}")
        return None


def _write_json(key: str, value: Dict[str, Any], ttl: Optional[int] = None) -> None:
    if _upstash_config() is None:
        return
    command: List[Any] = [
        "SET",
        key,
        json.dumps(value, separators=(",", ":"), default=str),
    ]
    if ttl:
        command.extend(["EX", max(60, int(ttl))])
    _upstash_command(command)


def _as_float(value: Any) -> Optional[float]:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _player_name(player_resource: Any) -> str:
    name_node = _first_value_for_key(player_resource, "name")
    fields = _scalar_map(name_node)
    full = fields.get("full")
    if full:
        return str(full)
    first = str(fields.get("first") or "").strip()
    last = str(fields.get("last") or "").strip()
    return " ".join(part for part in (first, last) if part) or "Unknown Player"


def _team_key_values(node: Any) -> List[str]:
    values: List[str] = []
    wanted = {
        "team_key",
        "source_team_key",
        "destination_team_key",
        "trader_team_key",
        "tradee_team_key",
    }

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if key in wanted and isinstance(item, str) and ".t." in item:
                values.append(item)
            else:
                visit(item)

    visit(node)
    return list(dict.fromkeys(values))


def _numeric_values_for_keys(node: Any, wanted: Iterable[str]) -> List[float]:
    wanted_set = {str(item).lower() for item in wanted}
    values: List[float] = []

    def visit(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        for key, item in value.items():
            if str(key).lower() in wanted_set:
                parsed = _as_float(item)
                if parsed is not None:
                    values.append(parsed)
            if isinstance(item, (dict, list)):
                visit(item)

    visit(node)
    return values


def _extract_team_states(payload: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    logos = _extract_team_logo_urls(payload)
    teams: Dict[str, Dict[str, Any]] = {}
    for team_resource in _walk_values_for_key(payload, "team"):
        fields = _scalar_map(team_resource)
        team_key = fields.get("team_key")
        if not team_key:
            continue
        key = str(team_key)
        teams[key] = {
            "team_key": key,
            "name": str(fields.get("name") or "Yahoo Team"),
            "logo_url": logos.get(key, ""),
            "faab_balance": _as_float(fields.get("faab_balance")),
        }
    return teams


def _extract_trade_sides(
    transaction_resource: Any,
    team_names: Dict[str, str],
) -> List[Dict[str, Any]]:
    transaction_fields = _scalar_map(transaction_resource)
    trader_key = str(transaction_fields.get("trader_team_key") or "")
    tradee_key = str(transaction_fields.get("tradee_team_key") or "")
    sides: Dict[str, Dict[str, Any]] = {}

    for key in (trader_key, tradee_key):
        if key:
            sides.setdefault(
                key,
                {"team_key": key, "team_name": team_names.get(key, "Yahoo Team"), "players": []},
            )

    for player_resource in _walk_values_for_key(transaction_resource, "player"):
        player_fields = _scalar_map(player_resource)
        player_key = str(player_fields.get("player_key") or "")
        if not player_key:
            continue
        transaction_data = _first_value_for_key(player_resource, "transaction_data")
        data_fields = _scalar_map(transaction_data)
        source_key = str(data_fields.get("source_team_key") or "")
        destination_key = str(data_fields.get("destination_team_key") or "")

        if not source_key and destination_key:
            if destination_key == trader_key:
                source_key = tradee_key
            elif destination_key == tradee_key:
                source_key = trader_key

        if not source_key:
            continue

        side = sides.setdefault(
            source_key,
            {
                "team_key": source_key,
                "team_name": team_names.get(source_key, "Yahoo Team"),
                "players": [],
            },
        )
        player_name = _player_name(player_resource)
        if player_name not in side["players"]:
            side["players"].append(player_name)

        if destination_key:
            sides.setdefault(
                destination_key,
                {
                    "team_key": destination_key,
                    "team_name": team_names.get(destination_key, "Yahoo Team"),
                    "players": [],
                },
            )

    return list(sides.values())


def _extract_transactions(
    payload: Dict[str, Any],
    team_states: Dict[str, Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    team_names = {
        team_key: str(team.get("name") or "Yahoo Team")
        for team_key, team in team_states.items()
    }
    trades: Dict[str, Dict[str, Any]] = {}
    commish: Dict[str, Dict[str, Any]] = {}

    for transaction_resource in _walk_values_for_key(payload, "transaction"):
        fields = _scalar_map(transaction_resource)
        transaction_key = str(fields.get("transaction_key") or "")
        transaction_type = str(fields.get("type") or "").lower()
        if not transaction_key or transaction_type not in {"trade", "commish"}:
            continue

        timestamp = _as_int(fields.get("timestamp")) or 0
        status = str(fields.get("status") or "")

        if transaction_type == "trade":
            sides = _extract_trade_sides(transaction_resource, team_names)
            team_keys = sorted(
                {
                    str(side.get("team_key") or "")
                    for side in sides
                    if side.get("team_key")
                }
            )
            if len(team_keys) < 2:
                team_keys = sorted(
                    {
                        key
                        for key in (
                            str(fields.get("trader_team_key") or ""),
                            str(fields.get("tradee_team_key") or ""),
                        )
                        if key
                    }
                )
            trades[transaction_key] = {
                "transaction_key": transaction_key,
                "transaction_id": fields.get("transaction_id"),
                "timestamp": timestamp,
                "status": status,
                "team_keys": team_keys,
                "sides": sides,
            }
        else:
            commish[transaction_key] = {
                "transaction_key": transaction_key,
                "transaction_id": fields.get("transaction_id"),
                "timestamp": timestamp,
                "status": status,
                "team_keys": _team_key_values(transaction_resource),
                "faab_amount_candidates": _numeric_values_for_keys(
                    transaction_resource,
                    {"faab", "faab_amount", "faab_bid", "budget_amount"},
                ),
                "source_team_key": str(fields.get("source_team_key") or ""),
                "destination_team_key": str(fields.get("destination_team_key") or ""),
            }

    return (
        sorted(trades.values(), key=lambda item: int(item.get("timestamp") or 0)),
        sorted(commish.values(), key=lambda item: int(item.get("timestamp") or 0)),
    )


def _fetch_trade_inputs(
    request: Request,
    league_key: str,
) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    encoded_key = urllib.parse.quote(league_key, safe=".-_")
    teams_payload = _fantasy_get(request, f"league/{encoded_key}/teams")
    team_states = _extract_team_states(teams_payload)
    transactions_payload = _fantasy_get(
        request,
        f"league/{encoded_key}/transactions;types=trade,commish;count=200",
    )
    trades, commish = _extract_transactions(transactions_payload, team_states)
    return team_states, trades, commish


def _load_associations(season: int) -> Dict[str, Dict[str, Any]]:
    stored = _read_json(_associations_key(season)) or {}
    value = stored.get("associations")
    return dict(value) if isinstance(value, dict) else {}


def _save_associations(season: int, associations: Dict[str, Dict[str, Any]]) -> None:
    _write_json(_associations_key(season), {"associations": associations})


def _trade_pair(trade: Dict[str, Any]) -> Tuple[str, ...]:
    return tuple(sorted(str(key) for key in trade.get("team_keys", []) if key))


def _find_closest_prior_trade(
    trades: List[Dict[str, Any]],
    team_a: str,
    team_b: str,
    event_time: float,
) -> Optional[Dict[str, Any]]:
    pair = tuple(sorted((team_a, team_b)))
    candidates = []
    for trade in trades:
        if _trade_pair(trade) != pair:
            continue
        trade_time = float(trade.get("timestamp") or 0)
        if not trade_time or trade_time > event_time:
            continue
        elapsed = event_time - trade_time
        if elapsed <= FAAB_MATCH_WINDOW_SECONDS:
            candidates.append((elapsed, trade))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _closest_commish_evidence(
    commish: List[Dict[str, Any]],
    detected_at: float,
    seen_before: Iterable[str],
) -> Optional[Dict[str, Any]]:
    seen = set(seen_before)
    candidates = []
    for item in commish:
        key = str(item.get("transaction_key") or "")
        if not key or key in seen:
            continue
        timestamp = float(item.get("timestamp") or 0)
        if not timestamp:
            continue
        distance = abs(timestamp - detected_at)
        if distance <= COMMISH_EVIDENCE_WINDOW_SECONDS:
            candidates.append((distance, item))
    if not candidates:
        return None
    candidates.sort(key=lambda item: item[0])
    return candidates[0][1]


def _backfill_direct_commish_associations(
    trades: List[Dict[str, Any]],
    commish: List[Dict[str, Any]],
    associations: Dict[str, Dict[str, Any]],
) -> None:
    """Use any explicit FAAB move Yahoo happens to include in a commish record.

    Yahoo documents commish transactions but does not promise a dedicated manual
    FAAB-transfer schema. This conservative path only backfills when a commish
    record itself contains an amount plus unambiguous source/destination teams.
    """
    for item in commish:
        source = str(item.get("source_team_key") or "")
        destination = str(item.get("destination_team_key") or "")
        amounts = [abs(float(value)) for value in item.get("faab_amount_candidates", []) if value]
        if not source or not destination or source == destination or not amounts:
            continue
        event_time = float(item.get("timestamp") or 0)
        trade = _find_closest_prior_trade(trades, source, destination, event_time)
        if trade is None:
            continue
        trade_key = str(trade.get("transaction_key") or "")
        associations.setdefault(
            trade_key,
            {
                "amount": max(amounts),
                "sender_team_key": source,
                "receiver_team_key": destination,
                "commish_transaction_keys": [item.get("transaction_key")],
                "detected_at": event_time,
                "match_method": "explicit_commish_payload",
            },
        )


def _update_faab_tracking(
    season: int,
    team_states: Dict[str, Dict[str, Any]],
    trades: List[Dict[str, Any]],
    commish: List[Dict[str, Any]],
    now_epoch: float,
) -> Dict[str, Dict[str, Any]]:
    associations = _load_associations(season)
    _backfill_direct_commish_associations(trades, commish, associations)

    previous = _read_json(_snapshot_key(season)) or {}
    previous_balances = previous.get("balances") if isinstance(previous.get("balances"), dict) else {}
    seen_commish = previous.get("seen_commish_keys") if isinstance(previous.get("seen_commish_keys"), list) else []
    pending = previous.get("pending_changes") if isinstance(previous.get("pending_changes"), list) else []

    current_balances = {
        team_key: float(team["faab_balance"])
        for team_key, team in team_states.items()
        if team.get("faab_balance") is not None
    }

    if previous_balances:
        for team_key, new_balance in current_balances.items():
            if team_key not in previous_balances:
                continue
            old_balance = _as_float(previous_balances.get(team_key))
            if old_balance is None:
                continue
            delta = float(new_balance) - float(old_balance)
            if abs(delta) <= EPSILON:
                continue
            evidence = _closest_commish_evidence(commish, now_epoch, seen_commish)
            pending.append(
                {
                    "team_key": team_key,
                    "delta": delta,
                    "before": old_balance,
                    "after": float(new_balance),
                    "detected_at": now_epoch,
                    "commish_transaction_key": (
                        evidence.get("transaction_key") if evidence else None
                    ),
                    "commish_timestamp": (
                        float(evidence.get("timestamp") or 0) if evidence else 0
                    ),
                }
            )

    pending = [
        item
        for item in pending
        if now_epoch - float(item.get("detected_at") or 0) <= PENDING_CHANGE_TTL_SECONDS
    ]

    used_indexes = set()
    for left_index, left in enumerate(pending):
        if left_index in used_indexes:
            continue
        left_delta = _as_float(left.get("delta"))
        if left_delta is None or abs(left_delta) <= EPSILON:
            continue
        for right_index in range(left_index + 1, len(pending)):
            if right_index in used_indexes:
                continue
            right = pending[right_index]
            right_delta = _as_float(right.get("delta"))
            if right_delta is None or left_delta * right_delta >= 0:
                continue
            if abs(abs(left_delta) - abs(right_delta)) > EPSILON:
                continue
            left_team = str(left.get("team_key") or "")
            right_team = str(right.get("team_key") or "")
            if not left_team or not right_team or left_team == right_team:
                continue

            commish_keys = [
                str(value)
                for value in (
                    left.get("commish_transaction_key"),
                    right.get("commish_transaction_key"),
                )
                if value
            ]
            if not commish_keys:
                continue

            event_time = max(
                float(left.get("commish_timestamp") or left.get("detected_at") or 0),
                float(right.get("commish_timestamp") or right.get("detected_at") or 0),
            )
            trade = _find_closest_prior_trade(
                trades, left_team, right_team, event_time or now_epoch
            )
            if trade is None:
                continue

            sender = left_team if left_delta < 0 else right_team
            receiver = right_team if left_delta < 0 else left_team
            amount = abs(left_delta)
            trade_key = str(trade.get("transaction_key") or "")
            existing = associations.get(trade_key)
            if not existing or float(existing.get("detected_at") or 0) <= event_time:
                associations[trade_key] = {
                    "amount": amount,
                    "sender_team_key": sender,
                    "receiver_team_key": receiver,
                    "commish_transaction_keys": list(dict.fromkeys(commish_keys)),
                    "detected_at": event_time or now_epoch,
                    "match_method": "equal_opposite_balance_delta",
                }
            used_indexes.update({left_index, right_index})
            break

    pending = [item for index, item in enumerate(pending) if index not in used_indexes]
    current_commish_keys = [
        str(item.get("transaction_key") or "")
        for item in commish
        if item.get("transaction_key")
    ]
    snapshot = {
        "balances": current_balances,
        "seen_commish_keys": current_commish_keys,
        "pending_changes": pending,
        "refreshed_at": now_epoch,
    }
    _write_json(_snapshot_key(season), snapshot)
    _save_associations(season, associations)
    return associations


def _format_timestamp(epoch: Any) -> str:
    parsed = _as_float(epoch)
    if not parsed:
        return "Yahoo transaction"
    return datetime.fromtimestamp(parsed, tz=CENTRAL_TZ).strftime("%b %-d, %-I:%M %p")


def _decorate_trades(
    trades: List[Dict[str, Any]],
    team_states: Dict[str, Dict[str, Any]],
    associations: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    decorated: List[Dict[str, Any]] = []
    for trade in sorted(trades, key=lambda item: int(item.get("timestamp") or 0), reverse=True):
        item = dict(trade)
        association = associations.get(str(trade.get("transaction_key") or ""))
        sides = []
        for raw_side in trade.get("sides", []):
            side = dict(raw_side)
            team_key = str(side.get("team_key") or "")
            team_state = team_states.get(team_key, {})
            side["team_name"] = str(team_state.get("name") or side.get("team_name") or "Yahoo Team")
            side["logo_url"] = str(team_state.get("logo_url") or "")
            side["faab_sent"] = 0.0
            if association and team_key == str(association.get("sender_team_key") or ""):
                side["faab_sent"] = float(association.get("amount") or 0)
            sides.append(side)

        item["sides"] = sides
        item["time_label"] = _format_timestamp(item.get("timestamp"))
        item["faab_association"] = association
        if association:
            elapsed = max(
                0,
                int(
                    round(
                        (float(association.get("detected_at") or 0) - float(item.get("timestamp") or 0))
                        / 60
                    )
                ),
            )
            item["faab_minutes_after"] = elapsed
        decorated.append(item)
    return decorated


def refresh_trade_tracker_once(
    request: Optional[Request] = None,
    *,
    season: Optional[int] = None,
    league_key: Optional[str] = None,
) -> Dict[str, Any]:
    request = request or _server_request()
    season = int(season or datetime.now(timezone.utc).year)
    league_key = str(league_key or os.getenv("YAHOO_LEAGUE_KEY", "")).strip()
    if not league_key:
        raise RuntimeError("YAHOO_LEAGUE_KEY is not configured.")

    team_states, trades, commish = _fetch_trade_inputs(request, league_key)
    now_epoch = time.time()
    associations = _update_faab_tracking(
        season, team_states, trades, commish, now_epoch
    )
    decorated = _decorate_trades(trades, team_states, associations)
    state = {
        "season": season,
        "league_key": league_key,
        "refreshed_at": now_epoch,
        "trades": decorated,
        "trade_count": len(decorated),
        "commish_count": len(commish),
        "association_count": len(associations),
    }
    _write_json(_state_key(season), state)
    _write_json(CURRENT_STATUS_KEY, state)
    return state


def load_trade_section(
    request: Request,
    season: int,
    league_key: str,
) -> Dict[str, Any]:
    cached = _read_json(_state_key(season)) or {}
    refreshed_at = float(cached.get("refreshed_at") or 0)
    current_year = datetime.now(timezone.utc).year
    max_age = CURRENT_SEASON_REFRESH_SECONDS if int(season) == current_year else HISTORICAL_CACHE_SECONDS

    if (
        not cached
        or str(cached.get("league_key") or "") != str(league_key)
        or time.time() - refreshed_at > max_age
    ):
        try:
            return refresh_trade_tracker_once(
                request=request,
                season=season,
                league_key=league_key,
            )
        except Exception as exc:
            print(f"WARNING: Yahoo trade tracker refresh failed: {exc}")
            if cached:
                cached["stale"] = True
                cached["error"] = str(exc)[:240]
                return cached
            return {
                "season": season,
                "league_key": league_key,
                "refreshed_at": 0,
                "trades": [],
                "trade_count": 0,
                "association_count": 0,
                "stale": True,
                "error": str(exc)[:240],
            }
    return cached


async def _tracker_loop() -> None:
    await asyncio.sleep(8)
    while True:
        try:
            if _upstash_config() is None:
                await asyncio.sleep(300)
                continue
            lock = _upstash_command(
                ["SET", TRACKER_LOCK_KEY, str(time.time()), "NX", "EX", 50]
            )
            if lock == "OK":
                await asyncio.to_thread(refresh_trade_tracker_once)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(f"WARNING: trade tracker collector failed: {exc}")
        await asyncio.sleep(CURRENT_SEASON_REFRESH_SECONDS)


def start_trade_tracker() -> None:
    global _tracker_task
    if _tracker_task is None or _tracker_task.done():
        _tracker_task = asyncio.create_task(_tracker_loop())


async def stop_trade_tracker() -> None:
    global _tracker_task
    if _tracker_task is None:
        return
    _tracker_task.cancel()
    try:
        await _tracker_task
    except asyncio.CancelledError:
        pass
    _tracker_task = None
