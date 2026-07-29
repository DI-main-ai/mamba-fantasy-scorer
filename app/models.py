from dataclasses import dataclass


@dataclass
class StandingRow:
    final_rank: int
    team_name: str
    hybrid_rank: float
    yahoo_rank: int
    mamba_rank: int
    mamba_points: float
    points_for: float