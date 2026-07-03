from __future__ import annotations

import asyncio
from datetime import datetime
import math
import unittest
from unittest.mock import patch
from pathlib import Path
from tempfile import TemporaryDirectory

from app.models.schemas import StockQuestionAnswer
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.llm_explainer import _allowed_numbers, enhance_stock_answer
from app.config import Settings, _load_shell_env
from tests.factories import make_kline as _kline, make_quote as _quote


class LlmExplainerTests(unittest.TestCase):
    def test_llm_shell_env_loads_llm_fields(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".zshrc"
            path.write_text(
                "\n".join(
                    [
                        "# AShareRadar LLM configuration",
                        "export ASHARE_RADAR_LLM_API_KEY=' file-key '",
                        'export ASHARE_RADAR_LLM_BASE_URL="https://example.test/v1"',
                        "export ASHARE_RADAR_LLM_MODEL='test-model'",
                        "export ASHARE_RADAR_LLM_ENABLED=1",
                        "export ASHARE_RADAR_LLM_TIMEOUT_SECONDS=3",
                    ]
                ),
                encoding="utf-8",
            )

            values = _load_shell_env(
                path,
                {
                    "ASHARE_RADAR_LLM_API_KEY",
                    "ASHARE_RADAR_LLM_BASE_URL",
                    "ASHARE_RADAR_LLM_MODEL",
                    "ASHARE_RADAR_LLM_ENABLED",
                    "ASHARE_RADAR_LLM_TIMEOUT_SECONDS",
                },
            )

        self.assertEqual(values["ASHARE_RADAR_LLM_API_KEY"], "file-key")
        self.assertEqual(values["ASHARE_RADAR_LLM_BASE_URL"], "https://example.test/v1")
        self.assertEqual(values["ASHARE_RADAR_LLM_MODEL"], "test-model")
        self.assertEqual(values["ASHARE_RADAR_LLM_ENABLED"], "1")
        self.assertEqual(values["ASHARE_RADAR_LLM_TIMEOUT_SECONDS"], "3")

    def test_llm_explainer_falls_back_without_api_key(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(llm_enabled=True, llm_api_key=None)

        result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, rule_answer.answer)
        self.assertEqual(result.answer_source, "规则问诊")
        self.assertFalse(result.llm_used)
        self.assertEqual(result.llm_status, "未配置大模型API")

    def test_llm_explainer_falls_back_without_endpoint_or_model(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(llm_enabled=True, llm_api_key="test-key", llm_base_url=None, llm_model=None)

        result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, rule_answer.answer)
        self.assertEqual(result.answer_source, "规则问诊")
        self.assertFalse(result.llm_used)
        self.assertEqual(result.llm_status, "未配置大模型API")

    def test_llm_explainer_uses_grounded_model_answer(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )
        llm_text = f"结论：先观察。为什么：现价 {analysis.quote.price:.2f}，高于支撑 {analysis.support:.2f}，压力 {analysis.resistance:.2f} 未突破。接下来盯什么：看20日线和量能。失效条件：跌破 {analysis.support:.2f}。"

        with patch("app.services.llm_explainer._call_llm", return_value=llm_text):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, llm_text)
        self.assertTrue(result.llm_used)
        self.assertIn("test-model", result.answer_source)
        self.assertEqual(result.llm_status, "已基于当前分析结果生成解释")

    def test_llm_explainer_rejects_ungrounded_numbers(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )

        with patch("app.services.llm_explainer._call_llm", return_value="结论：可以等 1888 元突破后再看，这是当前关键位。"):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, rule_answer.answer)
        self.assertFalse(result.llm_used)
        self.assertIn("事实校验", result.llm_status or "")

    def test_llm_explainer_rejects_ungrounded_number_before_comma(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )

        with patch("app.services.llm_explainer._call_llm", return_value="结论：等待 1888，突破后再看。"):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertEqual(result.answer, rule_answer.answer)
        self.assertFalse(result.llm_used)
        self.assertIn("事实校验", result.llm_status or "")

    def test_llm_explainer_allows_small_list_marker_numbers(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )
        llm_text = (
            f"1. 结论：先观察。\n"
            f"2. 为什么：现价 {analysis.quote.price:.2f}，支撑 {analysis.support:.2f}。\n"
            f"3. 失效条件：跌破 {analysis.support:.2f}。"
        )

        with patch("app.services.llm_explainer._call_llm", return_value=llm_text):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertTrue(result.llm_used)
        self.assertEqual(result.answer, llm_text)

    def test_llm_explainer_allows_stock_code_only_as_code_reference(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )
        llm_text = f"结论：{analysis.quote.code}.SH 先观察。为什么：现价 {analysis.quote.price:.2f}，支撑 {analysis.support:.2f}。"

        with patch("app.services.llm_explainer._call_llm", return_value=llm_text):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertTrue(result.llm_used)
        self.assertEqual(result.answer, llm_text)

    def test_llm_explainer_rejects_stock_code_as_market_number(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="test-key",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )

        with patch("app.services.llm_explainer._call_llm", return_value=f"结论：等 {analysis.quote.code} 元再看。"):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertFalse(result.llm_used)
        self.assertIn("事实校验", result.llm_status or "")

    def test_llm_explainer_redacts_api_key_from_failure_status(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = Settings(
            llm_enabled=True,
            llm_api_key="secret-key-123",
            llm_base_url="https://example.test/v1",
            llm_model="test-model",
        )

        with patch("app.services.llm_explainer._call_llm", side_effect=RuntimeError("bad secret-key-123")):
            result = asyncio.run(enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis))

        self.assertFalse(result.llm_used)
        self.assertNotIn("secret-key-123", result.llm_status or "")
        self.assertIn("<redacted>", result.llm_status or "")

    def test_llm_allowed_numbers_drop_non_finite_values(self) -> None:
        analysis, rule_answer = _llm_test_case()
        dirty_analysis = analysis.model_copy(
            update={
                "quote": analysis.quote.model_copy(update={"price": math.inf, "pe": math.nan}),
                "support": math.inf,
            }
        )

        allowed = _allowed_numbers(rule_answer, dirty_analysis)

        self.assertTrue(allowed)
        self.assertTrue(all(math.isfinite(item) for item in allowed))


def _llm_test_case() -> tuple:
    klines = [
        _kline(date=f"2026-05-{index + 1:02d}", close=1260 + index * 2.0, high=1262 + index * 2.0, low=1258 + index * 2.0, volume=2000 + index * 50)
        for index in range(25)
    ]
    quote = _quote(price=1300.0, prev_close=1290.0, high=1310.0, low=1288.0, change_pct=0.78)
    quality = build_data_quality(quote, klines, now=datetime(2026, 5, 13, 10, 0, 0))
    analysis = build_analysis(quote, klines, data_quality=quality)
    rule_answer = StockQuestionAnswer(
        symbol="600519.SH",
        updated_at=quote.timestamp,
        question="现在能不能买？",
        topic="买点",
        conclusion="等待确认",
        answer="规则结论：当前更适合等待确认，不追高。",
        confidence=68,
        evidence=[f"现价 {analysis.quote.price:.2f}", f"支撑 {analysis.support:.2f}", f"压力 {analysis.resistance:.2f}"],
        actions=[f"回踩 {analysis.support:.2f} 附近观察承接。"],
        invalidations=[f"跌破 {analysis.support:.2f} 先降级。"],
    )
    return analysis, rule_answer
