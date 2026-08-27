import re
from typing import AsyncIterator

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response


HISTORICAL_SEASON = 2025
MAXIMUM_HISTORICAL_WEEK = 13

PERIOD_SELECTOR_STYLES = """
<style>
.period-controls {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    gap: 10px;
}
.period-selector {
    display: flex;
    align-items: flex-end;
    flex-wrap: wrap;
    gap: 10px;
}
.period-field {
    display: flex;
    flex-direction: column;
    gap: 6px;
}
.period-field > span {
    color: var(--text-muted);
    font-size: 9px;
    font-weight: 800;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
.period-field select {
    min-width: 118px;
    height: 40px;
    padding: 0 34px 0 12px;
    border: 1px solid rgba(125, 70, 173, 0.48);
    border-radius: 10px;
    background-color: rgba(25, 25, 31, 0.92);
    color: var(--text-primary);
    font: inherit;
    font-size: 12px;
    font-weight: 800;
    cursor: pointer;
}
.period-field select:focus {
    border-color: var(--gold);
    outline: 2px solid rgba(253, 185, 39, 0.15);
    outline-offset: 1px;
}
.period-submit {
    height: 40px;
    padding: 0 15px;
    border: 1px solid rgba(253, 185, 39, 0.5);
    border-radius: 10px;
    background: rgba(253, 185, 39, 0.1);
    color: var(--gold);
    font-size: 11px;
    font-weight: 900;
    cursor: pointer;
}
.period-submit:hover {
    background: rgba(253, 185, 39, 0.17);
}
.period-season-note {
    color: var(--text-secondary);
    font-size: 10px;
}
@media (max-width: 620px) {
    .period-controls,
    .period-selector {
        width: 100%;
    }
    .period-selector {
        display: grid;
        grid-template-columns: minmax(0, 0.8fr) minmax(0, 1.2fr);
        gap: 8px;
    }
    .period-field select {
        width: 100%;
        min-width: 0;
        height: 38px;
        font-size: 11px;
    }
    .period-submit {
        display: none;
    }
}
</style>
"""


def build_period_selector(selected_week: int) -> str:
    week_options = "".join(
        (
            f'<option value="{week_number}" '
            f'{"selected" if week_number == selected_week else ""}>'
            f'Week {week_number}</option>'
        )
        for week_number in range(1, MAXIMUM_HISTORICAL_WEEK + 1)
    )

    return f"""
        <div class="period-controls">
            <form
                id="period-selector-form"
                class="period-selector"
                method="get"
                action="/"
            >
                <label class="period-field">
                    <span>Season</span>
                    <select
                        id="period-season-select"
                        aria-label="Season"
                    >
                        <option value="{HISTORICAL_SEASON}" selected>
                            {HISTORICAL_SEASON}
                        </option>
                    </select>
                </label>
                <label class="period-field">
                    <span>Through Week</span>
                    <select
                        id="period-week-select"
                        class="period-week-select"
                        aria-label="Through Week"
                    >
                        {week_options}
                    </select>
                </label>
                <button class="period-submit" type="submit">
                    Update
                </button>
            </form>
            <span class="updated-label">
                Recalculated through Week {selected_week}
            </span>
            <span class="period-season-note" id="period-season-note">
                Loading Yahoo league seasons…
            </span>
        </div>
        <script>
        (function () {{
            const form = document.getElementById('period-selector-form');
            const seasonSelect = document.getElementById('period-season-select');
            const weekSelect = document.getElementById('period-week-select');
            const seasonNote = document.getElementById('period-season-note');

            if (!form || !seasonSelect || !weekSelect) return;

            function navigateToSelection() {{
                const season = Number(seasonSelect.value || {HISTORICAL_SEASON});
                const week = Number(weekSelect.value || 1);

                if (season === {HISTORICAL_SEASON}) {{
                    window.location.href = '/?week=' + encodeURIComponent(week);
                    return;
                }}

                window.location.href =
                    '/auth/yahoo/mamba/history?season=' + encodeURIComponent(season) +
                    '&week=' + encodeURIComponent(week);
            }}

            form.addEventListener('submit', function (event) {{
                event.preventDefault();
                navigateToSelection();
            }});

            seasonSelect.addEventListener('change', navigateToSelection);
            weekSelect.addEventListener('change', navigateToSelection);

            fetch('/auth/yahoo/mamba/seasons', {{
                credentials: 'same-origin',
                headers: {{ 'Accept': 'application/json' }}
            }})
            .then(function (response) {{
                if (!response.ok) throw new Error('Yahoo season discovery unavailable');
                return response.json();
            }})
            .then(function (data) {{
                const seasons = Array.isArray(data.season_numbers)
                    ? data.season_numbers.map(Number)
                    : [];

                if (!seasons.includes({HISTORICAL_SEASON})) {{
                    seasons.push({HISTORICAL_SEASON});
                }}

                seasons.sort(function (a, b) {{ return b - a; }});
                seasonSelect.innerHTML = '';

                seasons.forEach(function (season) {{
                    const option = document.createElement('option');
                    option.value = String(season);
                    option.textContent = String(season);
                    if (season === {HISTORICAL_SEASON}) option.selected = true;
                    seasonSelect.appendChild(option);
                }});

                if (seasonNote) {{
                    seasonNote.textContent =
                        seasons.length > 1
                            ? 'Yahoo league history loaded.'
                            : 'Only the 2025 historical season is currently linked.';
                }}
            }})
            .catch(function () {{
                if (seasonNote) {{
                    seasonNote.textContent =
                        'Reconnect Yahoo to load additional league seasons.';
                }}
            }});
        }})();
        </script>
    """


class HistoricalWeekSelectorMiddleware(BaseHTTPMiddleware):
    """Add the historical week selector to the rendered dashboard."""

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        if request.url.path != "/":
            return response

        content_type = response.headers.get("content-type", "")

        if "text/html" not in content_type:
            return response

        body = b"".join(
            [chunk async for chunk in response.body_iterator]
        )
        html = body.decode("utf-8")

        week_match = re.search(
            r'<span class="updated-label">\s*Through Week (\d+)\s*</span>',
            html,
        )

        if week_match is None:
            return Response(
                content=body,
                status_code=response.status_code,
                headers=dict(response.headers),
                media_type=response.media_type,
            )

        selected_week = int(week_match.group(1))
        selector = build_period_selector(selected_week)

        html = re.sub(
            r'<span class="updated-label">\s*Through Week \d+\s*</span>',
            selector,
            html,
            count=1,
        )
        html = html.replace(
            "</head>",
            f"{PERIOD_SELECTOR_STYLES}</head>",
            1,
        )

        headers = dict(response.headers)
        headers.pop("content-length", None)

        return Response(
            content=html,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )
