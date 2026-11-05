"""Batch AJ / L17 (FAQ slice) — GroupAiFaq cache must be capped.

Approved answers previously accumulated forever. Pins: inserting
beyond FAQ_MAX_ROWS prunes the oldest rows, and the standalone prune
helper enforces the same cap.
"""

import pytest
from sqlalchemy import func, select

from app.models.schema import GroupAiFaq
from app.services import group_ai_admin as gai


@pytest.mark.asyncio
async def test_faq_cache_is_capped_on_insert(db_session):
    total = gai.FAQ_MAX_ROWS + 5
    for i in range(total):
        rid = await gai.save_faq_pair(
            db_session,
            question_text=f"မေးခွန်း နံပါတ် {i} သီးသန့်",
            answer_text=f"အဖြေ {i}",
        )
        assert rid is not None
    count = (
        await db_session.execute(select(func.count()).select_from(GroupAiFaq))
    ).scalar_one()
    assert count == gai.FAQ_MAX_ROWS
    rows = (await db_session.execute(select(GroupAiFaq))).scalars().all()
    answers = {r.answer_text for r in rows}
    assert "အဖြေ 0" not in answers  # oldest pruned
    assert f"အဖြေ {total - 1}" in answers  # newest kept


@pytest.mark.asyncio
async def test_prune_faq_cache_helper(db_session):
    for i in range(10):
        db_session.add(
            GroupAiFaq(
                question_norm=f"q{i} သီးသန့်",
                question_text=f"q{i}",
                answer_text=f"a{i}",
            )
        )
    await db_session.commit()
    removed = await gai.prune_faq_cache(db_session, max_rows=3)
    assert removed == 7
    count = (
        await db_session.execute(select(func.count()).select_from(GroupAiFaq))
    ).scalar_one()
    assert count == 3
    rows = (await db_session.execute(select(GroupAiFaq))).scalars().all()
    assert {r.answer_text for r in rows} == {"a7", "a8", "a9"}
