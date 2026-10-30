"""Cached, read-only Week 13 forecasts using current rosters and Yahoo projections."""
from copy import deepcopy
import asyncio
import hashlib
import json
import math
import os
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from app.roster_snapshot import _read_snapshot, _server_request, _projected_points, ROSTER_SNAPSHOT_PREFIX
from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import _walk_values_for_key, _scalar_map, _first_value_for_key, _extract_matchups, _league_metadata
from app.yahoo_shared_auth import _upstash_command, _upstash_config
from app.scoring import calculate_weekly_mamba_points

SOURCE_LEAGUE = os.getenv('YAHOO_PROJECTION_LEAGUE_KEY', '470.l.1715444')
END_WEEK = 13
REFRESH_SECONDS = 3600
CACHE_KEY = ROSTER_SNAPSHOT_PREFIX + ':ros:v1'
FINAL_STATUSES = {'postevent', 'final', 'complete', 'completed'}
router = APIRouter(tags=['rest-of-season'])
_memory = None
_task = None
_lock = threading.Lock()
_last_error = None


def public_get(path):
    """Publicly viewable scoring-copy league; never send OAuth credentials."""
    url = 'https://pub-api-ro.fantasysports.yahoo.com/fantasy/v2/' + path + '?format=json'
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=25) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                raise RuntimeError(f'Yahoo public projection service returned HTTP {exc.code}.') from None
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError('Yahoo public projection service timed out.') from None
        time.sleep(2 ** attempt)


def scoring_signature(payload):
    node = _first_value_for_key(payload, 'stat_modifiers') or {}
    # An absent zero-valued modifier is equivalent to an explicit zero.
    return {str(s['stat_id']): float(s['value']) for s in _walk_values_for_key(node, 'stat')
            if float(s.get('value') or 0) != 0}


def starting_slots(payload):
    node = _first_value_for_key(payload, 'roster_positions') or []
    slots = []
    for item in _walk_values_for_key(node, 'roster_position'):
        position = str(item.get('position') or '')
        if position not in {'BN', 'IR', 'IR+', 'IL', 'IL+', 'NA'}:
            slots.extend([position] * int(item.get('count') or 0))
    if not slots or len(slots) > 12:
        raise RuntimeError('Unsupported or missing starting roster slots.')
    return slots


def eligible(player, slot):
    positions = set(player.get('eligible_positions') or [player.get('position')])
    if slot in positions:
        return True
    flex = {'W/R/T': {'WR', 'RB', 'TE'}, 'W/R': {'WR', 'RB'},
            'W/T': {'WR', 'TE'}, 'Q/W/R/T': {'QB', 'WR', 'RB', 'TE'}}
    return bool(positions & flex.get(slot, set()))


def optimal_lineup(players, slots, week):
    """Exact assignment DP: no duplicate player, including multi-position FLEX."""
    states = {0: (Decimal('0'), [])}
    missing = []
    for player in sorted(players, key=lambda p: p['player_key']):
        points = player.get('projected_points')
        if points is None:
            missing.append(player['name'])
            continue
        if player.get('bye_week') == week:
            continue
        for mask, (score, chosen) in list(states.items()):
            for index, slot in enumerate(slots):
                if mask & (1 << index) or not eligible(player, slot):
                    continue
                new_mask = mask | (1 << index)
                new_score = score + Decimal(str(points))
                if new_mask not in states or new_score > states[new_mask][0]:
                    states[new_mask] = (new_score, chosen + [(index, player)])
    # Fill as many legal starting slots as possible, even for negative DEF scores.
    mask = max(states, key=lambda m: (bin(m).count('1'), states[m][0]))
    total, chosen = states[mask]
    assignments = {index: player for index, player in chosen}
    return {'total': float(total.quantize(Decimal('0.01'))),
            'starters': [{'slot': slot, 'player': assignments.get(i)} for i, slot in enumerate(slots)],
            'empty_slots': [slot for i, slot in enumerate(slots) if i not in assignments],
            'missing_projections': missing}


def _projection_batch(args):
    keys, week = args
    payload = public_get(f'league/{SOURCE_LEAGUE}/players;player_keys={",".join(keys)}/stats;type=week;week={week};show_projected_stats=1')
    if _league_metadata(payload).get('league_key') != SOURCE_LEAGUE:
        raise RuntimeError('Projection response has the wrong league scoring context.')
    found = {}
    for player in _walk_values_for_key(payload, 'player'):
        fields = _scalar_map(player)
        key = str(fields.get('player_key') or '')
        if key not in keys:
            continue
        positions = list(_walk_values_for_key(_first_value_for_key(player, 'eligible_positions'), 'position'))
        bye = _scalar_map(_first_value_for_key(player, 'bye_weeks')).get('week')
        found[key] = {'projected_points': _projected_points({'player': player}, key, week),
                      'eligible_positions': positions,
                      'bye_week': int(bye) if str(bye).isdigit() else None}
    if set(found) != set(keys):
        raise RuntimeError('Yahoo returned an incomplete player batch; retaining the previous forecast.')
    return week, found


def roster_fingerprint(roster):
    material = [(t['team_key'], t['team_name'], sorted(p['player_key'] for p in t['players']))
                for t in roster['teams']]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def build_forecast(roster, settings, scoreboards, projections):
    teams = roster['teams']
    names = {t['team_key']: t['team_name'] for t in teams}
    if len(set(names.values())) != len(names):
        raise RuntimeError('Team display names must be unique for standings.')
    slots = starting_slots(settings)
    weeks, weekly_mamba, lineups, projected_weeks = {}, {}, {}, []
    yahoo_teams = {name: {'weekly_results': {}} for name in names.values()}
    warnings = []
    for week in range(1, END_WEEK + 1):
        matchups = scoreboards[week]
        seen = [t['team_key'] for m in matchups for t in m['teams']]
        if len(seen) != len(names) or set(seen) != set(names):
            raise RuntimeError(f'Incomplete or duplicate Week {week} matchup schedule.')
        final = all(str(m.get('status')).lower() in FINAL_STATUSES for m in matchups)
        scores = {}
        if final:
            for matchup in matchups:
                for team in matchup['teams']:
                    score = team.get('score')
                    if score is None or not math.isfinite(float(score)):
                        raise RuntimeError(f'Missing final Week {week} score.')
                    scores[names[team['team_key']]] = float(score)
        else:
            projected_weeks.append(week)
            lineups[str(week)] = []
            for team in teams:
                players = [{**p, **projections[week][p['player_key']]} for p in team['players']]
                lineup = optimal_lineup(players, slots, week)
                scores[team['team_name']] = lineup['total']
                lineups[str(week)].append({'team_key': team['team_key'], 'team_name': team['team_name'], **lineup})
                if lineup['missing_projections']:
                    warnings.append(f"Week {week}, {team['team_name']}: no projection for {', '.join(lineup['missing_projections'])}; excluded from optimization.")
                if lineup['empty_slots']:
                    warnings.append(f"Week {week}, {team['team_name']}: unfilled {', '.join(lineup['empty_slots'])}; no eligible rostered replacement.")
        for matchup in matchups:
            a, b = matchup['teams']
            for team, opponent in ((a, b), (b, a)):
                name, other = names[team['team_key']], names[opponent['team_key']]
                winner = matchup.get('winner_team_key') if final else None
                result = ('W' if team['team_key'] == winner else 'L') if winner else (
                    'W' if scores[name] > scores[other] else 'L' if scores[name] < scores[other] else 'T')
                yahoo_teams[name]['weekly_results'][str(week)] = result
        weeks[str(week)] = scores
        weekly_mamba[str(week)] = calculate_weekly_mamba_points(scores)
    return {'week_numbers': list(weeks), 'weeks': weeks, 'weekly_mamba_points': weekly_mamba,
            'yahoo_teams': yahoo_teams, 'lineups': lineups, 'projected_weeks': projected_weeks,
            'warnings': warnings, 'slots': slots}


def apply_live_week(forecast, data):
    """Overlay the same selected-lineup totals used by Current Week Projected.

    Future optimized weeks remain cached; current games never wait for that
    expensive refresh and are never retrospectively re-optimized.
    """
    if not forecast or forecast.get('league_key') != data.get('league_key') or int(forecast.get('season', 0)) != int(data.get('season', 0)):
        return None
    from app.live_dashboard import _build_projected_dashboard_state
    live = _build_projected_dashboard_state(data)
    if not live:
        return None
    result = deepcopy(forecast)
    current = int(data['current_week'])
    for week in live['week_numbers']:
        if int(week) > min(current, END_WEEK):
            continue
        key = str(week)
        result['weeks'][key] = live['weeks'][key]
        result['weekly_mamba_points'][key] = live['weekly_mamba_points'][key]
        for name, team in live['yahoo_teams'].items():
            outcome = team.get('weekly_results', {}).get(key)
            if outcome is not None:
                result['yahoo_teams'][name]['weekly_results'][key] = outcome
        result['lineups'].pop(key, None)
    result['projected_weeks'] = [w for w in result['projected_weeks'] if int(w) >= current]
    result['warnings'] = [warning for warning in result.get('warnings', [])
                          if not any(warning.startswith(f'Week {w},') for w in range(1, current + 1))]
    result['current_week'] = current
    result['current_week_scores'] = data.get('projected_scores', {})
    result['current_week_refresh'] = data.get('_refresh_meta', {})
    result['stale'] = result.get('stale', False) or bool(result['current_week_refresh'].get('stale'))
    return result


def read_forecast():
    global _memory
    if _memory is None and _upstash_config():
        try:
            raw = _upstash_command(['GET', CACHE_KEY])
            if raw:
                _memory = json.loads(raw)
        except Exception:
            pass
    if not _memory:
        return None
    return {**_memory, 'stale': time.time() - _memory['updated_at_epoch'] > REFRESH_SECONDS * 2,
            'refresh_error': _last_error}


def refresh_forecast():
    global _memory, _last_error
    if not _lock.acquire(blocking=False):
        return
    try:
        roster = _read_snapshot()
        if not roster:
            raise RuntimeError('Waiting for the current league roster snapshot.')
        if int(roster['league']['season']) != datetime.now(timezone.utc).year:
            raise RuntimeError('The roster snapshot is from a different season.')
        if time.time() - float(roster.get('updated_at_epoch', 0)) > 3600:
            raise RuntimeError('Waiting for a fresh roster snapshot.')
        existing = read_forecast()
        fingerprint = roster_fingerprint(roster)
        if existing and existing.get('roster_fingerprint') == fingerprint and time.time() - existing['updated_at_epoch'] < REFRESH_SECONDS:
            return
        request = _server_request()
        league_key = roster['league']['league_key']
        settings = _fantasy_get(request, f'league/{league_key}/settings')
        source_settings = public_get(f'league/{SOURCE_LEAGUE}/settings')
        if not scoring_signature(settings) or scoring_signature(settings) != scoring_signature(source_settings):
            raise RuntimeError('Projection league scoring differs from the Mamba league. Forecast not updated.')
        if int(_league_metadata(source_settings).get('season') or 0) != int(roster['league']['season']):
            raise RuntimeError('Projection league is from a different season.')
        scoreboards = {}
        for week in range(1, END_WEEK + 1):
            scoreboards[week] = _extract_matchups(_fantasy_get(request, f'league/{league_key}/scoreboard;week={week}'))
        future = [w for w, matches in scoreboards.items() if not matches or not all(str(m.get('status')).lower() in FINAL_STATUSES for m in matches)]
        keys = sorted({p['player_key'] for t in roster['teams'] for p in t['players']})
        jobs = [(keys[i:i+25], w) for w in future for i in range(0, len(keys), 25)]
        projections = {w: {} for w in future}
        with ThreadPoolExecutor(max_workers=2) as pool:
            for week, batch in pool.map(_projection_batch, jobs):
                projections[week].update(batch)
        state = build_forecast(roster, settings, scoreboards, projections)
        now = time.time()
        result = {**state, 'success': True, 'season': roster['league']['season'], 'league_key': league_key,
                  'projection_league_key': SOURCE_LEAGUE, 'end_week': END_WEEK,
                  'updated_at': datetime.fromtimestamp(now, timezone.utc).isoformat(), 'updated_at_epoch': now,
                  'roster_updated_at': roster['updated_at'], 'roster_fingerprint': fingerprint,
                  'refresh_seconds': REFRESH_SECONDS, 'player_projections': projections,
                  'scoring_modifiers': scoring_signature(source_settings), 'rosters': roster['teams']}
        if _upstash_config():
            _upstash_command(['SET', CACHE_KEY, json.dumps(result, separators=(',', ':'))])
        _memory, _last_error = result, None
    except Exception as exc:
        # No upstream payloads or credentials in the public status response.
        _last_error = str(exc) if isinstance(exc, RuntimeError) else 'Yahoo forecast refresh failed; the last successful snapshot is retained.'
        print(f'WARNING: ROS refresh: {_last_error}')
    finally:
        _lock.release()


async def _loop():
    while True:
        await asyncio.to_thread(refresh_forecast)
        await asyncio.sleep(60)


def start_ros_collector():
    global _task
    if _task is None or _task.done():
        _task = asyncio.create_task(_loop())


async def stop_ros_collector():
    global _task
    if _task:
        _task.cancel()
        try:
            await _task
        except asyncio.CancelledError:
            pass
        _task = None


@router.get('/api/ros-projections')
def ros_snapshot():
    from app.yahoo_live_cache import load_cached_yahoo_dashboard_data
    forecast = read_forecast()
    if forecast:
        data = load_cached_yahoo_dashboard_data(_server_request(), int(forecast['season']), None)
        forecast = apply_live_week(forecast, data)
    if not forecast:
        return JSONResponse({'success': False, 'status': 'pending',
                             'message': _last_error or 'Preparing weekly projections and optimized lineups.'}, status_code=503,
                            headers={'Cache-Control': 'no-store'})
    return JSONResponse(forecast, headers={'Cache-Control': 'no-store'})
