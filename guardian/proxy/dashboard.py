"""Simple dashboard for AI Guardian."""
from guardian.models.database import async_session, UsageLog
from guardian.cost.tracker import check_budget
from fastapi import Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import os

templates_dir = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=templates_dir)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <title>AI Guardian Dashboard</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0a0a0f; color: #e0e0e0; padding: 2rem; }
        h1 { color: #00ff88; margin-bottom: 1.5rem; font-size: 1.8rem; }
        h2 { color: #88ccff; margin: 1.5rem 0 0.8rem; font-size: 1.2rem; }
        .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin-bottom: 2rem; }
        .card { background: #151520; border: 1px solid #2a2a3a; border-radius: 8px; padding: 1.2rem; }
        .card .label { font-size: 0.8rem; color: #888; text-transform: uppercase; letter-spacing: 0.05em; }
        .card .value { font-size: 1.8rem; font-weight: bold; color: #00ff88; margin-top: 0.3rem; }
        .card .value.warn { color: #ffaa00; }
        .card .value.danger { color: #ff4444; }
        table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
        th, td { padding: 0.6rem 0.8rem; text-align: left; border-bottom: 1px solid #2a2a3a; }
        th { color: #888; font-size: 0.8rem; text-transform: uppercase; }
        .badge { display: inline-block; padding: 0.2rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: bold; }
        .badge.pass { background: #00ff8833; color: #00ff88; }
        .badge.warn { background: #ffaa0033; color: #ffaa00; }
        .badge.fail { background: #ff444433; color: #ff4444; }
        .badge.ok { background: #00ff8833; color: #00ff88; }
        .badge.warning { background: #ffaa0033; color: #ffaa00; }
        .badge.exceeded { background: #ff444433; color: #ff4444; }
        pre { background: #1a1a2a; padding: 0.8rem; border-radius: 4px; overflow-x: auto; font-size: 0.85rem; }
        .refresh { float: right; background: #2a2a3a; color: #88ccff; border: 1px solid #3a3a5a; padding: 0.4rem 1rem; border-radius: 4px; cursor: pointer; }
        .refresh:hover { background: #3a3a5a; }
    </style>
</head>
<body>
    <button class="refresh" onclick="location.reload()">Refresh</button>
    <h1>🛡️ AI Guardian Dashboard</h1>

    <div class="grid">
        <div class="card">
            <div class="label">Total Requests</div>
            <div class="value">{{ stats.total_requests }}</div>
        </div>
        <div class="card">
            <div class="label">Total Cost</div>
            <div class="value">${{ "%.4f"|format(stats.total_cost_usd) }}</div>
        </div>
        <div class="card">
            <div class="label">Total Tokens</div>
            <div class="value">{{ "{:,}".format(stats.total_tokens) }}</div>
        </div>
        <div class="card">
            <div class="label">Avg Quality</div>
            <div class="value {{ 'warn' if stats.avg_quality_score < 70 }} {{ 'danger' if stats.avg_quality_score < 50 }}">
                {{ "%.1f"|format(stats.avg_quality_score) }}
            </div>
        </div>
        <div class="card">
            <div class="label">Budget Remaining</div>
            <div class="value {{ 'warn' if stats.monthly_budget_remaining < 20 }} {{ 'danger' if stats.monthly_budget_remaining < 5 }}">
                ${{ "%.2f"|format(stats.monthly_budget_remaining) }}
            </div>
        </div>
    </div>

    <h2>Top Models</h2>
    <table>
        <tr><th>Model</th><th>Requests</th><th>Cost</th></tr>
        {% for m in stats.top_models %}
        <tr>
            <td><code>{{ m.model }}</code></td>
            <td>{{ m.requests }}</td>
            <td>${{ "%.4f"|format(m.cost) }}</td>
        </tr>
        {% endfor %}
    </table>

    <h2>Recent Requests</h2>
    <table>
        <tr><th>Time</th><th>Model</th><th>Provider</th><th>Tokens</th><th>Cost</th><th>Quality</th><th>Status</th></tr>
        for log in recent_logs
        <tr>
            <td>{{ log.created_at.strftime('%H:%M:%S') if log.created_at else '—' }}</td>
            <td><code>{{ log.model }}</code></td>
            <td>{{ log.provider }}</td>
            <td>{{ log.total_tokens }}</td>
            <td>${{ "%.6f"|format(log.cost_usd) }}</td>
            <td>{{ "%.1f"|format(log.quality_score) if log.quality_score else '—' }}</td>
            <td><span class="badge {{ log.status }}">{{ log.status }}</span></td>
        </tr>
        endfor
    </table>

    <h2>Budget Status</h2>
    <pre>{{ budget | tojson(indent=2) }}</pre>
</body>
</html>
"""
