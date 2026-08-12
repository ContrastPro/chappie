from app.infrastructure.binance import OrderIdentity
from app.persistence.exchange import SQLiteExchangePersistence


class SQLiteOrderIdentityResolver:
    """Resolve Binance response identity only from already-durable commands."""

    def __init__(self, persistence: SQLiteExchangePersistence) -> None:
        self._persistence = persistence

    def by_client_order_id(self, client_order_id: str) -> OrderIdentity | None:
        row = self._persistence._connection.execute(
            "SELECT durable_command_key,client_order_id,side,original_quantity,created_at_ms "
            "FROM exchange_orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        return self._identity(row)

    def by_exchange_order_id(self, exchange_order_id: str) -> OrderIdentity | None:
        row = self._persistence._connection.execute(
            "SELECT durable_command_key,client_order_id,side,original_quantity,created_at_ms "
            "FROM exchange_orders WHERE exchange_order_id=?",
            (exchange_order_id,),
        ).fetchone()
        return self._identity(row)

    @staticmethod
    def _identity(row) -> OrderIdentity | None:
        if row is None:
            return None
        from app.exchange_execution import ExchangeOrderSide
        from app.persistence.canonical import deserialize_decimal

        return OrderIdentity(
            row["durable_command_key"],
            row["client_order_id"],
            ExchangeOrderSide(row["side"]),
            deserialize_decimal(row["original_quantity"]),
            row["created_at_ms"],
        )
