import json
from pathlib import Path
from typing import Any, Dict


PROJECT_ROOT = Path(__file__).resolve().parent.parent

HISTORICAL_DATA_PATH = (
    PROJECT_ROOT
    / "data"
    / "historical"
    / "historical_2025.json"
)

YAHOO_WL_DATA_PATH = (
    PROJECT_ROOT
    / "data"
    / "historical"
    / "yahoo_wl_2025.json"
)


def load_json_file(
    file_path: Path,
    description: str,
) -> Dict[str, Any]:
    """
    Load and return a JSON object from disk.
    """

    if not file_path.exists():
        raise FileNotFoundError(
            f"{description} was not found at: {file_path}"
        )

    try:
        with file_path.open(
            "r",
            encoding="utf-8",
        ) as file:
            data = json.load(file)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"{description} contains invalid JSON: {error}"
        ) from error

    if not isinstance(data, dict):
        raise ValueError(
            f"{description} must contain a JSON object."
        )

    return data


def load_historical_season() -> Dict[str, Any]:
    """
    Load weekly Points For, weekly Mamba Points,
    and historical Yahoo ranks.
    """

    data = load_json_file(
        file_path=HISTORICAL_DATA_PATH,
        description="Historical season data file",
    )

    required_fields = {
        "season",
        "weeks",
        "expected_weekly_mamba_points",
        "official_yahoo_ranks_after_week_13",
    }

    missing_fields = required_fields.difference(
        data.keys()
    )

    if missing_fields:
        missing_text = ", ".join(
            sorted(missing_fields)
        )

        raise ValueError(
            "Historical season data is missing "
            f"required fields: {missing_text}"
        )

    if not isinstance(data["weeks"], dict):
        raise ValueError(
            "Historical season 'weeks' must be an object."
        )

    if not isinstance(
        data["expected_weekly_mamba_points"],
        dict,
    ):
        raise ValueError(
            "'expected_weekly_mamba_points' "
            "must be an object."
        )

    if not isinstance(
        data["official_yahoo_ranks_after_week_13"],
        dict,
    ):
        raise ValueError(
            "'official_yahoo_ranks_after_week_13' "
            "must be an object."
        )

    return data


def load_yahoo_wl_season() -> Dict[str, Any]:
    """
    Load the week-by-week Yahoo W/L audit data.
    """

    data = load_json_file(
        file_path=YAHOO_WL_DATA_PATH,
        description="Yahoo W/L data file",
    )

    required_fields = {
        "season",
        "through_week",
        "ranking_rule",
        "teams",
    }

    missing_fields = required_fields.difference(
        data.keys()
    )

    if missing_fields:
        missing_text = ", ".join(
            sorted(missing_fields)
        )

        raise ValueError(
            "Yahoo W/L data is missing required "
            f"fields: {missing_text}"
        )

    teams = data["teams"]

    if not isinstance(teams, dict):
        raise ValueError(
            "Yahoo W/L 'teams' must be an object."
        )

    required_team_fields = {
        "total_wins",
        "total_points_for",
        "official_yahoo_rank",
        "weekly_results",
    }

    for team_name, team_data in teams.items():
        if not isinstance(team_data, dict):
            raise ValueError(
                "Yahoo W/L data for "
                f"'{team_name}' must be an object."
            )

        missing_team_fields = (
            required_team_fields.difference(
                team_data.keys()
            )
        )

        if missing_team_fields:
            missing_text = ", ".join(
                sorted(missing_team_fields)
            )

            raise ValueError(
                f"Yahoo W/L data for '{team_name}' "
                f"is missing: {missing_text}"
            )

        weekly_results = team_data[
            "weekly_results"
        ]

        if not isinstance(weekly_results, dict):
            raise ValueError(
                "Yahoo weekly results for "
                f"'{team_name}' must be an object."
            )

        for week_number, result in (
            weekly_results.items()
        ):
            if result not in {"W", "L", "T"}:
                raise ValueError(
                    f"Invalid Yahoo result '{result}' "
                    f"for '{team_name}', Week "
                    f"{week_number}. Expected W, L, or T."
                )

    return data