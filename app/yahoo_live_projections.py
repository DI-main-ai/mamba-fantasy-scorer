"""Live current-week totals from Yahoo's player projections in matching scoring."""
import json
import urllib.request
from datetime import datetime, timezone
import math
import time
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import _walk_values_for_key, _first_value_for_key, _scalar_map
from app.roster_snapshot import _extract_roster_players

_validated = {}
RESERVES = {'BN', 'IR', 'IR+', 'IL', 'IL+', 'NA'}


def completed_teams(week):
    year = datetime.now(timezone.utc).year
    url = f'https://site.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard?dates={year}&seasontype=2&week={week}'
    with urllib.request.urlopen(url, timeout=20) as response:
        payload = json.load(response)
    events = payload.get('events', [])
    if not events:
        raise RuntimeError('NFL game status unavailable; retaining the previous dashboard.')
    return {c['team']['abbreviation'].upper() for e in events if e.get('status', {}).get('type', {}).get('completed')
            for competition in e.get('competitions', []) for c in competition.get('competitors', [])}


def player_projection(resource, week, finished=None):
    # Live is already the projected final total, not remaining points.
    team = str(_scalar_map(resource).get('editorial_team_abbr') or '').upper()
    keys = ('player_points',) if team in (finished or set()) else ('player_live_projected_points', 'player_projected_points')
    for key in keys:
        for node in _walk_values_for_key(resource, key):
            fields = _scalar_map(node)
            if fields.get('coverage_type') != 'week' or str(fields.get('week')) != str(week):
                continue
            try:
                value = float(fields['total'])
            except (KeyError, ValueError, TypeError):
                continue
            if math.isfinite(value):
                return value, key in {'player_live_projected_points', 'player_points'}
    raise RuntimeError('Yahoo omitted a starter projection; retaining the previous dashboard.')


def live_team_totals(request, league_key, week, expected_keys):
    from app.ros_projection import SOURCE_LEAGUE, public_get, scoring_signature
    now = time.time()
    if now - _validated.get(league_key, 0) > 3600:
        actual = scoring_signature(_fantasy_get(request, f'league/{league_key}/settings'))
        source = scoring_signature(public_get(f'league/{SOURCE_LEAGUE}/settings'))
        if not actual or actual != source:
            raise RuntimeError('Public projection scoring does not match the league.')
        _validated[league_key] = now
    payload = _fantasy_get(request, f'league/{league_key}/teams/roster;week={week}')
    starters = {}
    for resource in _walk_values_for_key(payload, 'team'):
        key = str(_scalar_map(resource).get('team_key') or '')
        if key not in expected_keys:
            continue
        roster = _first_value_for_key(resource, 'roster')
        if roster is None or str(_scalar_map(roster).get('week')) != str(week):
            raise RuntimeError('Yahoo omitted the current weekly roster.')
        players = _extract_roster_players({'roster': roster})
        if not players or any(not p['roster_slot'] for p in players):
            raise RuntimeError('Yahoo omitted lineup positions.')
        starters[key] = [p for p in players if p['roster_slot'] not in RESERVES]
    if set(starters) != set(expected_keys):
        raise RuntimeError('Yahoo omitted a team lineup.')
    finished = completed_teams(week)
    keys = sorted({p['player_key'] for players in starters.values() for p in players})

    def batch(batch_keys):
        response = public_get(f'leagues;league_keys={SOURCE_LEAGUE}/players;player_keys={",".join(batch_keys)}/stats_collection;types=week;week={week};show_projected_stats=1;show_live_projected_points=1')
        if not any(str(_scalar_map(league).get('league_key')) == SOURCE_LEAGUE for league in _walk_values_for_key(response, 'league')):
            raise RuntimeError('Wrong projection scoring context.')
        found = {}
        for resource in _walk_values_for_key(response, 'player'):
            key = str(_scalar_map(resource).get('player_key') or '')
            if key in batch_keys:
                found[key] = player_projection(resource, week, finished)
        if set(found) != set(batch_keys):
            raise RuntimeError('Yahoo omitted player projections.')
        return found

    values = {}
    with ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(batch, [keys[i:i+25] for i in range(0, len(keys), 25)]):
            values.update(result)
    return {key: {'total': float(sum((Decimal(str(values[p['player_key']][0])) for p in players), Decimal('0')).quantize(Decimal('0.01'))),
                  'starters': [{**p, 'projected_points': values[p['player_key']][0], 'live': values[p['player_key']][1]} for p in players]}
            for key, players in starters.items()}
