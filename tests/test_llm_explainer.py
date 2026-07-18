from __future__ import annotations

import asyncio
from datetime import datetime
import json
import math
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.config import Settings, _load_shell_env
from app.models.schemas import StockQuestionAnswer
from app.services.analysis import build_analysis
from app.services.data_quality import build_data_quality
from app.services.llm_explainer import _allowed_numbers, _call_llm, enhance_stock_answer
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

    def test_llm_shell_env_only_accepts_standalone_top_level_assignments(self) -> None:
        with TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".zshrc"
            path.write_text(
                "\n".join(
                    [
                        "ASHARE_RADAR_LLM_MODEL='top-level model'",
                        "env ASHARE_RADAR_LLM_MODEL=env-value command",
                        "command --model ASHARE_RADAR_LLM_MODEL=argument-value",
                        "ASHARE_RADAR_LLM_MODEL=temporary-value command",
                        "(export ASHARE_RADAR_LLM_MODEL=subshell-value)",
                        "command --model \\",
                        "ASHARE_RADAR_LLM_MODEL=continued-argument-value",
                        "cat <<'LLM_CONFIG_SAMPLE'",
                        "ASHARE_RADAR_LLM_MODEL=heredoc-value",
                        "LLM_CONFIG_SAMPLE",
                        "configure_llm() {",
                        "  export ASHARE_RADAR_LLM_MODEL=function-value",
                        "}",
                        "if true; then",
                        "  ASHARE_RADAR_LLM_MODEL=conditional-value",
                        "fi",
                        "export ASHARE_RADAR_LLM_API_KEY=' quoted key ' # safe comment",
                    ]
                ),
                encoding="utf-8",
            )

            values = _load_shell_env(
                path,
                {"ASHARE_RADAR_LLM_API_KEY", "ASHARE_RADAR_LLM_MODEL"},
            )

        self.assertEqual(
            values,
            {
                "ASHARE_RADAR_LLM_API_KEY": "quoted key",
                "ASHARE_RADAR_LLM_MODEL": "top-level model",
            },
        )

    def test_llm_explainer_falls_back_without_complete_configuration(self) -> None:
        analysis, rule_answer = _llm_test_case()
        cases = (
            Settings(llm_enabled=True, llm_api_key=None),
            Settings(llm_enabled=True, llm_api_key=" ", llm_base_url="https://example.test/v1", llm_model="m"),
            Settings(llm_enabled=True, llm_api_key="test-key", llm_base_url=None, llm_model=None),
        )

        for settings in cases:
            with self.subTest(settings=settings):
                result = asyncio.run(
                    enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis)
                )

                _assert_rule_fallback(self, result, rule_answer)
                self.assertEqual(result.llm_status, "未配置大模型API")

    def test_llm_explainer_renders_only_explanation_from_model(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanation = (
            f"代码 {analysis.quote.code}.SH 的现价 {analysis.quote.price:.2f} 元位于"
            f"支撑 {analysis.support:.2f} 元和压力 {analysis.resistance:.2f} 元之间，"
            f"涨跌幅 {analysis.quote.change_pct:.2f}%，MA20仍用于确认趋势。"
        )

        result = _enhance_with_output(analysis, rule_answer, _structured_json(analysis, rule_answer, explanation))

        self.assertTrue(result.llm_used)
        self.assertEqual(result.conclusion, rule_answer.conclusion)
        self.assertEqual(result.confidence, rule_answer.confidence)
        self.assertEqual(result.actions, rule_answer.actions)
        self.assertEqual(result.invalidations, rule_answer.invalidations)
        self.assertIn(f"规则结论：{rule_answer.conclusion}", result.answer)
        self.assertIn(f"规则建议强度 {rule_answer.confidence}/100", result.answer)
        self.assertNotIn(f"置信度 {rule_answer.confidence}%", result.answer)
        self.assertIn(f"涨跌幅 {analysis.quote.change_pct:.2f}%", result.answer)
        self.assertIn(f"大模型解释：{explanation}", result.answer)
        self.assertIn(rule_answer.actions[0], result.answer)
        self.assertIn(f"支撑 {analysis.support:.2f} 元", result.answer)
        self.assertIn(f"压力 {analysis.resistance:.2f} 元", result.answer)
        self.assertIn(rule_answer.invalidations[0], result.answer)
        self.assertIn("test-model", result.answer_source)
        self.assertIn("仅增强解释", result.llm_status or "")

    def test_llm_explainer_repairs_one_invalid_output_then_succeeds(self) -> None:
        analysis, rule_answer = _llm_test_case()
        valid = _structured_json(analysis, rule_answer, "趋势与风险仍支持规则保持等待。")

        with patch("app.services.llm_explainer._call_llm", side_effect=["not-json", valid]) as call:
            result = asyncio.run(
                enhance_stock_answer(
                    settings=_llm_settings(),
                    rule_answer=rule_answer,
                    analysis=analysis,
                )
            )

        self.assertTrue(result.llm_used)
        self.assertEqual(call.call_count, 2)
        self.assertIn("经一次格式纠错", result.llm_status or "")
        self.assertTrue(call.call_args_list[1].kwargs["repair"])

    def test_llm_explainer_attempts_at_most_one_validation_repair(self) -> None:
        analysis, rule_answer = _llm_test_case()

        with patch("app.services.llm_explainer._call_llm", return_value="not-json") as call:
            result = asyncio.run(
                enhance_stock_answer(
                    settings=_llm_settings(),
                    rule_answer=rule_answer,
                    analysis=analysis,
                )
            )

        _assert_rule_fallback(self, result, rule_answer)
        self.assertEqual(call.call_count, 2)
        self.assertIn("纠错重试仍未通过", result.llm_status or "")

    def test_llm_explainer_rejects_each_authoritative_binding_change(self) -> None:
        analysis, rule_answer = _llm_test_case()
        mutations = (
            ("conclusion", "可以买入", "结论"),
            ("confidence", rule_answer.confidence + 1, "置信度"),
            ("support", analysis.resistance, "支撑位"),
            ("resistance", analysis.support, "压力位"),
            ("actions", ["立即买入"], "行动"),
            ("invalidations", ["永不失效"], "失效条件"),
        )

        for field, replacement, diagnostic in mutations:
            with self.subTest(field=field):
                output = _structured_output(analysis, rule_answer, "价格仍处于等待确认区域。")
                output[field] = replacement
                result = _enhance_with_output(analysis, rule_answer, json.dumps(output, ensure_ascii=False))

                _assert_rule_fallback(self, result, rule_answer)
                self.assertIn(diagnostic, result.llm_status or "")

    def test_llm_explainer_rejects_plain_text_and_malformed_json(self) -> None:
        analysis, rule_answer = _llm_test_case()

        for output in ("结论：先观察。", '{"conclusion":', "[]"):
            with self.subTest(output=output):
                result = _enhance_with_output(analysis, rule_answer, output)

                _assert_rule_fallback(self, result, rule_answer)
                self.assertIn("结构化输出", result.llm_status or "")

    def test_llm_explainer_rejects_numberless_trading_instruction(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanations = ("依据已经充分，务必清仓。", "依据已经充分，继续持有。")

        for explanation in explanations:
            with self.subTest(explanation=explanation):
                result = _enhance_with_output(
                    analysis,
                    rule_answer,
                    _structured_json(analysis, rule_answer, explanation),
                )

                _assert_rule_fallback(self, result, rule_answer)
                self.assertIn("越权", result.llm_status or "")

    def test_llm_explainer_allows_negated_action_signal_explanation(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanation = "买入信号尚未确认，因此规则保持等待。"

        result = _enhance_with_output(
            analysis,
            rule_answer,
            _structured_json(analysis, rule_answer, explanation),
        )

        self.assertTrue(result.llm_used)
        self.assertIn(explanation, result.answer)

    def test_llm_explainer_allows_clause_scoped_long_negated_action(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanation = "趋势仍弱，因此避免在确认不足时提前判定止跌或追涨。"

        result = _enhance_with_output(
            analysis,
            rule_answer,
            _structured_json(analysis, rule_answer, explanation),
        )

        self.assertTrue(result.llm_used)
        self.assertIn(explanation, result.answer)

    def test_llm_explainer_does_not_carry_negation_across_clause_boundary(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanation = "避免忽视风险，但建议持有。"

        result = _enhance_with_output(
            analysis,
            rule_answer,
            _structured_json(analysis, rule_answer, explanation),
        )

        _assert_rule_fallback(self, result, rule_answer)
        self.assertIn("越权", result.llm_status or "")

    def test_llm_explainer_accepts_grounded_rule_ratio_and_risk_multiplier(self) -> None:
        analysis, rule_answer = _llm_test_case()
        rule_answer = rule_answer.model_copy(
            update={"evidence": [*rule_answer.evidence, "收益风险比 1.17，环境风险倍率 0.97。"]}
        )
        explanation = "收益风险比仅 1.17，环境风险倍率 0.97，说明规则仍需等待确认。"

        result = _enhance_with_output(
            analysis,
            rule_answer,
            _structured_json(analysis, rule_answer, explanation),
        )

        self.assertTrue(result.llm_used)
        self.assertIn(explanation, result.answer)

    def test_llm_explainer_rejects_unbound_rule_ratio_or_multiplier(self) -> None:
        analysis, rule_answer = _llm_test_case()
        rule_answer = rule_answer.model_copy(
            update={"evidence": [*rule_answer.evidence, "收益风险比 1.17，环境风险倍率 0.97。"]}
        )

        for explanation in ("收益风险比 1.18 仍偏低。", "环境风险倍率 0.98 仍需观察。"):
            with self.subTest(explanation=explanation):
                result = _enhance_with_output(
                    analysis,
                    rule_answer,
                    _structured_json(analysis, rule_answer, explanation),
                )

                _assert_rule_fallback(self, result, rule_answer)
                self.assertIn("数字语义错配", result.llm_status or "")

    def test_llm_explainer_rejects_conclusion_conflict_inside_explanation(self) -> None:
        analysis, rule_answer = _llm_test_case()
        raw = _structured_json(analysis, rule_answer, "现阶段已经适合买入，无需等待确认。")

        result = _enhance_with_output(analysis, rule_answer, raw)

        _assert_rule_fallback(self, result, rule_answer)
        self.assertIn("矛盾", result.llm_status or "")

    def test_llm_explainer_rejects_ungrounded_number(self) -> None:
        analysis, rule_answer = _llm_test_case()
        raw = _structured_json(analysis, rule_answer, "关键数字是 1888，仍需观察。")

        result = _enhance_with_output(analysis, rule_answer, raw)

        _assert_rule_fallback(self, result, rule_answer)
        self.assertIn("上下文外数字", result.llm_status or "")

    def test_llm_explainer_rejects_numeric_semantic_mismatch(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanations = (
            f"支撑 {analysis.resistance:.2f} 元尚未确认。",
            f"支撑 {rule_answer.confidence} 元尚未确认。",
            f"涨跌幅 {rule_answer.confidence}% 说明波动存在。",
        )

        for explanation in explanations:
            with self.subTest(explanation=explanation):
                result = _enhance_with_output(
                    analysis,
                    rule_answer,
                    _structured_json(analysis, rule_answer, explanation),
                )

                _assert_rule_fallback(self, result, rule_answer)
                self.assertIn("数字语义错配", result.llm_status or "")

    def test_llm_explainer_rejects_unit_and_percentage_abuse(self) -> None:
        analysis, rule_answer = _llm_test_case()
        explanations = (
            f"支撑 {analysis.support:.2f}% 尚未确认。",
            f"规则置信度 {rule_answer.confidence} 元，仍需核验。",
            f"涨跌幅 {analysis.quote.change_pct:.2f}，波动有限。",
            f"现价 {analysis.quote.price:.2f} 万元，仍在区间内。",
        )

        for explanation in explanations:
            with self.subTest(explanation=explanation):
                result = _enhance_with_output(
                    analysis,
                    rule_answer,
                    _structured_json(analysis, rule_answer, explanation),
                )

                _assert_rule_fallback(self, result, rule_answer)
                self.assertIn("单位/百分比", result.llm_status or "")

    def test_llm_explainer_rejects_target_price_even_when_number_is_known(self) -> None:
        analysis, rule_answer = _llm_test_case()
        raw = _structured_json(analysis, rule_answer, f"目标价 {analysis.quote.price:.2f} 元已经明确。")

        result = _enhance_with_output(analysis, rule_answer, raw)

        _assert_rule_fallback(self, result, rule_answer)
        self.assertIn("目标价或仓位", result.llm_status or "")

    def test_llm_explainer_rejects_unknown_structured_fields(self) -> None:
        analysis, rule_answer = _llm_test_case()
        output = _structured_output(analysis, rule_answer, "区间尚未完成确认。")
        output["recommendation"] = "满仓"

        result = _enhance_with_output(analysis, rule_answer, json.dumps(output, ensure_ascii=False))

        _assert_rule_fallback(self, result, rule_answer)
        self.assertIn("未声明字段", result.llm_status or "")

    def test_llm_explainer_timeout_cancels_call_and_falls_back(self) -> None:
        analysis, rule_answer = _llm_test_case()
        cancelled = asyncio.Event()

        async def slow_call(*args, **kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                cancelled.set()

        settings = _llm_settings(llm_timeout_seconds=0.02)
        with patch("app.services.llm_explainer._call_llm", new=slow_call):
            result = asyncio.run(
                enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis)
            )

        _assert_rule_fallback(self, result, rule_answer)
        self.assertTrue(cancelled.is_set())
        self.assertIn("请求超时", result.llm_status or "")

    def test_llm_timeout_is_shared_with_the_validation_repair(self) -> None:
        analysis, rule_answer = _llm_test_case()
        calls: list[bool] = []

        async def staged_call(*args, repair=False, **kwargs):
            calls.append(repair)
            await asyncio.sleep(0.04)
            if repair:
                return _structured_json(analysis, rule_answer, "趋势仍需等待规则确认。")
            return "not-json"

        settings = _llm_settings(llm_timeout_seconds=0.06)
        with patch("app.services.llm_explainer._call_llm", new=staged_call):
            result = asyncio.run(
                enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis)
            )

        _assert_rule_fallback(self, result, rule_answer)
        self.assertEqual(calls, [False, True])
        self.assertIn("请求超时", result.llm_status or "")

    def test_llm_client_is_ark_compatible_closed_and_does_not_use_json_schema(self) -> None:
        analysis, rule_answer = _llm_test_case()
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
        )
        create = AsyncMock(return_value=completion)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            close=AsyncMock(),
        )
        settings = _llm_settings(
            llm_api_key="ark-secret-key",
            llm_base_url="https://ark.cn-beijing.volces.com/api/v3",
            llm_model="doubao-test",
            llm_timeout_seconds=1.25,
        )

        with patch("openai.AsyncOpenAI", return_value=client) as constructor:
            content = asyncio.run(_call_llm(settings, rule_answer, analysis))

        self.assertEqual(content, '{"ok": true}')
        constructor.assert_called_once_with(
            api_key="ark-secret-key",
            base_url="https://ark.cn-beijing.volces.com/api/v3",
            timeout=1.25,
            max_retries=0,
        )
        request = create.await_args.kwargs
        self.assertNotIn("response_format", request)
        self.assertNotIn("json_schema", request)
        self.assertEqual(request["model"], "doubao-test")
        self.assertEqual(request["temperature"], 0.0)
        serialized_messages = json.dumps(request["messages"], ensure_ascii=False)
        self.assertNotIn("ark-secret-key", serialized_messages)
        self.assertIn("expected_output", serialized_messages)
        self.assertIn("规则建议强度（兼容字段 confidence）", serialized_messages)
        self.assertIn("不是概率、命中率或统计置信度", serialized_messages)
        self.assertIn("不得用百分号表达", serialized_messages)
        client.close.assert_awaited_once_with()

    def test_llm_repair_prompt_requires_numberless_non_action_explanation(self) -> None:
        analysis, rule_answer = _llm_test_case()
        completion = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok": true}'))]
        )
        create = AsyncMock(return_value=completion)
        client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create)),
            close=AsyncMock(),
        )

        with patch("openai.AsyncOpenAI", return_value=client):
            asyncio.run(_call_llm(_llm_settings(), rule_answer, analysis, repair=True))

        serialized_messages = json.dumps(create.await_args.kwargs["messages"], ensure_ascii=False)
        self.assertIn("上一次输出未通过本地校验", serialized_messages)
        self.assertIn("不得出现数字", serialized_messages)
        self.assertIn("任何买卖动作词", serialized_messages)

    def test_llm_client_closes_when_completion_fails(self) -> None:
        analysis, rule_answer = _llm_test_case()
        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=RuntimeError("request failed")))
            ),
            close=AsyncMock(),
        )

        with patch("openai.AsyncOpenAI", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "request failed"):
                asyncio.run(_call_llm(_llm_settings(), rule_answer, analysis))

        client.close.assert_awaited_once_with()

    def test_llm_client_closes_when_outer_timeout_cancels_request(self) -> None:
        analysis, rule_answer = _llm_test_case()
        request_cancelled = asyncio.Event()

        async def slow_completion(**kwargs):
            try:
                await asyncio.sleep(10)
            finally:
                request_cancelled.set()

        client = SimpleNamespace(
            chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(side_effect=slow_completion))
            ),
            close=AsyncMock(),
        )
        settings = _llm_settings(llm_timeout_seconds=0.02)

        with patch("openai.AsyncOpenAI", return_value=client):
            result = asyncio.run(
                enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis)
            )

        _assert_rule_fallback(self, result, rule_answer)
        self.assertTrue(request_cancelled.is_set())
        self.assertIn("请求超时", result.llm_status or "")
        client.close.assert_awaited_once_with()

    def test_llm_explainer_sanitizes_provider_failure_status(self) -> None:
        analysis, rule_answer = _llm_test_case()
        settings = _llm_settings(llm_api_key="secret-key-123")
        cases = (
            (
                "GET https://alice:password@example.test/v1?access_token=query-token&key=query-key",
                ("alice", "password", "query-token", "query-key"),
            ),
            ("Authorization: Bearer bearer-token", ("bearer-token",)),
            ("X-API-Key: header-key", ("header-key",)),
            ("provider rejected sk-live-secret123", ("sk-live-secret123",)),
            (f"request failed for {settings.llm_api_key}", ("secret-key-123",)),
            (f"request payload question={rule_answer.question}", (rule_answer.question,)),
        )

        for message, secrets in cases:
            with self.subTest(message=message):
                with patch(
                    "app.services.llm_explainer._call_llm",
                    side_effect=RuntimeError(message),
                ):
                    result = asyncio.run(
                        enhance_stock_answer(settings=settings, rule_answer=rule_answer, analysis=analysis)
                    )

                _assert_rule_fallback(self, result, rule_answer)
                status = result.llm_status or ""
                for secret in secrets:
                    self.assertNotIn(secret, status)
                self.assertIn("<redacted>", status)

    def test_llm_validation_errors_use_the_same_sensitive_value_sanitizer(self) -> None:
        analysis, rule_answer = _llm_test_case()
        output = _structured_output(analysis, rule_answer, "价格区间仍需确认。")
        output[rule_answer.question] = "unexpected"

        result = _enhance_with_output(analysis, rule_answer, json.dumps(output, ensure_ascii=False))

        _assert_rule_fallback(self, result, rule_answer)
        self.assertNotIn(rule_answer.question, result.llm_status or "")
        self.assertIn("<redacted>", result.llm_status or "")

    def test_llm_validation_diagnostic_never_echoes_model_output(self) -> None:
        analysis, rule_answer = _llm_test_case()
        secret = "model-leaked-secret"

        result = _enhance_with_output(analysis, rule_answer, f"not-json {secret}")

        _assert_rule_fallback(self, result, rule_answer)
        self.assertNotIn(secret, result.llm_status or "")

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


def _enhance_with_output(analysis, rule_answer, output: str) -> StockQuestionAnswer:
    with patch("app.services.llm_explainer._call_llm", return_value=output):
        return asyncio.run(
            enhance_stock_answer(
                settings=_llm_settings(),
                rule_answer=rule_answer,
                analysis=analysis,
            )
        )


def _structured_json(analysis, rule_answer, explanation: str) -> str:
    return json.dumps(_structured_output(analysis, rule_answer, explanation), ensure_ascii=False)


def _structured_output(analysis, rule_answer, explanation: str) -> dict:
    return {
        "conclusion": rule_answer.conclusion,
        "confidence": rule_answer.confidence,
        "support": analysis.support,
        "resistance": analysis.resistance,
        "actions": list(rule_answer.actions),
        "invalidations": list(rule_answer.invalidations),
        "explanation": explanation,
    }


def _llm_settings(**updates) -> Settings:
    values = {
        "llm_enabled": True,
        "llm_api_key": "test-key",
        "llm_base_url": "https://example.test/v1",
        "llm_model": "test-model",
        "llm_timeout_seconds": 2.0,
    }
    values.update(updates)
    return Settings(**values)


def _assert_rule_fallback(
    case: unittest.TestCase,
    result: StockQuestionAnswer,
    rule_answer: StockQuestionAnswer,
) -> None:
    case.assertEqual(result.answer, rule_answer.answer)
    case.assertEqual(result.conclusion, rule_answer.conclusion)
    case.assertEqual(result.confidence, rule_answer.confidence)
    case.assertEqual(result.evidence, rule_answer.evidence)
    case.assertEqual(result.actions, rule_answer.actions)
    case.assertEqual(result.invalidations, rule_answer.invalidations)
    case.assertEqual(result.answer_source, "规则问诊")
    case.assertFalse(result.llm_used)


def _llm_test_case() -> tuple:
    klines = [
        _kline(
            date=f"2026-05-{index + 1:02d}",
            close=1260 + index * 2.0,
            high=1262 + index * 2.0,
            low=1258 + index * 2.0,
            volume=2000 + index * 50,
        )
        for index in range(25)
    ]
    quote = _quote(
        price=1300.0,
        prev_close=1290.0,
        high=1310.0,
        low=1288.0,
        change_pct=0.78,
    )
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
        evidence=[
            f"现价 {analysis.quote.price:.2f}",
            f"支撑 {analysis.support:.2f}",
            f"压力 {analysis.resistance:.2f}",
        ],
        actions=[f"回踩 {analysis.support:.2f} 附近观察承接。"],
        invalidations=[f"跌破 {analysis.support:.2f} 先降级。"],
    )
    return analysis, rule_answer
