# -*- coding: utf-8 -*-
"""Tests for intraday stock screener filters."""

import unittest

import pandas as pd

from src.services.intraday_stock_screener import (
    IntradayScreenCandidate,
    IntradayScreenerCriteria,
    IntradayStockScreener,
    _candidate_reliability_sort_key,
    _score_candidate_reliability,
)


class IntradayStockScreenerFilterTests(unittest.TestCase):
    def test_rough_filter_uses_turnover_rate_above_point_seven_without_default_upper_bound(self):
        snapshot = [
            self._row("600001", "低换手", 0.7),
            self._row("600002", "放宽后入选", 0.8),
            self._row("600003", "高换手仍入选", 12.0),
        ]
        screener = IntradayStockScreener(snapshot_provider=lambda: snapshot)

        rows = screener._rough_filter(snapshot, criteria=IntradayScreenerCriteria())

        self.assertEqual([row["code"] for row in rows], ["600002", "600003"])

    def test_intraday_pattern_exposes_separate_scoring_signals(self):
        minute_rows = [
            {"price": 10.0, "avg_price": 10.0},
            {"price": 10.1, "avg_price": 10.0},
            {"price": 10.2, "avg_price": 10.0},
            {"price": 10.3, "avg_price": 10.0},
            {"price": 10.4, "avg_price": 10.0},
            {"price": 10.5, "avg_price": 10.0},
            {"price": 10.4, "avg_price": 10.0},
            {"price": 10.6, "avg_price": 10.0},
            {"price": 10.8, "avg_price": 10.0},
            {"price": 11.0, "avg_price": 10.0},
        ]
        screener = IntradayStockScreener(minute_provider=lambda code: minute_rows)

        pattern = screener._check_intraday_pattern("600001", IntradayScreenerCriteria(tail_minutes=3))
        scored = _score_candidate_reliability(
            {"volume_ratio": 1.2, "turnover_rate": 1.0},
            {"intraday_pattern": {"status": pattern.status, "passed": pattern.passed, "metrics": pattern.metrics}},
            [],
        )

        self.assertTrue(pattern.metrics["above_avg_passed"])
        self.assertTrue(pattern.metrics["pullback_holds"])
        self.assertTrue(pattern.metrics["tail_lifts"])
        self.assertIn("全天站上分时均线达标 +6", scored["reasons"])
        self.assertTrue(any(reason.startswith("创新高后回踩均线不破 +") for reason in scored["reasons"]))
        self.assertTrue(any(reason.startswith("尾盘稳步拉升 +") for reason in scored["reasons"]))
        self.assertIn("分时三项全部满足 +12", scored["reasons"])

    def test_partial_intraday_pattern_scores_break_depth_and_tail_reversals(self):
        scored = _score_candidate_reliability(
            {"volume_ratio": 1.2, "turnover_rate": 1.0},
            {
                "intraday_pattern": {
                    "status": "checked",
                    "passed": False,
                    "metrics": {
                        "above_avg_ratio": 0.9,
                        "above_avg_passed": False,
                        "pullback_holds": False,
                        "pullback_break_count": 2,
                        "pullback_break_ratio": 0.2,
                        "pullback_max_break_pct": 0.5,
                        "tail_lifts": False,
                        "tail_gain_pct": 0.6,
                        "tail_reversal_count": 4,
                        "tail_close_near_high_pct": -1.0,
                    },
                }
            },
            [],
        )

        self.assertTrue(any("回踩破均线 2 次/最深 0.50%" in reason for reason in scored["reasons"]))
        self.assertTrue(any("尾盘涨幅 0.60%/往复 4 次/离高点 -1.00%" in reason for reason in scored["reasons"]))
        self.assertNotIn("分时三项全部满足 +12", scored["reasons"])

    def test_complete_intraday_pattern_ranks_before_higher_partial_score(self):
        complete = self._candidate_with_pattern("600001", passed=True, score=60.0)
        partial = self._candidate_with_pattern("600002", passed=False, score=90.0)

        ranked = sorted([partial, complete], key=_candidate_reliability_sort_key, reverse=True)

        self.assertEqual([item.code for item in ranked], ["600001", "600002"])

    def test_limit_up_memory_counts_twenty_day_and_recent_hits(self):
        history = pd.DataFrame({"pct_chg": [0.0] * 15 + [9.9, 0.0, 10.1, 0.0, 10.5]})

        metrics = IntradayStockScreener._limit_up_memory_metrics(history, IntradayScreenerCriteria())

        self.assertTrue(metrics["has_recent_limit_up"])
        self.assertEqual(metrics["limit_up_count_20d"], 3)
        self.assertEqual(metrics["recent_limit_up_count_5d"], 3)

    def test_limit_up_memory_scores_by_count_and_recent_hits(self):
        scored = _score_candidate_reliability(
            {"volume_ratio": 1.2, "turnover_rate": 5.5},
            {"limit_up_count_20d": 2, "recent_limit_up_count_5d": 1},
            [],
        )

        self.assertIn("20日涨停记忆 2 次/近5日 1 次 +14.0", scored["reasons"])
        self.assertNotIn("20日内涨停记忆 +15", scored["reasons"])

    def test_turnover_rate_no_longer_adds_health_bonus_and_flags_high_turnover(self):
        healthy = _score_candidate_reliability(
            {"volume_ratio": 1.2, "turnover_rate": 6.0},
            {},
            [],
        )
        high = _score_candidate_reliability(
            {"volume_ratio": 1.2, "turnover_rate": 26.0},
            {},
            [],
        )

        self.assertIn("换手 6.0% 活跃度达标", healthy["reasons"])
        self.assertNotIn("换手处于偏健康区间 +3", healthy["reasons"])
        self.assertIn("换手 26.0% 极高，分歧偏大 -3", high["reasons"])
        self.assertEqual(high["score"], healthy["score"] - 3.0)

    @staticmethod
    def _candidate_with_pattern(code, passed, score):
        return IntradayScreenCandidate(
            code=code,
            name=code,
            price=10.0,
            change_pct=3.5,
            volume_ratio=1.2,
            turnover_rate=1.0,
            circ_mv=10_000_000_000.0,
            amount=100_000_000.0,
            passed=passed,
            rejected_reasons=[] if passed else ["intraday_pattern_not_confirmed"],
            metrics={"reliability_score": score, "intraday_pattern": {"passed": passed}},
            data_quality={},
        )

    @staticmethod
    def _row(code, name, turnover_rate):
        return {
            "code": code,
            "name": name,
            "price": 10.0,
            "change_pct": 3.5,
            "volume_ratio": 1.2,
            "turnover_rate": turnover_rate,
            "circ_mv": 10_000_000_000.0,
            "amount": 100_000_000.0,
        }


if __name__ == "__main__":
    unittest.main()
