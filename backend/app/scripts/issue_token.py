"""Issue a dashboard API token for an agent.

    make token EMAIL=demo.agent@sunrisehomes.example

The token is shown once and stored only as a SHA-256 hash — re-running this
replaces the previous token, which is also how you revoke one.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.api.auth import generate_token, hash_token
from app.core.db import dispose_engine, get_sessionmaker
from app.core.logging import configure_logging
from app.models import Agent


async def issue(email: str | None) -> int:
    async with get_sessionmaker()() as session:
        query = select(Agent).order_by(Agent.created_at)
        if email:
            query = query.where(Agent.email == email)
        agent = (await session.execute(query.limit(1))).scalar_one_or_none()

        if agent is None:
            print(
                f"No agent found{f' for {email}' if email else ''}. "
                "Run `make migrate && make seed` first.",
                file=sys.stderr,
            )
            return 1

        token = generate_token()
        replacing = agent.api_token_hash is not None
        agent.api_token_hash = hash_token(token)
        await session.commit()

        print(f"\nAgent:  {agent.name} <{agent.email}>")
        if replacing:
            print("Note:   the previous token for this agent is now revoked.")
        print(f"\n  {token}\n")
        print("Store it now — it is not recoverable. Use it as:")
        print(f'  curl -H "Authorization: Bearer {token}" http://localhost:8000/api/leads\n')
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a dashboard API token.")
    parser.add_argument("--email", default=None, help="Agent email (defaults to the first agent).")
    args = parser.parse_args()
    configure_logging("WARNING")

    async def _run() -> int:
        try:
            return await issue(args.email)
        finally:
            await dispose_engine()

    raise SystemExit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
