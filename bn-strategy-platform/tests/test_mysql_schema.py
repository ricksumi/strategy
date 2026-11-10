import unittest
from decimal import Decimal

from bn_strategy_platform.core.models import Position, Side, StrategyEvent
from bn_strategy_platform.persistence.mysql import MySqlTradeStore, SCHEMA


class MySqlSchemaTests(unittest.TestCase):
    def test_event_kind_migration_only_runs_for_a_narrow_column(self) -> None:
        for width, should_alter in ((16, True), (32, False), (64, False)):
            with self.subTest(width=width):
                statements = []

                class Cursor:
                    def __enter__(self):
                        return self

                    def __exit__(self, *_args):
                        pass

                    def execute(self, sql):
                        statements.append(sql)

                    def fetchone(self):
                        return {"width": width}

                class Connection:
                    def ping(self, reconnect=False):
                        pass

                    def cursor(self):
                        return Cursor()

                    def commit(self):
                        pass

                MySqlTradeStore(Connection())
                altered = any("ALTER TABLE strategy_external_events" in sql for sql in statements)
                self.assertEqual(altered, should_alter)

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
        self.assertIn("event_kind VARCHAR(32)", event_schema)

    def test_lead_order_audit_keeps_first_seen_immutable_and_preserves_metrics(self) -> None:
        store = object.__new__(MySqlTradeStore)
        statements = []
        store._execute = lambda sql, values: statements.append((sql, values))
        event = StrategyEvent("id", "copy-lead", "open", "TESTUSDT", Side.LONG, 1_000)
        store.observe_lead_order("copy-lead", "2.5.0", "paper", "123", event,
                                 {"orderTime": 900}, 155_000)
        sql, values = statements[-1]
        self.assertEqual(sql.count("%s"), len(values))
        self.assertNotIn("first_seen_at_ms=", sql.split("ON DUPLICATE KEY UPDATE")[1])
        self.assertEqual(values[12], 154_000)
        store.decide_lead_order("copy-lead", "paper", "123", "id", "opened", "", None)
        sql, values = statements[-1]
        self.assertEqual(sql.count("%s"), len(values))
        self.assertIn("COALESCE(%s,metrics_json)", sql)
        self.assertIsNone(values[2])

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
