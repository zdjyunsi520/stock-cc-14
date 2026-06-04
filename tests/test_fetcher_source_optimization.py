# -*- coding: utf-8 -*-
"""Regression tests for fetcher routing and optional-source pruning."""

import sys
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

if "litellm" not in sys.modules:
    sys.modules["litellm"] = MagicMock()
if "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.base import DataFetcherManager
from data_provider.realtime_types import RealtimeSource, UnifiedRealtimeQuote
from src.storage import DatabaseManager, StockDaily


class _StubFetcher:
    def __init__(self, name: str, priority: int):
        self.name = name
        self.priority = priority


def _make_quote(code: str = "AAPL") -> UnifiedRealtimeQuote:
    return UnifiedRealtimeQuote(
        code=code,
        name="Apple",
        source=RealtimeSource.FALLBACK,
        price=188.8,
        change_pct=1.2,
        volume_ratio=1.0,
        turnover_rate=0.2,
        pe_ratio=20.0,
        pb_ratio=3.0,
        total_mv=1000.0,
        circ_mv=900.0,
        amplitude=2.0,
    )


def _make_daily_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "date": "2026-05-01",
                "open": 100.0,
                "high": 102.0,
                "low": 99.0,
                "close": 101.0,
                "volume": 1000,
                "amount": 101000.0,
                "pct_chg": 1.0,
            }
        ]
    )


class TestFetcherSourceOptimization(unittest.TestCase):
    @patch("src.config.get_config")
    def test_manager_skips_unconfigured_optional_fetchers(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            tushare_token="",
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
            longbridge_oauth_client_id="",
        )

        with patch.dict(
            "os.environ",
            {
                "LONGBRIDGE_OAUTH_CLIENT_ID": "",
                "LONGBRIDGE_APP_KEY": "",
                "LONGBRIDGE_APP_SECRET": "",
                "LONGBRIDGE_ACCESS_TOKEN": "",
            },
        ), patch("data_provider.efinance_fetcher.EfinanceFetcher", return_value=_StubFetcher("EfinanceFetcher", 0)), patch(
            "data_provider.akshare_fetcher.AkshareFetcher",
            return_value=_StubFetcher("AkshareFetcher", 1),
        ), patch(
            "data_provider.pytdx_fetcher.PytdxFetcher",
            return_value=_StubFetcher("PytdxFetcher", 2),
        ), patch(
            "data_provider.baostock_fetcher.BaostockFetcher",
            return_value=_StubFetcher("BaostockFetcher", 3),
        ), patch(
            "data_provider.yfinance_fetcher.YfinanceFetcher",
            return_value=_StubFetcher("YfinanceFetcher", 4),
        ), patch(
            "data_provider.tushare_fetcher.TushareFetcher",
            return_value=_StubFetcher("TushareFetcher", -1),
        ) as mock_tushare, patch(
            "data_provider.longbridge_fetcher.LongbridgeFetcher",
            return_value=_StubFetcher("LongbridgeFetcher", 5),
        ) as mock_longbridge:
            mock_longbridge.has_configured_credentials.return_value = False
            manager = DataFetcherManager()

        self.assertEqual(
            manager.available_fetchers,
            [
                "EfinanceFetcher",
                "AkshareFetcher",
                "PytdxFetcher",
                "BaostockFetcher",
                "YfinanceFetcher",
            ],
        )
        mock_tushare.assert_not_called()
        mock_longbridge.assert_not_called()

    @patch("src.config.get_config")
    def test_manager_enables_longbridge_with_oauth_client_id(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            tushare_token="",
            longbridge_app_key="",
            longbridge_app_secret="",
            longbridge_access_token="",
            longbridge_oauth_client_id="client-1",
        )

        with patch("data_provider.efinance_fetcher.EfinanceFetcher", return_value=_StubFetcher("EfinanceFetcher", 0)), patch(
            "data_provider.akshare_fetcher.AkshareFetcher",
            return_value=_StubFetcher("AkshareFetcher", 1),
        ), patch(
            "data_provider.pytdx_fetcher.PytdxFetcher",
            return_value=_StubFetcher("PytdxFetcher", 2),
        ), patch(
            "data_provider.baostock_fetcher.BaostockFetcher",
            return_value=_StubFetcher("BaostockFetcher", 3),
        ), patch(
            "data_provider.yfinance_fetcher.YfinanceFetcher",
            return_value=_StubFetcher("YfinanceFetcher", 4),
        ), patch(
            "data_provider.tushare_fetcher.TushareFetcher",
            return_value=_StubFetcher("TushareFetcher", -1),
        ), patch(
            "data_provider.longbridge_fetcher.LongbridgeFetcher",
            return_value=_StubFetcher("LongbridgeFetcher", 5),
        ) as mock_longbridge:
            mock_longbridge.has_configured_credentials.return_value = True
            manager = DataFetcherManager()

        self.assertIn("LongbridgeFetcher", manager.available_fetchers)
        mock_longbridge.assert_called_once()

    @patch("src.config.get_config")
    def test_us_realtime_route_skips_temporarily_unavailable_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance,akshare_em,tushare",
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = False

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_realtime_quote.return_value = _make_quote("AAPL")

        manager = DataFetcherManager(fetchers=[longbridge, yfinance])

        quote = manager.get_realtime_quote("AAPL")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "AAPL")
        yfinance.get_realtime_quote.assert_called_once_with("AAPL")
        longbridge.get_realtime_quote.assert_not_called()

    @patch("src.config.get_config")
    def test_us_realtime_route_marks_longbridge_fallback_when_secondary_succeeds(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            enable_realtime_quote=True,
            realtime_source_priority="efinance,akshare_em,tushare",
            realtime_cache_ttl=600,
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = True
        longbridge.get_realtime_quote.return_value = None

        yfinance_quote = _make_quote("AAPL")
        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_realtime_quote.return_value = yfinance_quote

        manager = DataFetcherManager(fetchers=[longbridge, yfinance])

        quote = manager.get_realtime_quote("AAPL")

        self.assertIs(quote, yfinance_quote)
        self.assertEqual(quote.fallback_from, "longbridge")
        self.assertIsNotNone(quote.fetched_at)
        longbridge.get_realtime_quote.assert_called_once_with("AAPL")
        yfinance.get_realtime_quote.assert_called_once_with("AAPL")

    def test_hot_theme_universe_uses_ranked_boards_and_members(self):
        fetcher = MagicMock()
        fetcher.name = "AkshareFetcher"
        fetcher.priority = 1
        fetcher.get_concept_rankings.return_value = ([{"name": "AI", "change_pct": 3.0}], [])
        fetcher.get_sector_rankings.return_value = ([{"name": "半导体", "change_pct": 2.0}], [])

        def board_members(name, board_type, max_members):
            if name == "AI":
                return [{"code": "600001", "name": "热点A"}, {"code": "300001", "name": "创业板过滤"}]
            if name == "半导体":
                return [{"code": "600002", "name": "热点B"}, {"code": "600001", "name": "热点A"}]
            return []

        fetcher.get_board_members.side_effect = board_members
        manager = DataFetcherManager(fetchers=[fetcher])

        universe = manager.get_hot_theme_universe(n=1, max_members_per_theme=20)

        self.assertEqual(universe, {"600001": ["AI", "半导体"], "600002": ["半导体"]})
        fetcher.get_concept_rankings.assert_called_once_with(1)
        fetcher.get_sector_rankings.assert_called_once_with(1)
        self.assertEqual(fetcher.get_board_members.call_count, 2)

    def test_a_share_snapshot_uses_small_tencent_batch_before_bulk_sources(self):
        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1
        akshare.get_a_share_realtime_snapshot_tencent.return_value = None
        akshare.get_a_share_realtime_snapshot_em.return_value = [{"code": "600001", "source": "akshare_em"}]

        efinance = MagicMock()
        efinance.name = "EfinanceFetcher"
        efinance.priority = 0
        efinance.get_a_share_realtime_snapshot.return_value = [{"code": "600001", "source": "efinance"}]

        tushare = MagicMock()
        tushare.name = "TushareFetcher"
        tushare.priority = -1
        tushare.get_a_share_realtime_snapshot.return_value = [{"code": "600001", "source": "tushare"}]

        manager = DataFetcherManager(fetchers=[tushare, efinance, akshare])

        rows = manager.get_a_share_realtime_snapshot(["600001", "600002"])

        self.assertEqual(rows, [{"code": "600001", "source": "efinance"}])
        akshare.get_a_share_realtime_snapshot_tencent.assert_called_once_with(["600001", "600002"])
        efinance.get_a_share_realtime_snapshot.assert_called_once_with(["600001", "600002"])
        akshare.get_a_share_realtime_snapshot_em.assert_not_called()
        tushare.get_a_share_realtime_snapshot.assert_not_called()

    def test_a_share_snapshot_reuses_short_ttl_auxiliary_cache(self):
        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1
        akshare.get_a_share_realtime_snapshot_tencent.return_value = [{"code": "600001", "source": "tencent"}]
        manager = DataFetcherManager(fetchers=[akshare])

        first = manager.get_a_share_realtime_snapshot(["600001"])
        second = manager.get_a_share_realtime_snapshot(["600001"])

        self.assertEqual(first, second)
        akshare.get_a_share_realtime_snapshot_tencent.assert_called_once_with(["600001"])

    def test_a_share_snapshot_skips_provider_during_connection_cooldown(self):
        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1
        akshare.get_a_share_realtime_snapshot_tencent.side_effect = ConnectionError("RemoteDisconnected")
        akshare.get_a_share_realtime_snapshot_em.return_value = [{"code": "600001", "source": "akshare_em"}]
        manager = DataFetcherManager(fetchers=[akshare])

        first = manager.get_a_share_realtime_snapshot(["600001"])
        second = manager.get_a_share_realtime_snapshot(["600002"])

        self.assertEqual(first, [{"code": "600001", "source": "akshare_em"}])
        self.assertEqual(second, [{"code": "600001", "source": "akshare_em"}])
        akshare.get_a_share_realtime_snapshot_tencent.assert_called_once_with(["600001"])
        akshare.get_a_share_realtime_snapshot_em.assert_any_call(["600001"])
        akshare.get_a_share_realtime_snapshot_em.assert_any_call(["600002"])

    def test_board_members_reuses_auxiliary_cache(self):
        fetcher = MagicMock()
        fetcher.name = "AkshareFetcher"
        fetcher.priority = 1
        fetcher.get_board_members.return_value = [{"code": "600001", "name": "热点A"}]
        manager = DataFetcherManager(fetchers=[fetcher])

        first = manager.get_board_members("AI", "concept", 20)
        second = manager.get_board_members("AI", "concept", 20)

        self.assertEqual(first, second)
        fetcher.get_board_members.assert_called_once_with("AI", "concept", 20)

    def test_hourly_rate_limit_uses_one_hour_provider_cooldown(self):
        self.assertEqual(
            DataFetcherManager._provider_cooldown_seconds("抱歉，您访问接口(trade_cal)频率超限(1次/小时)"),
            3600,
        )

    @patch("src.config.get_config")
    def test_us_daily_route_skips_temporarily_unavailable_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="app-key",
            longbridge_app_secret="app-secret",
            longbridge_access_token="access-token",
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = False

        yfinance = MagicMock()
        yfinance.name = "YfinanceFetcher"
        yfinance.priority = 4
        yfinance.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[longbridge, yfinance])

        df, source = manager.get_daily_data("AAPL", start_date="2026-05-01", end_date="2026-05-08")

        self.assertFalse(df.empty)
        self.assertEqual(source, "YfinanceFetcher")
        yfinance.get_daily_data.assert_called_once()
        longbridge.get_daily_data.assert_not_called()

    def test_daily_data_prefers_local_stock_daily_when_enough(self):
        DatabaseManager.reset_instance()
        db = DatabaseManager(db_url="sqlite:///:memory:")
        code = "600519"
        today = date.today()
        local_df = pd.DataFrame(
            [
                {
                    "date": today - timedelta(days=offset),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 1000,
                    "amount": 10500,
                    "pct_chg": 1.0,
                }
                for offset in range(30)
            ]
        )
        db.save_daily_data(local_df, code, data_source="local_seed")
        fetcher = MagicMock()
        fetcher.name = "EfinanceFetcher"
        fetcher.priority = 0
        manager = DataFetcherManager(fetchers=[fetcher])

        try:
            df, source = manager.get_daily_data(code, days=30)

            self.assertEqual(source, "local_stock_daily")
            self.assertEqual(len(df), 30)
            fetcher.get_daily_data.assert_not_called()
        finally:
            DatabaseManager.reset_instance()

    def test_daily_data_fetches_and_saves_when_local_stock_daily_is_insufficient(self):
        DatabaseManager.reset_instance()
        db = DatabaseManager(db_url="sqlite:///:memory:")
        code = "600519"
        fresh_df = _make_daily_df()
        fetcher = MagicMock()
        fetcher.name = "EfinanceFetcher"
        fetcher.priority = 0
        fetcher.get_daily_data.return_value = fresh_df
        manager = DataFetcherManager(fetchers=[fetcher])

        try:
            df, source = manager.get_daily_data(code, days=30)

            self.assertFalse(df.empty)
            self.assertEqual(source, "EfinanceFetcher")
            fetcher.get_daily_data.assert_called_once()
            with db.get_session() as session:
                saved = session.query(StockDaily).filter(StockDaily.code == code).count()
            self.assertEqual(saved, len(fresh_df))
        finally:
            DatabaseManager.reset_instance()

    def test_daily_data_uses_local_stock_daily_after_fetch_saved_it(self):
        DatabaseManager.reset_instance()
        DatabaseManager(db_url="sqlite:///:memory:")
        code = "600519"
        fresh_df = pd.DataFrame(
            [
                {
                    "date": date.today() - timedelta(days=offset),
                    "open": 10.0,
                    "high": 11.0,
                    "low": 9.0,
                    "close": 10.5,
                    "volume": 1000,
                    "amount": 10500,
                    "pct_chg": 1.0,
                }
                for offset in range(30)
            ]
        )
        fetcher = MagicMock()
        fetcher.name = "EfinanceFetcher"
        fetcher.priority = 0
        fetcher.get_daily_data.return_value = fresh_df
        manager = DataFetcherManager(fetchers=[fetcher])

        try:
            first_df, first_source = manager.get_daily_data(code, days=30)
            second_df, second_source = manager.get_daily_data(code, days=30)

            self.assertFalse(first_df.empty)
            self.assertFalse(second_df.empty)
            self.assertEqual(first_source, "EfinanceFetcher")
            self.assertEqual(second_source, "local_stock_daily")
            fetcher.get_daily_data.assert_called_once()
        finally:
            DatabaseManager.reset_instance()

    @patch("src.config.get_config")
    def test_hk_daily_route_skips_temporarily_unavailable_longbridge(self, mock_get_config):
        mock_get_config.return_value = SimpleNamespace(
            longbridge_app_key="app-key",
            longbridge_app_secret="app-secret",
            longbridge_access_token="access-token",
        )

        longbridge = MagicMock()
        longbridge.name = "LongbridgeFetcher"
        longbridge.priority = 5
        longbridge.is_available_for_request.return_value = False

        akshare = MagicMock()
        akshare.name = "AkshareFetcher"
        akshare.priority = 1
        akshare.get_daily_data.return_value = _make_daily_df()

        manager = DataFetcherManager(fetchers=[longbridge, akshare])

        df, source = manager.get_daily_data("HK00700", start_date="2026-05-01", end_date="2026-05-08")

        self.assertFalse(df.empty)
        self.assertEqual(source, "AkshareFetcher")
        akshare.get_daily_data.assert_called_once()
        longbridge.get_daily_data.assert_not_called()


if __name__ == "__main__":
    unittest.main()
