import json
import os
import secrets
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse


yahoo_router = APIRouter(prefix="/auth/yahoo", tags=["yahoo"])

YAHOO_AUTHORIZE_URL = "https://api.login.yahoo.com/oauth2/request_auth"
YAHOO_TOKEN_URL = "https://api.login.yahoo.com/oauth2/get_token"
YAHOO_FANTASY_TEST_URL = (
    "https://fantasysports.yahooapis.com/fantasy/v2/"
    "users;use_login=1/games;game_codes=nfl?format=json"
)


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
    request = urllib.request.Request(
        YAHOO_TOKEN_URL,
        data=data,
        method="POST",
    )
    credentials = f"{client_id}:{client_secret}".encode("utf-8")

    import base64

    request.add_header(
        "Authorization",
        "Basic " + base64.b64encode(credentials).decode("ascii"),
    )
    request.add_header(
        "Content-Type",
        "application/x-www-form-urlencoded",
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"Yahoo token request failed ({exc.code}): {body}",
        ) from exc


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

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(
            status_code=502,
            detail="Yahoo did not return an access token.",
        )

    request.session["yahoo_access_token"] = access_token
    if token_data.get("refresh_token"):
        request.session["yahoo_refresh_token"] = token_data[
            "refresh_token"
        ]

    return HTMLResponse(
        """
        <!doctype html>
        <html><head><meta name="viewport" content="width=device-width,initial-scale=1">
        <title>Yahoo Connected</title></head>
        <body style="font-family:Arial,sans-serif;background:#0d0d0f;color:#f7f7f8;padding:40px;">
        <h1 style="color:#fdb927;">Yahoo connected successfully.</h1>
        <p>The OAuth test worked. Your credentials were not exposed to the browser.</p>
        <p><a style="color:#b98be0;" href="/auth/yahoo/test">Test Fantasy Sports access</a></p>
        <p><a style="color:#b98be0;" href="/">Return to Mamba Fantasy</a></p>
        </body></html>
        """
    )


@yahoo_router.get("/test")
def test_yahoo_fantasy_access(request: Request):
    access_token = request.session.get("yahoo_access_token")
    if not access_token:
        return RedirectResponse("/auth/yahoo")

    yahoo_request = urllib.request.Request(YAHOO_FANTASY_TEST_URL)
    yahoo_request.add_header("Authorization", f"Bearer {access_token}")
    yahoo_request.add_header("Accept", "application/json")

    try:
        with urllib.request.urlopen(yahoo_request, timeout=20) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise HTTPException(
            status_code=502,
            detail=f"Yahoo Fantasy API test failed ({exc.code}): {body}",
        ) from exc

    return {
        "success": True,
        "message": "Yahoo Fantasy Sports API access is working.",
        "yahoo_response": payload,
    }
