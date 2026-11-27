from app.player_rankings import rank_rows, fetch_week
import pytest


def player(key, points):
    return {'key': key, 'name': key, 'position': 'RB', 'team': 'X', 'points': points}


def test_ties_zero_negative_missing_and_minimum():
    weeks = {str(w): {'players': [player('a', 40), player('b', 20), player('c', 20), player('d', 0), player('e', -2)]} for w in range(1, 6)}
    weeks['1']['players'].append(player('one-game', 50))
    rows = {r['key']: r for r in rank_rows(weeks, 'RB')}
    assert rows['one-game']['weeks']['1']['rank'] == 1
    assert rows['one-game']['games'] == 1
    assert rows['one-game']['average'] is None
    assert rows['a']['average'] == 1.2
    assert rows['b']['weeks']['2']['rank'] == rows['c']['weeks']['2']['rank'] == 2
    assert rows['d']['weeks']['2']['rank'] == 4
    assert rows['e']['weeks']['2']['rank'] == 5
    assert rows['d']['games'] == 5  # Zero points is a played game.
    assert '2' not in rows['one-game']['weeks']  # Missing weeks don't dilute average.
    assert rank_rows(weeks, 'RB')[-1]['key'] == 'one-game'


def resource(key, gp, points=None, week=1):
    base = [{'player_key': key}, {'name': {'full': key}}, {'display_position': 'RB'}]
    stats = {'player_stats': {'coverage_type': 'week', 'week': week, 'stats': [{'stat': {'stat_id': '0', 'value': gp}}]}}
    if points is not None:
        stats['player_points'] = {'coverage_type': 'week', 'week': week, 'total': points}
    return {'player': [base, stats]}


def test_games_played_excludes_dnp_includes_zero(monkeypatch):
    from app import player_rankings as r
    def get(path):
        if path.startswith('game/'):
            return {'players': [resource('a', 1), resource('b', 0), resource('c', 1)]}
        assert 'player_keys=a,c/' in path
        return {'fantasy_content': {'league': [{'league_key': r.SOURCE_LEAGUE}, {'players': [resource('a', 1, 0), resource('c', 1, -2)]}]}}
    monkeypatch.setattr(r, 'public_get', get)
    result = fetch_week('RB', 1)
    assert [(p['key'], p['points']) for p in result['players']] == [('a', 0), ('c', -2)]


def test_missing_points_rejects_partial_week(monkeypatch):
    from app import player_rankings as r
    def get(path):
        if path.startswith('game/'):
            return {'players': [resource('a', 1), resource('b', 1)]}
        return {'fantasy_content': {'league': [{'league_key': r.SOURCE_LEAGUE}, {'players': [resource('a', 1, 10)]}]}}
    monkeypatch.setattr(r, 'public_get', get)
    with pytest.raises(ValueError, match='Incomplete weekly points'):
        fetch_week('RB', 1)


def test_week_and_position_isolation():
    assert rank_rows({}, 'QB') == []
    assert rank_rows({'1': {'players': [player('qb', 2)]}}, 'QB')[0]['weeks']['1']['rank'] == 1


def test_transient_bad_request_retried_once(monkeypatch):
    from app import player_rankings as r
    calls = []
    def get(path):
        calls.append(path)
        if len(calls) == 1:
            raise RuntimeError('Yahoo public projection service returned HTTP 400.')
        return {'ok': True}
    monkeypatch.setattr(r, 'public_get', get)
    monkeypatch.setattr(r.time, 'sleep', lambda _: None)
    assert r.ranking_get('same/path') == {'ok': True}
    assert calls == ['same/path', 'same/path']
