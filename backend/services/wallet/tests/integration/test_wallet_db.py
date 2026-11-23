"""End-to-end Wallet behaviour against PostgreSQL."""

from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.errors import AppError
from app.models.wallet import Wallet
from app.repositories.wallet_repository import WalletRepository
from app.services.wallet_service import WalletService

pytestmark = pytest.mark.integration

USER_ID = uuid.UUID("55555555-5555-4555-8555-555555555555")
OTHER_USER_ID = uuid.UUID("66666666-6666-4666-8666-666666666666")
HEADERS = {"X-User-Id": str(USER_ID)}
OTHER_HEADERS = {"X-User-Id": str(OTHER_USER_ID)}


async def test_migration_head_is_applied(db_session: AsyncSession) -> None:
    version = await db_session.scalar(text("SELECT version_num FROM alembic_version"))
    assert version == "0002_create_wallets"


async def test_user_id_is_unique(db_session: AsyncSession) -> None:
    db_session.add(Wallet(user_id=USER_ID))
    await db_session.flush()
    db_session.add(Wallet(user_id=USER_ID))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_lazy_creation_has_zero_inr_balance(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    first = await db_client.get("/me", headers=HEADERS)
    assert first.status_code == 200
    assert first.json()["balance"] == "0.00"
    assert first.json()["currency"] == "INR"

    second = await db_client.get("/me", headers=HEADERS)
    assert second.json()["id"] == first.json()["id"]
    assert len((await db_session.execute(select(Wallet))).scalars().all()) == 1


async def test_deposit_and_withdraw_are_atomic(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_client.get("/me", headers=HEADERS)

    deposited = await db_client.post("/deposit", json={"amount": "500.00"}, headers=HEADERS)
    assert deposited.status_code == 200
    assert deposited.json()["balance"] == "500.00"

    withdrawn = await db_client.post("/withdraw", json={"amount": "125.50"}, headers=HEADERS)
    assert withdrawn.status_code == 200
    assert withdrawn.json()["balance"] == "374.50"

    stored = (await db_session.execute(select(Wallet))).scalar_one()
    assert stored.balance == Decimal("374.50")
    assert stored.currency == "INR"


async def test_insufficient_funds_does_not_change_balance(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    await db_client.get("/me", headers=HEADERS)
    await db_client.post("/deposit", json={"amount": "100.00"}, headers=HEADERS)

    response = await db_client.post("/withdraw", json={"amount": "100.01"}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "INSUFFICIENT_FUNDS"
    stored = (await db_session.execute(select(Wallet))).scalar_one()
    assert stored.balance == Decimal("100.00")


async def test_ownership_isolation(
    db_client: AsyncClient, db_session: AsyncSession
) -> None:
    mine = await db_client.get("/me", headers=HEADERS)
    theirs = await db_client.get("/me", headers=OTHER_HEADERS)
    assert mine.json()["id"] != theirs.json()["id"]

    await db_client.post("/deposit", json={"amount": "250.00"}, headers=HEADERS)

    other = (
        await db_session.execute(select(Wallet).where(Wallet.user_id == OTHER_USER_ID))
    ).scalar_one()
    assert other.balance == Decimal("0.00")
    assert other.currency == "INR"


async def test_database_rejects_negative_balance(db_session: AsyncSession) -> None:
    wallet = Wallet(user_id=USER_ID, balance=Decimal("10.00"), currency="INR")
    db_session.add(wallet)
    await db_session.flush()

    wallet.balance = Decimal("-0.01")
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_rollback_leaves_balance_unchanged(test_dsn: str) -> None:
    """An exception inside a transaction must roll the UPDATE back."""
    user_id = uuid.uuid4()
    engine = create_async_engine(test_dsn, poolclass=NullPool)
    try:
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO wallets (user_id, balance, currency) "
                    "VALUES (:uid, 50.00, 'INR')"
                ),
                {"uid": user_id},
            )

        with pytest.raises(RuntimeError):
            async with engine.begin() as connection:
                await connection.execute(
                    text("UPDATE wallets SET balance = 0 WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                raise RuntimeError("simulate a failure after the write")

        async with engine.connect() as connection:
            balance = await connection.scalar(
                text("SELECT balance FROM wallets WHERE user_id = :uid"), {"uid": user_id}
            )
            assert balance == Decimal("50.00")
    finally:
        async with engine.begin() as connection:
            await connection.execute(
                text("DELETE FROM wallets WHERE user_id = :uid"), {"uid": user_id}
            )
        await engine.dispose()


async def test_concurrent_lazy_creation_is_idempotent(test_dsn: str) -> None:
    user_id = uuid.uuid4()

    async def create_once() -> Wallet:
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                sessionmaker = async_sessionmaker(bind=connection, expire_on_commit=False)
                async with sessionmaker() as session:
                    created = await WalletRepository(session).get_or_create(user_id)
                    assert created is not None
                    await session.commit()
                    return created
        finally:
            await engine.dispose()

    try:
        results = await asyncio.gather(create_once(), create_once())
        assert results[0].id == results[1].id
        assert all(result.balance == Decimal("0.00") for result in results)
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                count = await connection.scalar(
                    text("SELECT count(*) FROM wallets WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                assert count == 1
        finally:
            await engine.dispose()
    finally:
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM wallets WHERE user_id = :uid"), {"uid": user_id}
                )
        finally:
            await engine.dispose()


async def test_concurrent_withdrawals_cannot_overspend(test_dsn: str) -> None:
    """Two 100.00 withdrawals from a 100.00 wallet: exactly one must succeed."""
    user_id = uuid.uuid4()

    async def withdraw_once() -> str:
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                sessionmaker = async_sessionmaker(bind=connection, expire_on_commit=False)
                async with sessionmaker() as session:
                    try:
                        await WalletService(session).withdraw(user_id, Decimal("100.00"))
                        return "ok"
                    except AppError as exc:
                        return exc.code
        finally:
            await engine.dispose()

    try:
        # One wallet, one deposit: both racers see the same 100.00 starting balance.
        setup_engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with setup_engine.begin() as connection:
                sessionmaker = async_sessionmaker(bind=connection, expire_on_commit=False)
                async with sessionmaker() as session:
                    await WalletService(session).deposit(user_id, Decimal("100.00"))
        finally:
            await setup_engine.dispose()

        outcomes = await asyncio.gather(withdraw_once(), withdraw_once())
        assert sorted(outcomes) == ["INSUFFICIENT_FUNDS", "ok"]

        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.connect() as connection:
                balance = await connection.scalar(
                    text("SELECT balance FROM wallets WHERE user_id = :uid"),
                    {"uid": user_id},
                )
                assert balance == Decimal("0.00")
        finally:
            await engine.dispose()
    finally:
        engine = create_async_engine(test_dsn, poolclass=NullPool)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("DELETE FROM wallets WHERE user_id = :uid"), {"uid": user_id}
                )
        finally:
            await engine.dispose()
