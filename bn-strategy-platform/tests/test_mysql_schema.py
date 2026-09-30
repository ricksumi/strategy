import unittest
from decimal import Decimal

from bn_strategy_platform.core.models import Position, Side
from bn_strategy_platform.persistence.mysql import MySqlTradeStore, SCHEMA


class MySqlSchemaTests(unittest.TestCase):
    def test_trade_table_remains_compatible_with_existing_database(self) -> None:
        trade_schema = SCHEMA[0]
        for column in ("trade_key", "strategy", "strategy_version", "mode",
                       "entry_context", "initial_quantity", "initial_margin",
                       "last_stop_price", "stop_reason", "best_price", "worst_price",
                       "partial_tiers_done", "initial_entry_price",
                       "initial_risk_distance", "scale_in_done",
                       "scale_in_quantity", "scale_in_price"):
            self.assertIn(column, trade_schema)
        self.assertIn("platform_position_state", "\n".join(SCHEMA))
        self.assertEqual(MySqlTradeStore._trade_mode("live"), "real")

    def test_external_event_identity_includes_run_mode(self) -> None:
        event_schema = next(item for item in SCHEMA if "strategy_external_events" in item)
        self.assertIn("PRIMARY KEY (strategy_name, run_mode, event_id)", event_schema)

    def test_cursor_pings_with_reconnect_before_use(self) -> None:
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

        class Connection:
            def __init__(self) -> None:
                self.reconnect = None

            def ping(self, reconnect=False) -> None:
                self.reconnect = reconnect

            def cursor(self):
                return Cursor()

        store = object.__new__(MySqlTradeStore)
        store.connection = Connection()
        with store._cursor() as cursor:
            self.assertIsInstance(cursor, Cursor)
        self.assertTrue(store.connection.reconnect)

    def test_failed_rollback_does_not_hide_original_database_error(self) -> None:
        class Connection:
            def rollback(self) -> None:
                raise RuntimeError("connection already closed")

        store = object.__new__(MySqlTradeStore)
        store.connection = Connection()
        store._rollback_safely()

    def test_save_open_sql_placeholder_count_matches_parameters(self) -> None:
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return None

            def execute(self, sql, params=None):
                if params is not None and sql.count("%s") != len(params):
                    raise AssertionError(
                        f"placeholder mismatch: {sql.count('%s')} != {len(params)}"
                    )

        class Connection:
            def ping(self, reconnect=False):
                return None

            def cursor(self):
                return Cursor()

            def commit(self):
                return None

            def rollback(self):
                return None

        store = object.__new__(MySqlTradeStore)
        store.connection = Connection()
        position = Position(
            "trade", "strategy", "1.0.0", "TESTUSDT", Side.LONG,
            Decimal("1"), Decimal("10"), Decimal("10"), Decimal("2"), 5,
            Decimal("0.98"), 1, Decimal("1"), initial_stop_price=Decimal("0.98"),
        )

        store.save_open(position, "live", Decimal("70"), "test")
