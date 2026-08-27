import html
import urllib.parse
from typing import Any, Dict, List

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from app.yahoo_auth import _fantasy_get
from app.yahoo_mamba import _extract_unique_teams
from app.yahoo_seasons import _league_record, _walk_values_for_key


legacy_candidate_router = APIRouter(
    prefix="/auth/yahoo/mamba",
    tags=["yahoo-mamba-legacy"],
)

LEGACY_YEARS = (2013, 2012, 2011)


def _league_candidates_for_year(request: Request, year: int) -> List[Dict[str, Any]]:
    """Return every Yahoo NFL league the approved account joined that year."""

    payload = _fantasy_get(
        request,
        (
            "users;use_login=1/games;game_codes=nfl;"
            f"seasons={year}/leagues"
        ),
    )

    candidates: Dict[str, Dict[str, Any]] = {}

    for resource in _walk_values_for_key(payload, "league"):
        record = _league_record(resource)
        if not record or int(record.get("season") or 0) != year:
            continue

        league_key = str(record["league_key"])
        candidates.setdefault(
            league_key,
            {
                "league_key": league_key,
                "season": year,
                "name": record.get("name") or "Unnamed Yahoo League",
                "renew": record.get("renew"),
                "renewed": record.get("renewed"),
            },
        )

    for candidate in candidates.values():
        encoded_key = urllib.parse.quote(candidate["league_key"], safe=".-_")
        teams_payload = _fantasy_get(request, f"league/{encoded_key}/teams")
        teams = _extract_unique_teams(teams_payload)
        candidate["teams"] = teams
        candidate["team_count"] = len(teams)

    return sorted(
        candidates.values(),
        key=lambda item: (str(item.get("name") or "").casefold(), item["league_key"]),
    )


def _render_candidates(year_groups: List[Dict[str, Any]]) -> str:
    sections: List[str] = []

    for group in year_groups:
        year = int(group["year"])
        leagues = group["leagues"]

        if not leagues:
            sections.append(
                f"""
                <section class="year-section">
                    <h2>{year}</h2>
                    <div class="empty-card">No Yahoo NFL leagues were returned for this account.</div>
                </section>
                """
            )
            continue

        cards: List[str] = []
        for league in leagues:
            team_items = "".join(
                "<li>{}</li>".format(
                    html.escape(str(team.get("name") or "Unnamed Team"))
                )
                for team in league.get("teams", [])
            )

            renewal_bits: List[str] = []
            if league.get("renew"):
                renewal_bits.append(
                    "Previous-season link: <code>{}</code>".format(
                        html.escape(str(league["renew"]))
                    )
                )
            if league.get("renewed"):
                renewal_bits.append(
                    "Next-season link: <code>{}</code>".format(
                        html.escape(str(league["renewed"]))
                    )
                )

            renewal_html = "<br>".join(renewal_bits) or "No Yahoo renewal link exposed."

            cards.append(
                """
                <article class="league-card">
                    <div class="card-heading">
                        <div>
                            <div class="candidate-label">Candidate league</div>
                            <h3>{name}</h3>
                        </div>
                        <div class="team-count">{team_count} teams</div>
                    </div>
                    <div class="meta-row"><strong>League key</strong><code>{league_key}</code></div>
                    <div class="meta-row renewal"><strong>Yahoo history links</strong><span>{renewal_html}</span></div>
                    <h4>Teams in this league</h4>
                    <ol class="team-list">{team_items}</ol>
                </article>
                """.format(
                    name=html.escape(str(league.get("name") or "Unnamed Yahoo League")),
                    team_count=int(league.get("team_count") or 0),
                    league_key=html.escape(str(league["league_key"])),
                    renewal_html=renewal_html,
                    team_items=team_items or "<li>No teams returned.</li>",
                )
            )

        sections.append(
            """
            <section class="year-section">
                <h2>{year}</h2>
                <div class="league-grid">{cards}</div>
            </section>
            """.format(year=year, cards="".join(cards))
        )

    return """
    <!doctype html>
    <html lang="en">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>2011-2013 League Candidates - Mamba Fantasy</title>
        <style>
            :root {{ color-scheme: dark; }}
            * {{ box-sizing: border-box; }}
            body {{
                margin: 0;
                background: #0d0d0f;
                color: #f5f3f8;
                font-family: Inter, Arial, sans-serif;
            }}
            main {{ max-width: 1050px; margin: 0 auto; padding: 34px 18px 70px; }}
            h1 {{ margin: 0 0 10px; color: #fdb927; font-size: clamp(30px, 5vw, 48px); }}
            .intro {{ color: #bcb8c5; line-height: 1.6; max-width: 820px; }}
            .notice {{
                margin: 22px 0 32px;
                padding: 15px 17px;
                border: 1px solid rgba(253, 185, 39, .35);
                border-radius: 12px;
                background: rgba(253, 185, 39, .07);
                color: #e8e3ed;
            }}
            a {{ color: #c59aeb; }}
            .actions {{ display: flex; gap: 18px; flex-wrap: wrap; margin: 20px 0; }}
            .year-section {{ margin-top: 38px; }}
            .year-section > h2 {{ color: #c59aeb; font-size: 28px; }}
            .league-grid {{ display: grid; gap: 18px; }}
            .league-card, .empty-card {{
                background: #17171c;
                border: 1px solid #303038;
                border-radius: 16px;
                padding: 21px;
            }}
            .card-heading {{ display: flex; justify-content: space-between; gap: 20px; align-items: flex-start; }}
            .candidate-label {{ color: #fdb927; font-size: 11px; font-weight: 800; letter-spacing: .08em; text-transform: uppercase; }}
            h3 {{ margin: 6px 0 12px; font-size: 23px; }}
            .team-count {{
                white-space: nowrap;
                border: 1px solid rgba(125, 70, 173, .55);
                background: rgba(125, 70, 173, .15);
                border-radius: 999px;
                padding: 7px 11px;
                color: #d4b4ee;
                font-size: 12px;
                font-weight: 700;
            }}
            .meta-row {{ display: grid; grid-template-columns: 145px 1fr; gap: 12px; margin: 9px 0; color: #c9c5ce; }}
            code {{ color: #d1acef; overflow-wrap: anywhere; }}
            h4 {{ color: #fdb927; margin: 21px 0 8px; }}
            .team-list {{ columns: 2; margin: 0; padding-left: 24px; }}
            .team-list li {{ margin: 7px 16px 7px 0; break-inside: avoid; }}
            footer {{ color: #8e8995; margin-top: 42px; font-size: 12px; }}
            @media (max-width: 640px) {{
                .card-heading {{ flex-direction: column; }}
                .meta-row {{ grid-template-columns: 1fr; gap: 3px; }}
                .team-list {{ columns: 1; }}
            }}
        </style>
    </head>
    <body>
        <main>
            <h1>Find the missing Mamba seasons</h1>
            <p class="intro">
                Yahoo's automatic renewal chain currently stops at 2014. This diagnostic asks
                Yahoo for every NFL fantasy league this approved account participated in during
                2011, 2012, and 2013 so we can identify the correct predecessor manually.
            </p>
            <div class="notice">
                Nothing on this page changes Mamba's season history yet. Please identify the
                correct league for each year from the league name and team list; then we can
                explicitly attach those league keys to the Mamba history.
            </div>
            <div class="actions">
                <a href="/">Return to Mamba Fantasy</a>
                <a href="/auth/yahoo/mamba/seasons">Current linked season chain</a>
            </div>
            {sections}
            <footer>
                Fantasy data provided by
                <a href="https://football.fantasysports.yahoo.com/" target="_blank" rel="noopener noreferrer">Yahoo Fantasy</a>.
                Read-only diagnostic; no Yahoo data is modified.
            </footer>
        </main>
    </body>
    </html>
    """.format(sections="".join(sections))


@legacy_candidate_router.get("/legacy-candidates", response_class=HTMLResponse)
def legacy_league_candidates(request: Request):
    year_groups = [
        {
            "year": year,
            "leagues": _league_candidates_for_year(request, year),
        }
        for year in LEGACY_YEARS
    ]
    return HTMLResponse(_render_candidates(year_groups))
