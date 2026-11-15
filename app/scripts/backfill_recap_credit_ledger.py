"""Idempotent one-time backfill for RecapCreditLedgerEvent opening balances.

Usage:
    python -m app.scripts.backfill_recap_credit_ledger --dry-run
    python -m app.scripts.backfill_recap_credit_ledger
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db.session import AsyncSessionLocal
from app.services.ledger import backfill_recap_credit_opening_balances


async def run_backfill(
    session_maker: async_sessionmaker[AsyncSession] = AsyncSessionLocal,
    *,
    dry_run: bool = False,
) -> dict[str, int]:
    """Print dry-run counts first; apply opening CREDIT rows unless dry_run."""
    async with session_maker() as session:
        preview = await backfill_recap_credit_opening_balances(session, dry_run=True)
        print(
            "[backfill-recap-ledger] dry-run counts: "
            f"users_scanned={preview['users_scanned']} "
            f"already_balanced={preview['users_already_balanced']} "
            f"needing_backfill={preview['users_needing_backfill']} "
            f"total_shortfall_credits={preview['total_shortfall_credits']} "
            f"negative_shortfall_users={preview['negative_shortfall_users']}"
        )
        if dry_run:
            return preview

        result = await backfill_recap_credit_opening_balances(session, dry_run=False)
        await session.commit()
        print(
            "[backfill-recap-ledger] applied: "
            f"users_backfilled={result['users_backfilled']} "
            f"credits_written={result['credits_written']}"
        )
        return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Backfill RecapCreditLedgerEvent opening balance CREDIT rows."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print counts without writing any RecapCreditLedgerEvent rows.",
    )
    args = parser.parse_args(argv)
    if sys.platform == "win32":
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except Exception:
            pass
    asyncio.run(run_backfill(dry_run=args.dry_run))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
