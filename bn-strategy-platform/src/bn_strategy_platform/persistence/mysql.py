"""MariaDB storage compatible with the existing shared strategy tables."""

from __future__ import annotations

import json
import os
import time
from contextlib import contextmanager
from decimal import Decimal
from typing import Any, Iterator

from ..core.models import ClosedTrade, Position, Side, Signal, StrategyEvent


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS strategy_trades (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      trade_key VARCHAR(128) NOT NULL,
      strategy VARCHAR(96) NOT NULL,
      strategy_version VARCHAR(32) NOT NULL,
      mode VARCHAR(16) NOT NULL,
      entry_context VARCHAR(32) NOT NULL DEFAULT 'normal',
      symbol VARCHAR(32) NOT NULL,
      side VARCHAR(8) NOT NULL,
      leverage INT NOT NULL,
      status VARCHAR(16) NOT NULL,
      opened_at_ms BIGINT NOT NULL,
      closed_at_ms BIGINT NULL,
      entry_price DECIMAL(38,18) NOT NULL,
      exit_price DECIMAL(38,18) NULL,
      initial_quantity DECIMAL(38,18) NOT NULL,
      remaining_quantity DECIMAL(38,18) NOT NULL,
      initial_margin DECIMAL(38,18) NOT NULL,
      initial_stop_price DECIMAL(38,18) NOT NULL,
      initial_entry_price DECIMAL(38,18) NULL,
      initial_risk_distance DECIMAL(38,18) NULL,
      last_stop_price DECIMAL(38,18) NOT NULL,
      stop_reason VARCHAR(32) NOT NULL DEFAULT 'initial_stop',
      best_price DECIMAL(38,18) NOT NULL,
      worst_price DECIMAL(38,18) NULL,
      partial_tiers_done VARCHAR(128) NOT NULL DEFAULT '[]',
      scale_in_done TINYINT(1) NOT NULL DEFAULT 0,
      scale_in_quantity DECIMAL(38,18) NOT NULL DEFAULT 0,
      scale_in_price DECIMAL(38,18) NULL,
      close_reason VARCHAR(32) NULL,
      realized_gross_pnl DECIMAL(38,18) NULL,
      commission DECIMAL(38,18) NULL,
      funding DECIMAL(38,18) NULL,
      net_pnl DECIMAL(38,18) NULL,
      margin_roi DECIMAL(38,18) NULL,
      duration_seconds BIGINT NULL,
      created_at_ms BIGINT NOT NULL,
      updated_at_ms BIGINT NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uq_strategy_trade (strategy, trade_key),
      KEY idx_strategy_status (strategy, status),
      KEY idx_strategy_opened (strategy, opened_at_ms)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS strategy_entry_scores (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
      trade_key VARCHAR(128) NOT NULL,
      strategy VARCHAR(96) NOT NULL,
      strategy_version VARCHAR(32) NOT NULL,
      mode VARCHAR(16) NOT NULL,
      symbol VARCHAR(32) NOT NULL,
      side VARCHAR(8) NOT NULL,
      opened_at_ms BIGINT NOT NULL,
      score_model_version VARCHAR(64) NOT NULL,
      total_score DECIMAL(12,4) NOT NULL,
      grade VARCHAR(4) NOT NULL,
      breakdown_json JSON NOT NULL,
      metrics_json JSON NOT NULL,
      created_at_ms BIGINT NOT NULL,
      updated_at_ms BIGINT NOT NULL,
      PRIMARY KEY (id),
      UNIQUE KEY uq_entry_score (strategy, trade_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS strategy_signals (
      id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT PRIMARY KEY,
      strategy_name VARCHAR(96) NOT NULL,
      strategy_version VARCHAR(32) NOT NULL,
      run_mode VARCHAR(16) NOT NULL,
      symbol VARCHAR(32) NOT NULL,
      side VARCHAR(8) NOT NULL,
      signal_price DECIMAL(38,18) NOT NULL,
      stop_price DECIMAL(38,18) NOT NULL,
      score DECIMAL(12,4) NOT NULL,
      reason VARCHAR(128) NOT NULL,
      metrics JSON NOT NULL,
      observed_at_ms BIGINT NOT NULL,
      created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      KEY idx_signal_strategy_time (strategy_name, observed_at_ms),
      KEY idx_signal_symbol_time (symbol, observed_at_ms)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS platform_position_state (
      strategy VARCHAR(96) NOT NULL,
      trade_key VARCHAR(128) NOT NULL,
      stop_order_id VARCHAR(64) NULL,
      realized_pnl DECIMAL(38,18) NOT NULL DEFAULT 0,
      updated_at_ms BIGINT NOT NULL,
      PRIMARY KEY (strategy, trade_key)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
    """CREATE TABLE IF NOT EXISTS strategy_external_events (
      strategy_name VARCHAR(96) NOT NULL,
      strategy_version VARCHAR(32) NOT NULL,
      run_mode VARCHAR(16) NOT NULL,
      event_id VARCHAR(96) NOT NULL,
      event_kind VARCHAR(16) NOT NULL,
      symbol VARCHAR(32) NOT NULL,
      side VARCHAR(8) NOT NULL,
      status VARCHAR(32) NOT NULL,
      observed_at_ms BIGINT NOT NULL,
      created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
      PRIMARY KEY (strategy_name, run_mode, event_id),
      KEY idx_external_event_time (observed_at_ms)
    ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4""",
)


class MySqlTradeStore:
    def __init__(self, connection: Any) -> None:
        self.connection = connection
        with self._cursor() as cursor:
            for statement in SCHEMA:
                cursor.execute(statement)
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS entry_context VARCHAR(32)
                           NOT NULL DEFAULT 'normal' AFTER mode""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS partial_tiers_done VARCHAR(128)
                           NOT NULL DEFAULT '[]' AFTER best_price""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS stop_reason VARCHAR(32)
                           NOT NULL DEFAULT 'initial_stop' AFTER last_stop_price""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS worst_price DECIMAL(38,18)
                           NULL AFTER best_price""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS initial_entry_price DECIMAL(38,18)
                           NULL AFTER initial_stop_price""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS initial_risk_distance DECIMAL(38,18)
                           NULL AFTER initial_entry_price""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS scale_in_done TINYINT(1)
                           NOT NULL DEFAULT 0 AFTER partial_tiers_done""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS scale_in_quantity DECIMAL(38,18)
                           NOT NULL DEFAULT 0 AFTER scale_in_done""")
            cursor.execute("""ALTER TABLE strategy_trades
                           ADD COLUMN IF NOT EXISTS scale_in_price DECIMAL(38,18)
                           NULL AFTER scale_in_quantity""")
        connection.commit()

    @classmethod
    def from_environment(cls) -> "MySqlTradeStore":
        try:
            import pymysql
        except ImportError as exc:
            raise RuntimeError("PyMySQL is required for database_backend=mysql") from exc
        names = ("BN_TRADE_DB_HOST", "BN_TRADE_DB_NAME", "BN_TRADE_DB_USER", "BN_TRADE_DB_PASSWORD")
        missing = [name for name in names if not os.environ.get(name)]
        if missing:
            raise RuntimeError(f"missing MySQL environment variables: {', '.join(missing)}")
        return cls(pymysql.connect(
            host=os.environ["BN_TRADE_DB_HOST"],
            port=int(os.environ.get("BN_TRADE_DB_PORT", "3306")),
            database=os.environ["BN_TRADE_DB_NAME"],
            user=os.environ["BN_TRADE_DB_USER"],
            password=os.environ["BN_TRADE_DB_PASSWORD"],
            charset="utf8mb4", autocommit=False, connect_timeout=5, read_timeout=5, write_timeout=5,
            cursorclass=pymysql.cursors.DictCursor,
        ))

    def load_open_positions(self, strategy: str, mode: str) -> list[Position]:
        sql = """SELECT t.*, s.stop_order_id, COALESCE(s.realized_pnl, 0) platform_realized_pnl,
                        COALESCE(e.total_score, 0) entry_score
                 FROM strategy_trades t LEFT JOIN platform_position_state s
                   ON s.strategy=t.strategy AND s.trade_key=t.trade_key
                 LEFT JOIN strategy_entry_scores e
                   ON e.strategy=t.strategy AND e.trade_key=t.trade_key
                 WHERE t.strategy=%s AND t.mode=%s AND t.status='open'"""
        with self._cursor() as cursor:
            cursor.execute(sql, (strategy, self._trade_mode(mode)))
            rows = cursor.fetchall()
        return [self._position(row) for row in rows]

    def load_last_open_times(self, strategy: str, mode: str) -> dict[str, int]:
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT symbol, MAX(opened_at_ms) opened_at_ms
                   FROM strategy_trades WHERE strategy=%s AND mode=%s
                   GROUP BY symbol""",
                (strategy, self._trade_mode(mode)),
            )
            rows = cursor.fetchall()
        return {str(row["symbol"]): int(row["opened_at_ms"]) for row in rows}

    def load_last_open_times(self, strategy: str, mode: str) -> dict[str, int]:
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT symbol, MAX(opened_at_ms) opened_at_ms
                   FROM strategy_trades WHERE strategy=%s AND mode=%s GROUP BY symbol""",
                (strategy, self._trade_mode(mode)),
            )
            rows = cursor.fetchall()
        return {str(row["symbol"]): int(row["opened_at_ms"]) for row in rows}

    def save_signal(self, signal: Signal, mode: str) -> None:
        self._execute("""INSERT INTO strategy_signals
          (strategy_name,strategy_version,run_mode,symbol,side,signal_price,stop_price,score,reason,metrics,observed_at_ms)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
          (signal.strategy, signal.strategy_version, mode, signal.symbol, signal.side.value,
           signal.signal_price, signal.stop_price, signal.score, signal.reason,
           json.dumps(signal.metrics, ensure_ascii=True), signal.observed_at_ms))

    def save_open(self, position: Position, mode: str, score: Decimal, reason: str,
                  entry_context: str = "normal", metrics=None) -> None:
        now = int(time.time() * 1000)
        mode = self._trade_mode(mode)
        score_metrics = dict(metrics or {})
        score_model_version = str(score_metrics.get("score_model_version", "platform-entry-v1"))
        score_breakdown = score_metrics.get("score_breakdown", {"reason": reason})
        grade = "A" if score >= 85 else "B" if score >= 75 else "C" if score >= 65 else "D"
        try:
            with self._cursor() as cursor:
                cursor.execute("""INSERT INTO strategy_trades
                  (trade_key,strategy,strategy_version,mode,entry_context,symbol,side,leverage,status,opened_at_ms,
                   entry_price,initial_quantity,remaining_quantity,initial_margin,initial_stop_price,
                   initial_entry_price,initial_risk_distance,last_stop_price,stop_reason,best_price,
                   worst_price,partial_tiers_done,scale_in_done,scale_in_quantity,scale_in_price,
                   created_at_ms,updated_at_ms)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'open',
                          %s,%s,%s,%s,%s,%s,
                          %s,%s,%s,%s,%s,%s,
                          %s,%s,%s,%s,%s,%s)""",
                  (position.trade_id, position.strategy, position.strategy_version, mode, entry_context,
                   position.symbol,
                   position.side.value, position.leverage, position.opened_at_ms, position.entry_price,
                   position.quantity, position.remaining_quantity, position.margin,
                   position.initial_stop_price or position.stop_price,
                   position.initial_entry_price or position.entry_price,
                   position.initial_risk_distance,
                   position.stop_price, position.stop_reason, position.best_price,
                   position.worst_price or position.entry_price,
                   json.dumps(position.partial_tiers_done), position.scale_in_done,
                   position.scale_in_quantity, position.scale_in_price, now, now))
                cursor.execute("""INSERT INTO strategy_entry_scores
                  (trade_key,strategy,strategy_version,mode,symbol,side,opened_at_ms,score_model_version,
                   total_score,grade,breakdown_json,metrics_json,created_at_ms,updated_at_ms)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
                  (position.trade_id, position.strategy, position.strategy_version, mode, position.symbol,
                   position.side.value, position.opened_at_ms, score_model_version, score, grade,
                   json.dumps(score_breakdown, ensure_ascii=True),
                   json.dumps(score_metrics, ensure_ascii=True), now, now))
                self._upsert_state(cursor, position, now)
            self.connection.commit()
        except Exception:
            self._rollback_safely()
            raise

    def save_position(self, position: Position) -> None:
        now = int(time.time() * 1000)
        try:
            with self._cursor() as cursor:
                cursor.execute("""UPDATE strategy_trades SET entry_price=%s,initial_quantity=%s,
                               remaining_quantity=%s,initial_margin=%s,last_stop_price=%s,
                               stop_reason=%s,best_price=%s,worst_price=%s,
                               initial_entry_price=%s,initial_risk_distance=%s,
                               partial_tiers_done=%s,scale_in_done=%s,scale_in_quantity=%s,
                               scale_in_price=%s,updated_at_ms=%s
                               WHERE strategy=%s AND trade_key=%s""",
                               (position.entry_price, position.quantity,
                                position.remaining_quantity, position.margin,
                                position.stop_price, position.stop_reason,
                                position.best_price, position.worst_price or position.entry_price,
                                position.initial_entry_price or position.entry_price,
                                position.initial_risk_distance,
                                json.dumps(position.partial_tiers_done), position.scale_in_done,
                                position.scale_in_quantity, position.scale_in_price, now,
                                position.strategy, position.trade_id))
                self._upsert_state(cursor, position, now)
            self.connection.commit()
        except Exception:
            self._rollback_safely()
            raise

    def save_close(self, position: Position, exit_price: Decimal, net_pnl: Decimal, reason: str) -> None:
        now = int(time.time() * 1000)
        roi = net_pnl / position.margin * Decimal("100") if position.margin else Decimal("0")
        self._execute("""UPDATE strategy_trades SET status='closed',closed_at_ms=%s,exit_price=%s,
                      remaining_quantity=0,close_reason=%s,net_pnl=%s,margin_roi=%s,duration_seconds=%s,
                      updated_at_ms=%s WHERE strategy=%s AND trade_key=%s""",
                      (now, exit_price, reason, net_pnl, roi, max(0, (now - position.opened_at_ms) // 1000),
                       now, position.strategy, position.trade_id))

    def daily_net_pnl(self, strategy: str, mode: str, start_ms: int, end_ms: int) -> Decimal:
        mode = self._trade_mode(mode)
        clause = "" if strategy == "*" else "AND strategy=%s"
        params = (mode, start_ms, end_ms) if strategy == "*" else (mode, start_ms, end_ms, strategy)
        with self._cursor() as cursor:
            cursor.execute("""SELECT COALESCE(SUM(net_pnl),0) total FROM strategy_trades
                           WHERE status='closed' AND mode=%s AND closed_at_ms >= %s AND closed_at_ms < %s """ + clause,
                           params)
            return Decimal(str(cursor.fetchone()["total"]))

    def recent_fast_stop_candidates(
        self, source_strategies, start_ms: int, end_ms: int, max_duration_seconds: int,
    ) -> list[ClosedTrade]:
        if not source_strategies:
            return []
        placeholders = ",".join(["%s"] * len(source_strategies))
        sql = f"""SELECT trade_key,strategy,strategy_version,mode,entry_context,symbol,side,
                         opened_at_ms,closed_at_ms,entry_price,exit_price,initial_stop_price,
                         last_stop_price,net_pnl,duration_seconds,close_reason
                  FROM strategy_trades
                  WHERE strategy IN ({placeholders}) AND mode='real' AND entry_context='normal'
                    AND status='closed' AND closed_at_ms >= %s AND closed_at_ms < %s
                    AND duration_seconds <= %s AND net_pnl < 0
                    AND close_reason IN ('stop_loss','exchange_stop_reconciled')
                    AND last_stop_price = initial_stop_price
                    AND ((side='long' AND last_stop_price < entry_price)
                         OR (side='short' AND last_stop_price > entry_price))
                    AND ((side='long' AND exit_price <= last_stop_price)
                         OR (side='short' AND exit_price >= last_stop_price))
                  ORDER BY closed_at_ms"""
        params = (*source_strategies, start_ms, end_ms, max_duration_seconds)
        with self._cursor() as cursor:
            cursor.execute(sql, params)
            rows = cursor.fetchall()
        return [ClosedTrade(
            trade_key=str(row["trade_key"]), strategy=str(row["strategy"]),
            strategy_version=str(row["strategy_version"]), mode=str(row["mode"]),
            entry_context=str(row["entry_context"]), symbol=str(row["symbol"]),
            side=Side(str(row["side"])), opened_at_ms=int(row["opened_at_ms"]),
            closed_at_ms=int(row["closed_at_ms"]), entry_price=Decimal(row["entry_price"]),
            exit_price=Decimal(row["exit_price"]),
            initial_stop_price=Decimal(row["initial_stop_price"]),
            last_stop_price=Decimal(row["last_stop_price"]), net_pnl=Decimal(row["net_pnl"]),
            duration_seconds=int(row["duration_seconds"]), close_reason=str(row["close_reason"]),
        ) for row in rows]

    def last_closed_trade(
        self, strategy: str, mode: str, symbol: str,
    ) -> ClosedTrade | None:
        with self._cursor() as cursor:
            cursor.execute("""SELECT trade_key,strategy,strategy_version,mode,entry_context,
                           symbol,side,opened_at_ms,closed_at_ms,entry_price,exit_price,
                           initial_stop_price,last_stop_price,net_pnl,duration_seconds,close_reason
                           FROM strategy_trades
                           WHERE strategy=%s AND mode=%s AND symbol=%s AND entry_context='normal'
                             AND status='closed'
                           ORDER BY closed_at_ms DESC LIMIT 1""",
                           (strategy, self._trade_mode(mode), symbol))
            row = cursor.fetchone()
        if row is None:
            return None
        return ClosedTrade(
            trade_key=str(row["trade_key"]), strategy=str(row["strategy"]),
            strategy_version=str(row["strategy_version"]), mode=str(row["mode"]),
            entry_context=str(row["entry_context"]), symbol=str(row["symbol"]),
            side=Side(str(row["side"])), opened_at_ms=int(row["opened_at_ms"]),
            closed_at_ms=int(row["closed_at_ms"]), entry_price=Decimal(row["entry_price"]),
            exit_price=Decimal(row["exit_price"]),
            initial_stop_price=Decimal(row["initial_stop_price"]),
            last_stop_price=Decimal(row["last_stop_price"]), net_pnl=Decimal(row["net_pnl"]),
            duration_seconds=int(row["duration_seconds"]), close_reason=str(row["close_reason"]),
        )

    def count_stop_losses(
        self, strategy: str, mode: str, symbol: str, side: Side,
        start_ms: int, end_ms: int,
    ) -> int:
        with self._cursor() as cursor:
            cursor.execute("""SELECT COUNT(*) total FROM strategy_trades
                           WHERE strategy=%s AND mode=%s AND symbol=%s AND side=%s
                             AND entry_context='normal' AND status='closed'
                             AND close_reason='stop_loss'
                             AND closed_at_ms >= %s AND closed_at_ms < %s""",
                           (strategy, self._trade_mode(mode), symbol, side.value,
                            start_ms, end_ms))
            row = cursor.fetchone()
        return int(row["total"])

    def has_losing_trade(
        self, strategy: str, mode: str, symbol: str, start_ms: int, end_ms: int,
    ) -> bool:
        with self._cursor() as cursor:
            cursor.execute(
                """SELECT 1 FROM strategy_trades
                   WHERE strategy=%s AND mode=%s AND symbol=%s AND status='closed'
                     AND closed_at_ms >= %s AND closed_at_ms < %s AND net_pnl < 0
                   LIMIT 1""",
                (strategy, self._trade_mode(mode), symbol, start_ms, end_ms),
            )
            return cursor.fetchone() is not None

    def event_seen(self, event_id: str, strategy: str, mode: str) -> bool:
        with self._cursor() as cursor:
            cursor.execute("""SELECT status FROM strategy_external_events
                           WHERE strategy_name=%s AND run_mode=%s AND event_id=%s""",
                           (strategy, mode, event_id))
            row = cursor.fetchone()
        return bool(row and row["status"] == "processed")

    def save_event(self, event: StrategyEvent, status: str, mode: str, version: str) -> None:
        self._execute("""INSERT INTO strategy_external_events
          (strategy_name,strategy_version,run_mode,event_id,event_kind,symbol,side,status,observed_at_ms)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
          ON DUPLICATE KEY UPDATE status=VALUES(status)""",
          (event.strategy, version, mode, event.event_id, event.kind, event.symbol,
           event.side.value, status, event.observed_at_ms))

    def _upsert_state(self, cursor: Any, position: Position, now: int) -> None:
        cursor.execute("""INSERT INTO platform_position_state
          (strategy,trade_key,stop_order_id,realized_pnl,updated_at_ms) VALUES (%s,%s,%s,%s,%s)
          ON DUPLICATE KEY UPDATE stop_order_id=VALUES(stop_order_id),
          realized_pnl=VALUES(realized_pnl),updated_at_ms=VALUES(updated_at_ms)""",
          (position.strategy, position.trade_id, position.stop_order_id, position.realized_pnl, now))

    @staticmethod
    def _trade_mode(mode: str) -> str:
        return "real" if mode == "live" else mode

    def _execute(self, sql: str, values: tuple[Any, ...]) -> None:
        try:
            with self._cursor() as cursor:
                cursor.execute(sql, values)
            self.connection.commit()
        except Exception:
            self._rollback_safely()
            raise

    @contextmanager
    def _cursor(self) -> Iterator[Any]:
        self.connection.ping(reconnect=True)
        with self.connection.cursor() as cursor:
            yield cursor

    def _rollback_safely(self) -> None:
        try:
            self.connection.rollback()
        except Exception:
            pass

    @staticmethod
    def _position(row: dict[str, Any]) -> Position:
        return Position(
            trade_id=row["trade_key"], strategy=row["strategy"], strategy_version=row["strategy_version"],
            symbol=row["symbol"], side=Side(row["side"]), entry_price=Decimal(row["entry_price"]),
            quantity=Decimal(row["initial_quantity"]), remaining_quantity=Decimal(row["remaining_quantity"]),
            margin=Decimal(row["initial_margin"]), leverage=int(row["leverage"]),
            stop_price=Decimal(row["last_stop_price"]), opened_at_ms=int(row["opened_at_ms"]),
            best_price=Decimal(row["best_price"]),
            worst_price=Decimal(row.get("worst_price") or row["entry_price"]),
            initial_stop_price=Decimal(row["initial_stop_price"]),
            initial_entry_price=Decimal(row.get("initial_entry_price") or row["entry_price"]),
            initial_risk_distance=Decimal(
                row.get("initial_risk_distance")
                or abs(Decimal(row["entry_price"]) - Decimal(row["initial_stop_price"]))
            ),
            scale_in_done=bool(row.get("scale_in_done")),
            scale_in_quantity=Decimal(row.get("scale_in_quantity") or "0"),
            scale_in_price=(Decimal(row["scale_in_price"])
                            if row.get("scale_in_price") is not None else None),
            realized_pnl=Decimal(row["platform_realized_pnl"]),
            partial_tiers_done=tuple(int(item) for item in json.loads(
                row.get("partial_tiers_done") or "[]"
            )),
            stop_order_id=row.get("stop_order_id"),
            stop_reason=str(row.get("stop_reason") or "initial_stop"),
            entry_score=Decimal(str(row.get("entry_score") or "0")),
        )

    def close(self) -> None:
        self.connection.close()
