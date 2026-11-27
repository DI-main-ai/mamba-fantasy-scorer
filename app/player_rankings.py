"""Weekly positional ranks from Yahoo actual league points and games played."""
import json
import math
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from fractions import Fraction
from fastapi import APIRouter, Request, Query
from app.routes import templates
from app.ros_projection import SOURCE_LEAGUE, public_get, scoring_signature
from app.yahoo_mamba import _walk_values_for_key, _first_value_for_key, _scalar_map, _league_metadata
from app.yahoo_matchup_detail import _player_name
from app.trade_tracker import _read_json, _write_json

router = APIRouter()
POSITIONS = ('QB', 'RB', 'WR', 'TE', 'DEF')
PREFIX = 'mamba:player-rankings:test:v1:' + SOURCE_LEAGUE
SEED = Path('data/player_rankings_seed.json')
_lock = threading.Lock()
_state = None
_running = False
_attempt = 0
_error = None


def ranking_get(path):
    # Yahoo occasionally rejects a valid batch with 400, then accepts the
    # identical request. Retry once; never publish a partial position/week.
    try:
        return public_get(path)
    except RuntimeError as exc:
        if 'HTTP 400' not in str(exc):
            raise
        time.sleep(1)
        return public_get(path)


def fetch_catalog(position):
    """Season participants, including released/IR players who played earlier."""
    keys = []
    seen = set()
    for start in range(0, 3000, 25):
        payload = ranking_get(f'game/{SOURCE_LEAGUE.split(".")[0]}/players;position={position};start={start};count=25/stats;type=season')
        players = list(_walk_values_for_key(payload, 'player'))
        for player in players:
            key = str(_scalar_map(player).get('player_key') or '')
            if not key or key in seen:
                raise ValueError('Repeated player catalog page')
            seen.add(key)
            stats = {str(s['stat_id']): s['value'] for s in _walk_values_for_key(_first_value_for_key(player, 'player_stats'), 'stat')}
            if '0' not in stats:
                raise ValueError('Season games played missing')
            if float(stats['0'] or 0) > 0:
                keys.append(key)
        if len(players) < 25:
            return {'updated': time.time(), 'keys': sorted(keys)}
    raise ValueError('Catalog pagination exceeded limit')


def fetch_week(position, week, catalog=None):
    """Enumerate the full Yahoo position pool; no top-N cutoff or roster filter."""
    active, seen = {}, set()
    pages = range(0, len(catalog), 25) if catalog is not None else range(0, 3000, 25)
    for start in pages:
        selector = f'player_keys={",".join(catalog[start:start+25])}' if catalog is not None else f'position={position};start={start};count=25'
        payload = ranking_get(f'game/{SOURCE_LEAGUE.split(".")[0]}/players;{selector}/stats;type=week;week={week}')
        if catalog is not None and {str(_scalar_map(p).get('player_key')) for p in _walk_values_for_key(payload, 'player')} != set(catalog[start:start+25]):
            raise ValueError('Incomplete weekly participation batch')
        players = list(_walk_values_for_key(payload, 'player'))
        for player in players:
            fields = _scalar_map(player)
            key = str(fields.get('player_key') or '')
            if not key or key in seen:
                raise ValueError('Incomplete or repeated Yahoo player page')
            seen.add(key)
            stats_node = _first_value_for_key(player, 'player_stats')
            coverage = _scalar_map(stats_node)
            if int(coverage.get('week') or 0) != week:
                raise ValueError('Unexpected weekly stats coverage')
            stats = {str(s['stat_id']): s['value'] for s in _walk_values_for_key(stats_node, 'stat')}
            if '0' not in stats:
                raise ValueError('Yahoo games-played field missing')
            if float(stats['0'] or 0) > 0:
                active[key] = {'key': key, 'name': _player_name(player), 'team': fields.get('editorial_team_abbr') or '', 'position': position}
        if len(players) < 25 and catalog is None:
            break
    else:
        if catalog is None:
            raise ValueError('Yahoo player pagination exceeded limit')
    keys = sorted(active)
    for start in range(0, len(keys), 25):
        batch = keys[start:start+25]
        payload = ranking_get(f'league/{SOURCE_LEAGUE}/players;player_keys={",".join(batch)}/stats;type=week;week={week}')
        if _league_metadata(payload).get('league_key') != SOURCE_LEAGUE:
            raise ValueError('Incorrect scoring league')
        found = set()
        for player in _walk_values_for_key(payload, 'player'):
            key = str(_scalar_map(player).get('player_key') or '')
            points = _scalar_map(_first_value_for_key(player, 'player_points'))
            if key not in batch or int(points.get('week') or 0) != week:
                raise ValueError('Unexpected weekly point coverage')
            total = float(points['total'])
            if not math.isfinite(total):
                raise ValueError('Invalid points')
            active[key]['points'] = total
            found.add(key)
        if found != set(batch):
            raise ValueError('Incomplete weekly points')
    return {'updated': time.time(), 'players': list(active.values())}


def rank_rows(weeks, position):
    rows = {}
    for week, entry in sorted(weeks.items(), key=lambda x: int(x[0])):
        players = sorted(entry['players'], key=lambda p: (-p['points'], p['key']))
        previous, rank = None, 0
        for i, player in enumerate(players, 1):
            if player['points'] != previous:
                rank = i
            previous = player['points']
            row = rows.setdefault(player['key'], {**player, 'weeks': {}})
            row['weeks'][str(week)] = {'rank': rank, 'points': player['points']}
    for row in rows.values():
        ranks = [v['rank'] for v in row['weeks'].values()]
        row['games'] = len(ranks)
        row['average'] = float(Fraction(sum(ranks), len(ranks))) if len(ranks) >= 5 else None
    return sorted(rows.values(), key=lambda r: (r['average'] is None, r['average'] or 0, r['name'].casefold()))


def load_state():
    global _state
    if _state is None:
        saved = _read_json(PREFIX)
        if not saved and SEED.exists():
            saved = json.loads(SEED.read_text())
        _state = saved if saved and saved.get('source') == SOURCE_LEAGUE else {'source': SOURCE_LEAGUE, 'weeks': {p: {} for p in POSITIONS}}
    return _state


def refresh():
    global _running, _error
    try:
        payload = ranking_get(f'league/{SOURCE_LEAGUE}/settings')
        meta = _league_metadata(payload)
        season, current = int(meta['season']), min(18, int(meta['current_week']))
        signature = scoring_signature(payload)
        if not signature:
            raise ValueError('Scoring settings unavailable')
        with _lock:
            state = load_state()
            if state.get('signature') != signature:
                state['weeks'] = {p: {} for p in POSITIONS}
            state.update(season=season, current_week=current, signature=signature)
        def collect(position):
            catalog = state.setdefault('catalogs', {}).get(position)
            if not catalog or time.time()-catalog['updated'] >= 3600:
                catalog = fetch_catalog(position)
                with _lock:
                    state['catalogs'][position] = catalog
                    _write_json(PREFIX, state)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(collect, POSITIONS))
        def collect_week(job):
            position, week = job
            old = state['weeks'][position].get(str(week))
            ttl = 300 if week == current else (86400 if week >= current-1 else 604800)
            if old and time.time()-old['updated'] < ttl:
                return
            entry = fetch_week(position, week, state['catalogs'][position]['keys'])
            with _lock:
                state['weeks'][position][str(week)] = entry
                _write_json(PREFIX, state)
        with ThreadPoolExecutor(max_workers=8) as pool:
            jobs = [pool.submit(collect_week, (p, w)) for w in range(1, current+1) for p in POSITIONS]
            failures = []
            for job in as_completed(jobs):
                try:
                    job.result()
                except Exception as exc:
                    failures.append(exc)
            if failures:
                raise failures[0]
        _error = None
    except Exception:
        logging.getLogger(__name__).exception('Weekly player rankings refresh failed')
        _error = 'Yahoo refresh is temporarily unavailable. Previously loaded weeks remain visible.'
    finally:
        _running = False


def ensure_refresh():
    global _running, _attempt
    with _lock:
        load_state()
        if not _running and time.time()-_attempt >= 300:
            _running, _attempt = True, time.time()
            threading.Thread(target=refresh, daemon=True).start()


@router.get('/player-rankings')
def page(request: Request):
    ensure_refresh()
    state = load_state()
    return templates.TemplateResponse(request=request, name='player_rankings.html', context={'request': request, 'page_title': 'Player Rankings · Mamba Fantasy', 'season': state.get('season', ''), 'current_week': state.get('current_week', 1), 'rankings_header': True, 'show_bottom_matchups': False})


@router.get('/api/player-rankings')
def api(position: str = Query('RB', pattern='^(QB|RB|WR|TE|DEF)$')):
    ensure_refresh()
    with _lock:
        state = load_state()
        weeks = state['weeks'][position]
        return {'position': position, 'season': state.get('season'), 'current_week': state.get('current_week'), 'weeks': sorted(map(int, weeks)), 'rows': rank_rows(weeks, position), 'loading': _running, 'error': _error, 'updated': min((w['updated'] for w in weeks.values()), default=None), 'current_updated': (weeks.get(str(state.get('current_week'))) or {}).get('updated')}
