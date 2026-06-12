"""Unit tests for hybrid CloudWatch + DynamoDB usage metrics."""

from __future__ import annotations

import datetime as dt
import unittest
from decimal import Decimal
from unittest.mock import MagicMock, patch

from agent import usage


class UsageSummaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = patch.dict(
            "os.environ",
            {
                "BEDROCK_MODEL_ID": "amazon.nova-lite-v1:0",
                "AGENT_USAGE_BUDGET_TOKENS": "1000",
                "AGENT_USAGE_DAILY_BUDGET_TOKENS": "500",
            },
            clear=False,
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()

    def _cw_response(self, *, day: dt.date, input_tokens: float, output_tokens: float, invocations: float):
        ts = dt.datetime.combine(day, dt.time(12, 0), tzinfo=dt.timezone.utc)
        return {
            "MetricDataResults": [
                {"Id": "input", "Timestamps": [ts], "Values": [input_tokens]},
                {"Id": "output", "Timestamps": [ts], "Values": [output_tokens]},
                {"Id": "invocations", "Timestamps": [ts], "Values": [invocations]},
            ]
        }

    @patch("agent.usage._table")
    @patch("agent.usage.cloudwatch_client")
    def test_merges_cloudwatch_and_dynamo(self, mock_cw_client, mock_table_fn):
        today = dt.datetime.now(dt.timezone.utc).date()
        cw = MagicMock()
        cw.get_metric_data.return_value = self._cw_response(
            day=today, input_tokens=100, output_tokens=50, invocations=3
        )
        mock_cw_client.return_value = cw

        table = MagicMock()
        mock_table_fn.return_value = table
        table.get_item.side_effect = [
            {"Item": {"requestCount": Decimal(2), "toolCallCount": Decimal(5)}},
            {"Item": {"requestCount": Decimal(2), "toolCallCount": Decimal(5)}},
        ]

        summary = usage.get_usage_summary(history_days=1)

        self.assertEqual(summary["totals"]["input_tokens"], 100)
        self.assertEqual(summary["totals"]["output_tokens"], 50)
        self.assertEqual(summary["totals"]["total_tokens"], 150)
        self.assertEqual(summary["totals"]["bedrock_rounds"], 3)
        self.assertEqual(summary["totals"]["request_count"], 2)
        self.assertEqual(summary["totals"]["tool_calls"], 5)
        self.assertEqual(len(summary["daily"]), 1)
        self.assertEqual(summary["daily"][0]["total_tokens"], 150)
        self.assertEqual(summary["daily"][0]["request_count"], 2)

    @patch("agent.usage._table")
    @patch("agent.usage.cloudwatch_client")
    def test_empty_cloudwatch_datapoints(self, mock_cw_client, mock_table_fn):
        cw = MagicMock()
        cw.get_metric_data.return_value = {
            "MetricDataResults": [
                {"Id": "input", "Timestamps": [], "Values": []},
                {"Id": "output", "Timestamps": [], "Values": []},
                {"Id": "invocations", "Timestamps": [], "Values": []},
            ]
        }
        mock_cw_client.return_value = cw

        table = MagicMock()
        mock_table_fn.return_value = table
        table.get_item.return_value = {"Item": {}}

        summary = usage.get_usage_summary(history_days=1)

        self.assertEqual(summary["totals"]["total_tokens"], 0)
        self.assertEqual(summary["totals"]["bedrock_rounds"], 0)
        self.assertEqual(summary["budget"]["total_used_pct"], 0.0)

    @patch("agent.usage._table")
    @patch("agent.usage.cloudwatch_client")
    def test_budget_uses_cloudwatch_totals(self, mock_cw_client, mock_table_fn):
        today = dt.datetime.now(dt.timezone.utc).date()
        cw = MagicMock()
        cw.get_metric_data.return_value = self._cw_response(
            day=today, input_tokens=400, output_tokens=100, invocations=1
        )
        mock_cw_client.return_value = cw

        table = MagicMock()
        mock_table_fn.return_value = table
        table.get_item.return_value = {"Item": {}}

        summary = usage.get_usage_summary(history_days=1)

        self.assertEqual(summary["budget"]["total_used_pct"], 50.0)
        self.assertEqual(summary["budget"]["daily_used_pct"], 100.0)
        self.assertEqual(summary["budget"]["today_tokens"], 500)

    @patch("agent.usage._table")
    def test_record_chat_usage_only_app_counters(self, mock_table_fn):
        table = MagicMock()
        mock_table_fn.return_value = table

        usage.record_chat_usage(
            input_tokens=999,
            output_tokens=888,
            total_tokens=1887,
            bedrock_rounds=4,
            tool_calls=2,
        )

        self.assertEqual(table.update_item.call_count, 2)
        for call in table.update_item.call_args_list:
            expr = call.kwargs["UpdateExpression"]
            self.assertIn("requestCount", expr)
            self.assertIn("toolCallCount", expr)
            self.assertNotIn("totalInputTokens", expr)
            self.assertNotIn("totalTokens", expr)
            self.assertNotIn("bedrockCallCount", expr)


if __name__ == "__main__":
    unittest.main()
