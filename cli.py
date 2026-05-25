"""CLI tool for managing AI Guardian."""
import asyncio
import json
import os
import sys

import httpx
from typer import Typer, Option, Argument
from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich import print as rprint

app = Typer(name="guardian", help="AI Guardian CLI — manage users, budgets, and inspect usage")
console = Console()

# Default to localhost:9191 if not set
SERVER = os.getenv("GUARDIAN_SERVER", "http://localhost:9191")


@app.command()
def register(
    user_id: str = Argument(..., help="Unique user identifier"),
    provider: str = Option("anthropic", help="AI provider (openai, anthropic, google, deepseek)"),
    api_key: str = Option(..., help="Your API key for this provider", prompt=True, hide_input=True),
    monthly_budget: float = Option(100.0, help="Monthly budget in USD"),
    daily_budget: float = Option(10.0, help="Daily budget in USD"),
):
    """Register a user with their AI provider API key."""
    asyncio.run(_register(user_id, provider, api_key, monthly_budget, daily_budget))


async def _register(user_id, provider, api_key, monthly_budget, daily_budget):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{SERVER}/guardian/users", json={
            "user_id": user_id,
            "provider": provider,
            "api_key": api_key,
            "monthly_budget": monthly_budget,
            "daily_budget": daily_budget,
        })
        if resp.status_code == 200:
            data = resp.json()
            rprint(Panel(
                f"User [bold]{data['user_id']}[/] registered\n"
                f"Provider: {data['provider']}\n"
                f"Monthly budget: ${data['monthly_budget']}\n"
                f"Daily budget: ${data['daily_budget']}",
                title="✅ Success",
                border_style="green",
            ))
        else:
            console.print(f"[red]Error:[/] {resp.text}")
            sys.exit(1)


@app.command()
def budget(
    user_id: str = Argument(..., help="User identifier"),
    monthly: float = Option(None, help="Set monthly budget in USD"),
    daily: float = Option(None, help="Set daily budget in USD"),
    hard_cap: bool = Option(None, help="Enable/disable hard cap (true/false)"),
):
    """View or update budget for a user."""
    if monthly is None and daily is None and hard_cap is None:
        asyncio.run(_show_budget(user_id))
    else:
        asyncio.run(_update_budget(user_id, monthly, daily, hard_cap))


async def _show_budget(user_id):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER}/guardian/budget/{user_id}")
        if resp.status_code == 200:
            b = resp.json()
            status_color = {"ok": "green", "warning": "yellow", "exceeded": "red"}.get(b['status'], "white")
            table = Table(title=f"Budget — {user_id}")
            table.add_column("Metric", style="bold")
            table.add_column("Value")
            table.add_row("Status", f"[{status_color}]{b['status']}[/]")
            table.add_row("Daily spent", f"${b['daily_spent']:.4f}")
            table.add_row("Daily budget", f"${b['daily_budget']:.2f}")
            table.add_row("Monthly spent", f"${b['monthly_spent']:.4f}")
            table.add_row("Monthly budget", f"${b['monthly_budget']:.2f}")
            table.add_row("Would exceed", "Yes ⚠️" if b['would_exceed'] else "No ✅")
            console.print(table)
        else:
            console.print(f"[red]Error:[/] {resp.text}")
            sys.exit(1)


async def _update_budget(user_id, monthly, daily, hard_cap):
    payload = {}
    if monthly is not None:
        payload["monthly_budget"] = monthly
    if daily is not None:
        payload["daily_budget"] = daily
    if hard_cap is not None:
        payload["hard_cap"] = hard_cap

    async with httpx.AsyncClient() as client:
        resp = await client.put(f"{SERVER}/guardian/users/{user_id}/budget", json=payload)
        if resp.status_code == 200:
            data = resp.json()
            rprint(Panel(
                f"Budget updated for [bold]{data['user_id']}[/]\n"
                f"Monthly: ${data['monthly_budget']}\n"
                f"Daily: ${data['daily_budget']}\n"
                f"Hard cap: {data['hard_cap']}",
                title="✅ Updated",
                border_style="green",
            ))
        else:
            console.print(f"[red]Error:[/] {resp.text}")
            sys.exit(1)


@app.command()
def stats(
    user_id: str = Argument(..., help="User identifier"),
):
    """Show usage statistics for a user."""
    asyncio.run(_show_stats(user_id))


async def _show_stats(user_id):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER}/guardian/stats/{user_id}")
        if resp.status_code == 200:
            s = resp.json()
            table = Table(title=f"Usage Stats — {user_id}")
            table.add_column("Metric", style="bold")
            table.add_column("Value")
            table.add_row("Total requests", str(s['total_requests']))
            table.add_row("Total cost", f"${s['total_cost_usd']:.4f}")
            table.add_row("Total tokens", f"{s['total_tokens']:,}")
            table.add_row("Avg quality", f"{s['avg_quality_score']:.1f}/100")
            table.add_row("Budget remaining", f"${s['monthly_budget_remaining']:.2f}")
            console.print(table)

            if s.get('top_models'):
                t2 = Table(title="Top Models")
                t2.add_column("Model")
                t2.add_column("Requests", justify="right")
                t2.add_column("Cost", justify="right")
                for m in s['top_models']:
                    t2.add_row(m['model'], str(m['requests']), f"${m['cost']:.4f}")
                console.print(t2)
        else:
            console.print(f"[red]Error:[/] {resp.text}")
            sys.exit(1)


@app.command()
def keys(
    user_id: str = Argument(..., help="User identifier"),
    delete: str = Option(None, help="Delete a stored key for this provider"),
):
    """List or delete stored API keys."""
    if delete:
        asyncio.run(_delete_key(user_id, delete))
    else:
        asyncio.run(_list_keys(user_id))


async def _list_keys(user_id):
    async with httpx.AsyncClient() as client:
        resp = await client.get(f"{SERVER}/guardian/users/{user_id}/keys")
        if resp.status_code == 200:
            data = resp.json()
            if data['providers']:
                table = Table(title=f"Stored Keys — {user_id}")
                table.add_column("Provider", style="bold")
                table.add_column("Added")
                for p in data['providers']:
                    table.add_row(p['provider'], p.get('created_at', 'unknown'))
                console.print(table)
            else:
                console.print("[yellow]No stored keys[/]")
        else:
            console.print(f"[red]Error:[/] {resp.text}")


async def _delete_key(user_id, provider):
    async with httpx.AsyncClient() as client:
        resp = await client.delete(f"{SERVER}/guardian/users/{user_id}/keys/{provider}")
        if resp.status_code == 200:
            console.print(f"[green]✅[/] Deleted {provider} key for {user_id}")
        else:
            console.print(f"[red]Error:[/] {resp.text}")
            sys.exit(1)


@app.command()
def serve(
    host: str = Option("0.0.0.0", help="Bind host"),
    port: int = Option(9191, help="Bind port"),
):
    """Start the Guardian proxy server."""
    import uvicorn
    from guardian.proxy.server import app as fastapi_app
    console.print(f"[bold green]🛡️ AI Guardian[/] starting on {host}:{port}")
    uvicorn.run(fastapi_app, host=host, port=port, log_level="info")


@app.command()
def test(
    user_id: str = Option("test-user", help="User ID for the test"),
    message: str = Option("Hello, write a simple Python function", help="Test message"),
    model: str = Option("anthropic/claude-sonnet-4", help="Model to use"),
):
    """Send a test request through Guardian."""
    asyncio.run(_test_request(user_id, message, model))


async def _test_request(user_id, message, model):
    async with httpx.AsyncClient() as client:
        resp = await client.post(f"{SERVER}/v1/chat/completions", json={
            "model": model,
            "messages": [{"role": "user", "content": message}],
            "max_tokens": 500,
        }, headers={
            "x-guardian-user": user_id,
        }, timeout=30)

        if resp.status_code == 200:
            data = resp.json()
            guardian_meta = data.get('guardian', {})

            rprint(Panel(
                f"Model used: {data.get('model')}\n"
                f"Cost: ${guardian_meta.get('cost_usd', 0):.6f}\n"
                f"Quality: {guardian_meta.get('quality', {}).get('score', 'N/A')}\n"
                f"Budget status: {guardian_meta.get('budget_status', 'unknown')}",
                title="🛡️ Guardian Response",
                border_style="green",
            ))

            content = data.get('choices', [{}])[0].get('message', {}).get('content', '')
            if content:
                console.print(f"\n[bold]AI Response:[/]\n{content[:500]}")

            if guardian_meta.get('routed'):
                r = guardian_meta['routed']
                console.print(f"\n[yellow]⚠️ Model routed:[/] {r['original_model']} → {r['routed_model']} ({r['reason']})")

            if guardian_meta.get('warnings'):
                for w in guardian_meta['warnings']:
                    console.print(f"[yellow]⚠️ {w}[/]")
        else:
            console.print(f"[red]Error ({resp.status_code}):[/] {resp.text}")
            sys.exit(1)


if __name__ == "__main__":
    app()
