from typing import Dict, List, Tuple

from app.models import StandingRow


ScoreMap = Dict[str, float]
WeeklyScoreMap = Dict[str, ScoreMap]


def calculate_weekly_mamba_points(
    weekly_scores: ScoreMap,
) -> ScoreMap:
    """
    Award Mamba Points from lowest score to highest score.

    For a 14-team league:
        lowest score = 1 point
        highest score = 14 points

    Exact score ties receive the average of the occupied positions.
    """

    sorted_scores: List[Tuple[str, float]] = sorted(
        weekly_scores.items(),
        key=lambda item: item[1],
    )

    results: ScoreMap = {}

    index = 0

    while index < len(sorted_scores):
        tied_score = sorted_scores[index][1]
        tie_end = index

        while (
            tie_end + 1 < len(sorted_scores)
            and sorted_scores[tie_end + 1][1] == tied_score
        ):
            tie_end += 1

        first_position = index + 1
        last_position = tie_end + 1

        average_position = (
            first_position + last_position
        ) / 2.0

        for tied_index in range(index, tie_end + 1):
            team_name = sorted_scores[tied_index][0]
            results[team_name] = average_position

        index = tie_end + 1

    return results


def calculate_points_for(
    weeks: WeeklyScoreMap,
) -> ScoreMap:
    totals: ScoreMap = {}

    for weekly_scores in weeks.values():
        for team_name, score in weekly_scores.items():
            totals[team_name] = (
                totals.get(team_name, 0.0) + float(score)
            )

    return totals


def calculate_season_mamba_points(
    weeks: WeeklyScoreMap,
) -> ScoreMap:
    totals: ScoreMap = {}

    for weekly_scores in weeks.values():
        weekly_points = calculate_weekly_mamba_points(
            weekly_scores
        )

        for team_name, points in weekly_points.items():
            totals[team_name] = (
                totals.get(team_name, 0.0) + points
            )

    return totals


def calculate_mamba_ranks(
    mamba_points: ScoreMap,
    points_for: ScoreMap,
) -> Dict[str, int]:
    """
    Rank teams by total Mamba Points.

    Points For is used as the tiebreaker.
    """

    ordered_teams = sorted(
        mamba_points.keys(),
        key=lambda team_name: (
            -mamba_points[team_name],
            -points_for[team_name],
            team_name.lower(),
        ),
    )

    return {
        team_name: rank
        for rank, team_name in enumerate(
            ordered_teams,
            start=1,
        )
    }


def build_hybrid_standings(
    weeks: WeeklyScoreMap,
    yahoo_ranks: Dict[str, int],
) -> List[StandingRow]:
    points_for = calculate_points_for(weeks)

    mamba_points = calculate_season_mamba_points(
        weeks
    )

    mamba_ranks = calculate_mamba_ranks(
        mamba_points=mamba_points,
        points_for=points_for,
    )

    standings = []

    for team_name, yahoo_rank in yahoo_ranks.items():
        mamba_rank = mamba_ranks[team_name]

        hybrid_rank = (
            float(yahoo_rank) + float(mamba_rank)
        ) / 2.0

        standings.append(
            StandingRow(
                final_rank=0,
                team_name=team_name,
                hybrid_rank=hybrid_rank,
                yahoo_rank=int(yahoo_rank),
                mamba_rank=mamba_rank,
                mamba_points=mamba_points[team_name],
                points_for=points_for[team_name],
            )
        )

    standings.sort(
        key=lambda row: (
            row.hybrid_rank,
            -row.points_for,
            row.team_name.lower(),
        )
    )

    for final_rank, row in enumerate(
        standings,
        start=1,
    ):
        row.final_rank = final_rank

    return standings


def validate_historical_mamba_points(
    weeks: WeeklyScoreMap,
    expected_weekly_points: WeeklyScoreMap,
) -> None:
    """
    Confirm that our Python scoring calculations reproduce
    the expected Excel/VBA results.
    """

    for week_number, weekly_scores in weeks.items():
        calculated = calculate_weekly_mamba_points(
            weekly_scores
        )

        expected = expected_weekly_points[week_number]

        if calculated != expected:
            raise ValueError(
                "Mamba regression check failed for "
                f"Week {week_number}.\n"
                f"Expected: {expected}\n"
                f"Calculated: {calculated}"
            )