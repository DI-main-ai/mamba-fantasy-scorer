import base64
import html
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


yahoo_router = APIRouter(prefix="/auth/yahoo", tags=["yahoo"])

YAHOO_AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_FANTASY_BASE_URL = "https://fantasysports.yahooapis.com/fantasy/v2"
TARGET_SEASON = 2026

# OAuth tokens stay server-side only. The browser session contains only an
# opaque session id plus the short-lived OAuth state value. The test service is
# intentionally ephemeral: a Render restart simply requires reconnecting Yahoo.
_TOKEN_STORE: Dict[str, Dict[str, Any]] = {}
_TOKEN_STORE_LOCK = threading.Lock()


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(
            status_code=503,
            detail=f"Missing required server environment variable: {name}",
        )
    return value


def _token_request(form_data: Dict[str, str]) -> Dict[str, Any]:
    client_id = _required_env("YAHOO_CLIENT_ID")
    client_secret = _required_env("YAHOO_CLIENT_SECRET")

    data = urllib.parse.urlencode(form_data).encode("utf-8")
    yahoo_request = urllib.request.Request(
        YAHOO_TOKEN_URL,
        data=data,
        method="POST",
    )
    credentials = f"{client_id}:{client_secret}".encode("utf-8")

    yahoo_request.add_header(
        "Authorization",
        "Basic " + base64.b64encode(credentials).decode("ascii"),
    )
    yahoo_request.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded",
    )

    try:
        with urllib.request.urlopen(yahoo_request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"Yahoo token request failed ({exc.code}): {body}",
        ) from exc


def _session_id(request: Request, create: bool = True) -> Optional[str]:
    session_id = request.session.get("yahoo_session_id")

    if not session_id and create:
        session_id = secrets.token_urlsafe(32)
        request.session["yahoo_session_id"] = session_id

    return session_id


def _store_tokens(
    request: Request,
    token_data: Dict[str, Any],
) -> None:
    session_id = _session_id(request)
    assert session_id is not None

    with _TOKEN_STORE_LOCK:
        existing = _TOKEN_STORE.get(session_id, {})
        refresh_token = token_data.get("refresh_token") or existing.get(
            "refresh_token"
        )

        expires_in = int(token_data.get("expires_in") or 3600)
        _TOKEN_STORE[session_id] = {
            "access_token": token_data.get("access_token"),
            "refresh_token": refresh_token,
            # Refresh a little early so a token cannot expire mid-request.
            "expires_at": time.time() + max(expires_in - 60, 60),
        }


def _get_stored_tokens(request: Request) -> Optional[Dict[str, Any]]:
    session_id = _session_id(request, create=False)
    if not session_id:
        return None

    with _TOKEN_STORE_LOCK:
        token_data = _TOKEN_STORE.get(session_id)
        return dict(token_data) if token_data else None


def _refresh_access_token(request: Request) -> str:
    token_data = _get_stored_tokens(request)
    refresh_token = token_data.get("refresh_token") if token_data else None

    if not refresh_token:
        raise HTTPException(
            status_code=401,
            detail="Yahoo authorization expired. Please reconnect Yahoo.",
        )

    refreshed = _token_request(
        {
            "grant_type": "refresh_token",
            "redirect_uri": _required_env("YAHOO_REDIRECT_URI"),
            "refresh_token": str(refresh_token),
        }
    )
    access_token = refreshed.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="Yahoo did not return a refreshed access token.",
        )

    _store_tokens(request, refreshed)
    return str(access_token)


def _get_access_token(request: Request) -> str:
    token_data = _get_stored_tokens(request)
    if not token_data:
        raise HTTPException(
            status_code=401,
            detail="Yahoo is not connected for this browser session.",
        )

    access_token = token_data.get("access_token")
    expires_at = float(token_data.get("expires_at") or 0)

    if access_token and expires_at > time.time():
        return str(access_token)

    return _refresh_access_token(request)


def _fantasy_url(path: str) -> str:
    url = f"{YAHOO_FANTASY_BASE_URL}/{path.lstrip('/')}"
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}format=json"


def _fantasy_get(
    request: Request,
    path: str,
    *,
    allow_refresh_retry: bool = True,
) -> Dict[str, Any]:
    access_token = _get_access_token(request)
    url = _fantasy_url(path)

    for attempt in range(3):
        yahoo_request = urllib.request.Request(url)
        yahoo_request.add_header("Authorization", f"Bearer {access_token}")
        yahoo_request.add_header("Accept", "application/json")

        try:
            with urllib.request.urlopen(yahoo_request, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")

            if exc.code == 401 and allow_refresh_retry:
                access_token = _refresh_access_token(request)
                allow_refresh_retry = False
                continue

            if exc.code == 429 or 500 <= exc.code <= 599:
                if attempt < 2:
                    retry_after = exc.headers.get("Retry-After", "")
                    try:
                        delay = min(max(float(retry_after), 1.0), 10.0)
                    except (TypeError, ValueError):
                        delay = float(2**attempt)
                    time.sleep(delay)
                    continue

            raise HTTPException(
                status_code=502,
                detail=(
                    f"Yahoo Fantasy API request failed ({exc.code}): {body}"
                ),
            ) from exc

    raise HTTPException(
        status_code=502,
        detail="Yahoo Fantasy API request failed after retries.",
    )


def _collection_entries(collection: Any) -> List[Any]:
    if not isinstance(collection, dict):
        return []

    numbered_entries = []
    for key, value in collection.items():
        if str(key).isdigit():
            numbered_entries.append((int(key), value))

    return [value for _, value in sorted(numbered_entries)]


def _scalar_fields(value: Any) -> Dict[str, Any]:
    """Flatten Yahoo's metadata fragments into scalar key/value fields."""

    fields: Dict[str, Any] = {}

    def visit(node: Any) -> None:
        if isinstance(node, list):
            for item in node:
                visit(item)
            return

        if not isinstance(node, dict):
            return

        for key, item in node.items():
            if isinstance(item, (str, int, float, bool)) or item is None:
                fields.setdefault(str(key), item)
            elif key not in {
                "leagues",
                "teams",
                "players",
                "scoreboard",
                "standings",
                "transactions",
            }:
                visit(item)

    visit(value)
    return fields


def _extract_2026_leagues(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    fantasy_content = payload.get("fantasy_content", {})
    users = fantasy_content.get("users", {})
    leagues: List[Dict[str, Any]] = []

    for user_entry in _collection_entries(users):
        user_resource = user_entry.get("user") if isinstance(user_entry, dict) else None
        if not isinstance(user_resource, list):
            continue

        games = None
        for item in user_resource:
            if isinstance(item, dict) and "games" in item:
                games = item["games"]
                break

        for game_entry in _collection_entries(games):
            game_resource = game_entry.get("game") if isinstance(game_entry, dict) else None
            if not isinstance(game_resource, list):
                continue

            game_fields = _scalar_fields(game_resource[0] if game_resource else {})
            if str(game_fields.get("season", "")) != str(TARGET_SEASON):
                continue

            league_collection = None
            for item in game_resource[1:]:
                if isinstance(item, dict) and "leagues" in item:
                    league_collection = item["leagues"]
                    break

            for league_entry in _collection_entries(league_collection):
                league_resource = (
                    league_entry.get("league")
                    if isinstance(league_entry, dict)
                    else None
                )
                if league_resource is None:
                    continue

                metadata = (
                    league_resource[0]
                    if isinstance(league_resource, list) and league_resource
                    else league_resource
                )
                league_fields = _scalar_fields(metadata)
                league_key = league_fields.get("league_key")
                if not league_key:
                    continue

                leagues.append(
                    {
                        "league_key": str(league_key),
                        "league_id": league_fields.get("league_id"),
                        "name": league_fields.get("name") or "Unnamed Yahoo League",
                        "num_teams": league_fields.get("num_teams"),
                        "current_week": league_fields.get("current_week"),
                        "start_week": league_fields.get("start_week"),
                        "end_week": league_fields.get("end_week"),
                        "season": game_fields.get("season") or TARGET_SEASON,
                        "game_key": game_fields.get("game_key"),
                    }
                )

    # Defensive de-duplication in case Yahoo repeats a resource in its nested
    # collection response.
    unique: Dict[str, Dict[str, Any]] = {}
    for league in leagues:
        unique[league["league_key"]] = league

    return list(unique.values())


def _extract_teams(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    fantasy_content = payload.get("fantasy_content", {})
    league_resource = fantasy_content.get("league")
    if not isinstance(league_resource, list):
        return []

    teams_collection = None
    for item in league_resource:
        if isinstance(item, dict) and "teams" in item:
            teams_collection = item["teams"]
            break

    teams: List[Dict[str, Any]] = []
    for team_entry in _collection_entries(teams_collection):
        team_resource = (
            team_entry.get("team") if isinstance(team_entry, dict) else None
        )
        if team_resource is None:
            continue

        metadata = (
            team_resource[0]
            if isinstance(team_resource, list) and team_resource
            else team_resource
        )
        team_fields = _scalar_fields(metadata)
        team_key = team_fields.get("team_key")
        if not team_key:
            continue

        teams.append(
            {
                "team_key": str(team_key),
                "team_id": team_fields.get("team_id"),
                "name": team_fields.get("name") or "Unnamed Yahoo Team",
                "is_owned_by_current_login": team_fields.get(
                    "is_owned_by_current_login"
                ),
            }
        )

    return teams


def _render_leagues_page(leagues: List[Dict[str, Any]]) -> str:
    cards: List[str] = []

    for league in leagues:
        team_items = "".join(
            "<li><strong>{}</strong><br><code>{}</code></li>".format(
                html.escape(str(team["name"])),
                html.escape(str(team["team_key"])),
            )
            for team in league.get("teams", [])
        )

        cards.append(
            """
            <section class="card">
              <h2>{name}</h2>
              <div class="meta"><strong>League key:</strong> <code>{key}</code></div>
              <div class="meta"><strong>Yahoo teams:</strong> {team_count}</div>
              <div class="meta"><strong>Current week:</strong> {current_week}</div>
              <h3>Teams</h3>
              <ol>{teams}</ol>
            </section>
            """.format(
                name=html.escape(str(league["name"])),
                key=html.escape(str(league["league_key"])),
                team_count=html.escape(
                    str(league.get("num_teams") or len(league.get("teams", [])))
                ),
                current_week=html.escape(str(league.get("current_week") or "—")),
                teams=team_items or "<li>No teams returned yet.</li>",
            )
        )

    if not cards:
        cards.append(
            """
            <section class="card">
              <h2>No 2026 NFL leagues found</h2>
              <p>Yahoo authenticated successfully, but no 2026 fantasy football
              leagues were returned for this Yahoo account.</p>
            </section>
            """
        )

    return """
    <!doctype html>
    <html lang="en">
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Yahoo League Discovery - Mamba Fantasy</title>
      <style>
        body {{ margin:0; padding:32px 18px 48px; font-family:Arial,sans-serif;
               background:#0d0d0f; color:#f7f7f8; }}
        main {{ max-width:900px; margin:0 auto; }}
        h1 {{ color:#fdb927; margin-bottom:8px; }}
        .sub {{ color:#b8b8c2; margin-bottom:26px; line-height:1.5; }}
        .card {{ background:#17171c; border:1px solid #303038; border-radius:16px;
                 padding:22px; margin:18px 0; }}
        h2 {{ color:#fdb927; margin-top:0; }}
        h3 {{ color:#c7a7e8; margin-bottom:10px; }}
        .meta {{ margin:7px 0; color:#d7d7dc; }}
        code {{ color:#c7a7e8; overflow-wrap:anywhere; }}
        li {{ margin:9px 0; }}
        a {{ color:#c7a7e8; }}
        .actions {{ display:flex; gap:18px; flex-wrap:wrap; margin:24px 0; }}
        footer {{ margin-top:30px; color:#9999a3; font-size:13px; }}
      </style>
    </head>
    <body>
      <main>
        <h1>2026 Yahoo Fantasy Football leagues</h1>
        <p class="sub">Live read-only discovery from the Yahoo Fantasy Sports API.
        Nothing on this page is written back to Yahoo or permanently stored by Mamba.</p>
        <div class="actions">
          <a href="/auth/yahoo/leagues">Refresh from Yahoo</a>
          <a href="/">Return to Mamba Fantasy</a>
          <a href="/auth/yahoo">Reconnect Yahoo</a>
        </div>
        {cards}
        <footer>
          Fantasy data provided by
          <a href="https://football.fantasysports.yahoo.com/" target="_blank" rel="noopener noreferrer">Yahoo Fantasy</a>.
        </footer>
      </main>
    </body>
    </html>
    """.format(cards="".join(cards))


@yahoo_router.get("", response_class=RedirectResponse)
def start_yahoo_login(request: Request):
    client_id = _required_env("YAHOO_CLIENT_ID")
    redirect_uri = _required_env("YAHOO_REDIRECT_URI")

    state = secrets.token_urlsafe(32)
    request.session["yahoo_oauth_state"] = state

    params = urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "state": state,
        }
    )

    return RedirectResponse(f"{YAHOO_AUTHORIZE_URL}?{params}")


@yahoo_router.get("/callback", response_class=HTMLResponse)
def yahoo_callback(
    request: Request,
    code: str = "",
    state: str = "",
    error: str = "",
):
    if error:
        raise HTTPException(
            status_code=400,
            detail=f"Yahoo authorization returned an error: {error}",
        )

    expected_state = request.session.pop("yahoo_oauth_state", None)
    if not expected_state or not state or not secrets.compare_digest(
        expected_state,
        state,
    ):
        raise HTTPException(status_code=400, detail="Invalid OAuth state.")

    if not code:
        raise HTTPException(
            status_code=400,
            detail="Yahoo did not return an authorization code.",
        )

    redirect_uri = _required_env("YAHOO_REDIRECT_URI")
    token_data = _token_request(
        {
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
            "code": code,
        }
    )

    if not token_data.get("access_token"):
        raise HTTPException(
            status_code=502,
            detail="Yahoo did not return an access token.",
        )

    # Remove the earlier prototype's browser-stored tokens, if present.
    request.session.pop("yahoo_access_token", None)
    request.session.pop("yahoo_refresh_token", None)
    _store_tokens(request, token_data)

    return HTMLResponse(
        """
        <!doctype html>
        <html lang="en"><head><meta charset="utf-8">
        <meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Yahoo Connected</title></head>
        <body style="font-family:Arial,sans-serif;background:#0d0d0f;color:#f7f7f8;padding:40px;">
        <h1 style="color:#fdb927;">Yahoo connected successfully.</h1>
        <p>The OAuth connection worked and Yahoo tokens are being kept server-side.</p>
        <p><a style="color:#b98be0;" href="/auth/yahoo/leagues">Discover my 2026 leagues and teams</a></p>
        <p><a style="color:#b98be0;" href="/auth/yahoo/test">Run basic Fantasy Sports access test</a></p>
        <p><a style="color:#b98be0;" href="/">Return to Mamba Fantasy</a></p>
        <footer style="margin-top:32px;color:#999;font-size:13px;">
          Fantasy data provided by <a style="color:#b98be0;" href="https://football.fantasysports.yahoo.com/">Yahoo Fantasy</a>.
        </footer>
        </body></html>
        """
    )


@yahoo_router.get("/test")
def test_yahoo_fantasy_access(request: Request):
    payload = _fantasy_get(
        request,
        f"users;use_login=1/games;game_codes=nfl;seasons={TARGET_SEASON}",
    )

    fantasy_content = payload.get("fantasy_content", {})
    return {
        "success": True,
        "message": "Yahoo Fantasy Sports API access is working.",
        "season": TARGET_SEASON,
        "response_time": fantasy_content.get("time"),
        "yahoo_refresh_rate": fantasy_content.get("refresh_rate"),
        "next": "/auth/yahoo/leagues",
    }


@yahoo_router.get("/leagues", response_class=HTMLResponse)
def discover_yahoo_leagues(request: Request):
    try:
        payload = _fantasy_get(
            request,
            (
                "users;use_login=1/games;game_codes=nfl;"
                f"seasons={TARGET_SEASON}/leagues"
            ),
        )
    except HTTPException as exc:
        if exc.status_code == 401:
            return RedirectResponse("/auth/yahoo")
        raise

    leagues = _extract_2026_leagues(payload)

    for league in leagues:
        league_key = urllib.parse.quote(str(league["league_key"]), safe=".-_")
        teams_payload = _fantasy_get(
            request,
            f"league/{league_key}/teams",
        )
        league["teams"] = _extract_teams(teams_payload)

    return HTMLResponse(_render_leagues_page(leagues))
