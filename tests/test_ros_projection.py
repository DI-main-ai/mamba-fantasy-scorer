import itertools
from copy import deepcopy
from datetime import datetime, timezone

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from app import ros_projection as ros
from app import live_dashboard as live
from app.routes import build_yahoo_snapshot
from app.scoring import build_hybrid_standings


def player(key, position, points, **extra):
    return {'player_key': key, 'name': key, 'position': position,
            'eligible_positions': position.split('/'), 'projected_points': points, **extra}


def test_flex_and_multi_position_global_optimum():
    players = [player('a', 'RB/WR', 20), player('b', 'RB', 19), player('c', 'WR', 18), player('d', 'TE', 17)]
    result = ros.optimal_lineup(players, ['RB', 'WR', 'W/R/T'], 9)
    assert result['total'] == 57
    assert len({s['player']['player_key'] for s in result['starters']}) == 3


def test_optimizer_matches_exhaustive_assignment():
    players = [player('a', 'RB/WR', 11.21), player('b', 'RB', 10), player('c', 'WR', 14), player('d', 'TE', 12)]
    slots = ['RB', 'WR', 'W/R/T']
    possible = [sum(p['projected_points'] for p in assignment) for assignment in itertools.permutations(players, 3)
                if all(ros.eligible(p, slot) for p, slot in zip(assignment, slots))]
    assert ros.optimal_lineup(players, slots, 9)['total'] == round(max(possible), 2)


def test_byes_missing_zero_negative_and_unfilled():
    players = [player('bye', 'WR', 100, bye_week=9), player('missing', 'WR', None), player('zero', 'WR', 0), player('def', 'DEF', -2)]
    result = ros.optimal_lineup(players, ['WR', 'DEF', 'QB'], 9)
    assert result['total'] == -2
    assert result['missing_projections'] == ['missing']
    assert result['empty_slots'] == ['QB']
    assert result['starters'][0]['player']['player_key'] == 'zero'


def fixtures():
    settings = {'roster_positions': [{'roster_position': {'position': 'QB', 'count': 1}}, {'roster_position': {'position': 'BN', 'count': 6}}]}
    roster = {'teams': [{'team_key': 't1', 'team_name': 'One', 'players': [player('p1', 'QB', 15)]}, {'team_key': 't2', 'team_name': 'Two', 'players': [player('p2', 'QB', 15)]}]}
    boards = {w: [{'status': 'postevent' if w < 8 else 'preevent', 'teams': [{'team_key': 't1', 'score': 20}, {'team_key': 't2', 'score': 10}]}] for w in range(1, 14)}
    projections = {w: {'p1': {'projected_points': 15}, 'p2': {'projected_points': 15}} for w in range(8, 14)}
    return roster, settings, boards, projections


def test_final_weeks_preserved_future_ties_and_totals():
    state = ros.build_forecast(*fixtures())
    assert state['weeks']['7'] == {'One': 20, 'Two': 10}
    assert state['weeks']['8'] == {'One': 15, 'Two': 15}
    assert state['weekly_mamba_points']['8'] == {'One': 1.5, 'Two': 1.5}
    assert state['projected_weeks'] == [8, 9, 10, 11, 12, 13]
    rows, ranks = build_yahoo_snapshot(state['yahoo_teams'], state['weeks'], state['week_numbers'])
    standings = build_hybrid_standings(state['weeks'], ranks)
    assert rows[0]['total_wins'] == 10  # 7 wins + six half-win ties
    assert standings[0].points_for == 230
    assert standings[0].mamba_points == 23


def test_incomplete_schedule_rejected():
    args = fixtures(); args[2][10] = []
    with pytest.raises(RuntimeError, match='schedule'):
        ros.build_forecast(*args)


def test_projected_points_do_not_read_actual_scores():
    from app.roster_snapshot import _projected_points
    data = {'player': [[{'player_key': 'p1'}], {'player_points': {'coverage_type': 'week', 'week': '9', 'total': '99'}}]}
    assert _projected_points(data, 'p1', 9) is None


def test_failed_refresh_keeps_last_snapshot(monkeypatch):
    monkeypatch.setattr(ros, '_memory', {'updated_at_epoch': 1, 'weeks': {'1': {'One': 42}}})
    monkeypatch.setattr(ros, '_read_snapshot', lambda: None)
    monkeypatch.setattr(ros, '_last_error', None)
    ros.refresh_forecast()
    assert ros.read_forecast()['weeks']['1']['One'] == 42
    assert ros.read_forecast()['refresh_error']


def test_scoring_normalization():
    assert ros.scoring_signature({'stat_modifiers': {'stat': {'stat_id': 11, 'value': '0'}}}) == {}
    assert ros.scoring_signature({'stat_modifiers': {'stat': {'stat_id': 11, 'value': '0.5'}}}) == {'11': 0.5}


@pytest.mark.parametrize('view', ['actual', 'projected', 'ros', 'pending'])
def test_render_all_modes(monkeypatch, view):
    state = ros.build_forecast(*fixtures())
    season = datetime.now(timezone.utc).year
    state.update(season=season, league_key='league', updated_at='2026-10-29T20:00:00+00:00', warnings=[])
    data = {'mode': 'dashboard', 'season': season, 'league_key': 'league', 'current_week': 8, 'maximum_week': 8,
            'available_weeks': list(range(1, 9)), 'teams': [{'name': 'One'}, {'name': 'Two'}],
            'weeks': {str(w): {'One': 20, 'Two': 10} for w in range(1, 8)}, 'week_numbers': [str(w) for w in range(1, 8)],
            'yahoo_teams': {n: {'weekly_results': {str(w): 'W' if n=='One' else 'L' for w in range(1,8)}} for n in ['One', 'Two']},
            'weekly_mamba_points': {str(w): {'One': 2, 'Two': 1} for w in range(1,8)}, 'actual_through_week': 7,
            'projected_scores': {'One': 15, 'Two': 15}, 'projection_available': True,
            'matchups': [{'teams': [{'name': 'One', 'score': 0, 'projected_score': 15}, {'name': 'Two', 'score': 0, 'projected_score': 15}]}]}
    monkeypatch.setattr(live, 'load_cached_yahoo_dashboard_data', lambda **kw: deepcopy(data))
    monkeypatch.setattr(live, 'load_trade_section', lambda **kw: {})
    monkeypatch.setattr(live, 'read_forecast', lambda: None if view=='pending' else deepcopy(state))
    app = FastAPI(); app.mount('/static', StaticFiles(directory='static'), name='static'); app.include_router(live.live_dashboard_router)
    response = TestClient(app).get(f'/?season={season}&view={"ros" if view=="pending" else view}')
    assert response.status_code == 200
    assert 'Current Week Projected' in response.text
    assert 'ROS (Week 13) Projected' in response.text
    if view=='ros':
        assert '230.00' in response.text
        assert '>W13<' in response.text
        assert 'Inspect optimized lineups' in response.text
        assert response.text.count('projection-mode-option is-active') == 1
    if view=='actual': assert '140.00' in response.text and '>W13<' not in response.text
    if view=='projected': assert '155.00' in response.text and '>W13<' not in response.text
    if view=='pending': assert 'Preparing the rest-of-season forecast' in response.text

@pytest.mark.parametrize('live_value,expected', [('118.37',118.37), ('0.00',0.0), (None,100.0)])
def test_live_team_projection_preferred_without_double_counting(live_value, expected):
    from app.yahoo_mamba import _team_from_resource
    resource = [{'team_key':'t1'}, {'team_points':{'total':'21.4'}}, {'team_projected_points':{'total':'100'}}]
    if live_value is not None:
        resource.append({'team_live_projected_points':{'total':live_value}})
    team = _team_from_resource(resource)
    assert team['projected_score'] == expected
    assert team['pregame_projected_score'] == 100


def test_ros_current_week_changes_results_but_not_future_or_cached_state():
    state = ros.build_forecast(*fixtures())
    state.update(league_key='league',season=2026)
    original = deepcopy(state)
    data = {'league_key':'league','season':2026,'current_week':8,
            'projected_scores':{'One':12,'Two':33},
            'matchups':[{'teams':[{'name':'One','projected_score':12}, {'name':'Two','projected_score':33}]}]}
    result = ros.apply_live_week(state,data)
    assert result['weeks']['8'] == {'One':12,'Two':33}
    assert result['yahoo_teams']['Two']['weekly_results']['8'] == 'W'
    assert result['weekly_mamba_points']['8'] == {'One':1,'Two':2}
    assert '8' not in result['lineups']
    assert result['weeks']['9'] == original['weeks']['9']
    assert result['lineups']['9'] == original['lineups']['9']
    assert result['weeks']['7'] == original['weeks']['7']
    assert state == original
