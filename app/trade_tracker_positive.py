"""League-specific FAAB trade matching.

In the Mamba league a team's FAAB balance only increases when FAAB is received
as part of a trade.  That makes a positive balance delta the authoritative
signal.  The base trade tracker still owns Yahoo fetching, caching, display
formatting, and persistence; this module replaces only its FAAB association
step and re-exports the public loader.
"""

from typing import Any, Dict, List, Optional, Tuple

from app import trade_tracker as _base


SENDER_CONFIRM_WINDOW_SECONDS = 10 * 60


def _closest_prior_unmatched_trade(
    trades: List[Dict[str, Any]],
    receiver_team_key: str,
    event_time: float,
    associations: Dict[str, Dict[str, Any]],
) -> Optional[Tuple[Dict[str, Any], str]]:
    candidates: List[Tuple[float, Dict[str, Any], str]] = []

    for trade in trades:
        trade_key = str(trade.get("transaction_key") or "")
        if not trade_key or trade_key in associations:
            continue

        pair = _base._trade_pair(trade)
        if len(pair) != 2 or receiver_team_key not in pair:
            continue

        trade_time = float(trade.get("timestamp") or 0)
        if not trade_time or trade_time > event_time:
            continue

        elapsed = event_time - trade_time
        if elapsed > _base.FAAB_MATCH_WINDOW_SECONDS:
            continue

        sender_team_key = pair[0] if pair[1] == receiver_team_key else pair[1]
        candidates.append((elapsed, trade, sender_team_key))

    if not candidates:
        return None

    candidates.sort(key=lambda item: item[0])
    _, trade, sender_team_key = candidates[0]
    return trade, sender_team_key


def _sender_negative_confirmation(
    pending: List[Dict[str, Any]],
    sender_team_key: str,
    amount: float,
    event_time: float,
) -> bool:
    """Use a sender decrease as supporting evidence, never as a requirement.

    The sender may also have spent ordinary waiver FAAB while Render was asleep,
    so a negative delta larger than the received amount still confirms the trade.
    """
    for item in pending:
        if str(item.get("team_key") or "") != sender_team_key:
            continue
        delta = _base._as_float(item.get("delta"))
        if delta is None or delta >= -_base.EPSILON:
            continue
        detected_at = float(item.get("detected_at") or 0)
        if abs(detected_at - event_time) > SENDER_CONFIRM_WINDOW_SECONDS:
            continue
        if abs(delta) + _base.EPSILON >= amount:
            return True
    return False


def _update_faab_tracking_positive_gain(
    season: int,
    team_states: Dict[str, Dict[str, Any]],
    trades: List[Dict[str, Any]],
    commish: List[Dict[str, Any]],
    now_epoch: float,
) -> Dict[str, Dict[str, Any]]:
    associations = _base._load_associations(season)
    _base._backfill_direct_commish_associations(trades, commish, associations)

    previous = _base._read_json(_base._snapshot_key(season)) or {}
    previous_balances = (
        previous.get("balances")
        if isinstance(previous.get("balances"), dict)
        else {}
    )
    pending = (
        previous.get("pending_changes")
        if isinstance(previous.get("pending_changes"), list)
        else []
    )

    current_balances = {
        team_key: float(team["faab_balance"])
        for team_key, team in team_states.items()
        if team.get("faab_balance") is not None
    }

    if previous_balances:
        for team_key, new_balance in current_balances.items():
            if team_key not in previous_balances:
                continue
            old_balance = _base._as_float(previous_balances.get(team_key))
            if old_balance is None:
                continue
            delta = float(new_balance) - float(old_balance)
            if abs(delta) <= _base.EPSILON:
                continue
            pending.append(
                {
                    "team_key": team_key,
                    "delta": delta,
                    "before": old_balance,
                    "after": float(new_balance),
                    "detected_at": now_epoch,
                }
            )

    pending = [
        item
        for item in pending
        if now_epoch - float(item.get("detected_at") or 0)
        <= _base.PENDING_CHANGE_TTL_SECONDS
    ]

    used_positive_indexes = set()

    for index, item in enumerate(pending):
        delta = _base._as_float(item.get("delta"))
        if delta is None or delta <= _base.EPSILON:
            continue

        receiver_team_key = str(item.get("team_key") or "")
        if not receiver_team_key:
            continue

        event_time = float(item.get("detected_at") or now_epoch)
        match = _closest_prior_unmatched_trade(
            trades,
            receiver_team_key,
            event_time,
            associations,
        )
        if match is None:
            continue

        trade, sender_team_key = match
        amount = float(delta)
        confirmed_by_sender = _sender_negative_confirmation(
            pending,
            sender_team_key,
            amount,
            event_time,
        )

        trade_key = str(trade.get("transaction_key") or "")
        associations[trade_key] = {
            "amount": amount,
            "sender_team_key": sender_team_key,
            "receiver_team_key": receiver_team_key,
            "commish_transaction_keys": [],
            "detected_at": event_time,
            "match_method": (
                "positive_balance_delta_sender_confirmed"
                if confirmed_by_sender
                else "positive_balance_delta"
            ),
        }
        used_positive_indexes.add(index)

    # Only an unmatched positive change can become a future trade association.
    # Negative changes are ordinary waiver spending or supporting evidence and
    # should not linger and accidentally influence a later trade.
    remaining_pending = [
        item
        for index, item in enumerate(pending)
        if index not in used_positive_indexes
        and (_base._as_float(item.get("delta")) or 0) > _base.EPSILON
    ]

    snapshot = {
        "balances": current_balances,
        "pending_changes": remaining_pending,
        "refreshed_at": now_epoch,
    }
    _base._write_json(_base._snapshot_key(season), snapshot)
    _base._save_associations(season, associations)
    return associations


# Patch the base module so its existing background collector and refresh paths
# automatically use the league-specific positive-gain rule.
_base._update_faab_tracking = _update_faab_tracking_positive_gain

load_trade_section = _base.load_trade_section
