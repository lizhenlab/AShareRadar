from __future__ import annotations

import json
import math
import re
from typing import Any

from app.models.schemas import AnalysisResult, StockQuestionAnswer


_RESPONSE_FIELDS = frozenset(
    {
        "conclusion",
        "confidence",
        "support",
        "resistance",
        "actions",
        "invalidations",
        "explanation",
    }
)
_PRICE_SEMANTICS = frozenset(
    {
        "current_price",
        "previous_close",
        "open_price",
        "high_price",
        "low_price",
        "price_change",
        "support",
        "resistance",
        "ma5",
        "ma10",
        "ma20",
        "generic_price",
    }
)
_PERCENT_SEMANTICS = frozenset({"change_pct", "turnover_rate", "confidence"})
_SCORE_SEMANTICS = frozenset({"trend_score", "data_quality_score"})
_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9_.])[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?")
_CHINESE_NUMBER_WITH_UNIT_RE = re.compile(
    r"(?:百分之[零〇一二两三四五六七八九十百点]+|"
    r"[零〇一二两三四五六七八九十百][零〇一二两三四五六七八九十百千万亿]*"
    r"(?:点[零〇一二两三四五六七八九]+)?"
    r"(?:%|％|成(?:仓)?|元|块|个百分点))"
)
_AUTHORITY_LABEL_RE = re.compile(
    r"(?:^|[。；;！？!?])\s*(?:规则)?(?:结论|行动|操作|失效条件|置信度|支撑位?|压力位?)\s*[:：]"
)
_CONCLUSION_REWRITE_RE = re.compile(
    r"(?:规则|系统)?(?:结论|判断).{0,8}(?:错误|不成立|并非|不是|应改|改为|应为|应该是)|"
    r"(?:推翻|改变|修正|改写|调整).{0,8}(?:结论|判断)"
)
_BULLISH_CONCLUSION_RE = re.compile(
    r"(?:已经|现在|当前|可以|适合|应当|应该|建议|值得).{0,5}(?:买入|加仓|建仓|抄底|追涨|做多)|"
    r"(?:无需|不用).{0,3}(?:等待|确认)"
)
_BEARISH_CONCLUSION_RE = re.compile(
    r"(?:应该|应当|建议|必须|立即|立刻|直接).{0,4}(?:卖出|减仓|清仓|离场|做空)"
)
_FORBIDDEN_DIRECTIVE_RES = (
    re.compile(
        r"(?:建议|应该|应当|务必|必须|最好|不妨|宜|可以|可考虑|立即|立刻|马上|直接|果断)"
        r"\s*(?:全仓|满仓|重仓|加仓|减仓|建仓|买入|卖出|清仓|持有|追涨|抄底|止盈|止损|做多|做空)"
    ),
    re.compile(r"(?:全仓|满仓|重仓|梭哈|无脑)\s*(?:买入|加仓|建仓|抄底|追涨)?"),
    re.compile(
        r"(?:^|[，,：:。；;！？!?])\s*(?:立即|立刻|马上|直接|果断)?\s*"
        r"(?:买入|卖出|加仓|减仓|建仓|清仓|满仓|重仓|持有|追涨|抄底|止盈|止损)(?:吧|即可|就行|为宜|。|！|!|$)"
    ),
    re.compile(r"(?:稳赚|保本|保证收益|确保盈利|必涨|必跌|零风险|没有风险)"),
)
_NEGATION_SUFFIXES = ("不", "别", "莫", "非", "勿", "未", "不要", "不能", "不应", "不宜", "避免", "切勿", "不可", "无需")
_DIRECT_ACTION_RE = re.compile(r"(?:买入|卖出|加仓|减仓|建仓|清仓|满仓|重仓|持有|追涨|抄底|止盈|止损|做多|做空)")
_ACTION_NOUN_SUFFIXES = ("信号", "条件", "指令", "建议", "依据", "逻辑", "理由", "时点", "区间", "价格", "动作")
_ACTION_CLAUSE_BOUNDARY_RE = re.compile(r"[，,。；;！？!?：:\n]")
_LONG_NEGATION_RE = re.compile(r"(?:避免|切勿|不可|不宜|不能|不应)(?P<scope>.{0,24})$")
_NEGATION_SCOPE_BREAK_RE = re.compile(r"(?:之后|以后|然后|建议|应该|应当|可以|可考虑|最好|不妨|反而|却|仍然|依然|后)$")
_RULE_NUMERIC_SEMANTIC_RES = {
    "risk_reward_ratio": re.compile(
        r"(?:收益风险比|风险收益比|盈亏比)\s*(?:仅|约|为|是|：|:|=)?\s*"
        r"(?P<value>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    ),
    "risk_multiplier": re.compile(
        r"(?:环境|市场)?风险倍率\s*(?:仅|约|为|是|：|:|=)?\s*"
        r"(?P<value>[-+]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)"
    ),
}


class LlmOutputValidationError(ValueError):
    pass


def authority_bindings(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> dict[str, Any]:
    return {
        "conclusion": rule_answer.conclusion,
        "confidence": rule_answer.confidence,
        "support": analysis.support,
        "resistance": analysis.resistance,
        "actions": list(rule_answer.actions),
        "invalidations": list(rule_answer.invalidations),
    }


def authority_binding_issue(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> str | None:
    if not _nonempty(rule_answer.conclusion):
        return "规则结论为空"
    if isinstance(rule_answer.confidence, bool) or not isinstance(rule_answer.confidence, int):
        return "置信度不是整数"
    if not 0 <= rule_answer.confidence <= 100:
        return "置信度超出0到100"
    for label, value in (("支撑位", analysis.support), ("压力位", analysis.resistance)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return f"{label}不是有限数值"
    for label, values in (("行动", rule_answer.actions), ("失效条件", rule_answer.invalidations)):
        if not isinstance(values, list) or any(not isinstance(item, str) for item in values):
            return f"{label}不是文本列表"
    return None


def validate_and_render_answer(
    raw_answer: Any,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> str:
    output = _decode_structured_output(raw_answer)
    _validate_response_shape(output)
    _validate_authority_bindings(output, rule_answer, analysis)
    explanation = _validated_explanation(output["explanation"], rule_answer, analysis)
    return _render_authoritative_answer(rule_answer, analysis, explanation)


def _validate_response_shape(output: dict[str, Any]) -> None:
    missing = sorted(_RESPONSE_FIELDS.difference(output))
    if missing:
        raise LlmOutputValidationError(f"缺少结构化字段 {', '.join(missing)}")
    extra = sorted(str(key) for key in set(output).difference(_RESPONSE_FIELDS))
    if extra:
        raise LlmOutputValidationError(f"包含未声明字段 {', '.join(extra)}")


def _validate_authority_bindings(
    output: dict[str, Any],
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> None:
    expected = authority_bindings(rule_answer, analysis)
    if output["conclusion"] != expected["conclusion"]:
        raise LlmOutputValidationError("结论矛盾：conclusion未原样绑定规则结论")
    if (
        isinstance(output["confidence"], bool)
        or not isinstance(output["confidence"], int)
        or output["confidence"] != expected["confidence"]
    ):
        raise LlmOutputValidationError("置信度绑定不一致")
    for field, label in (("support", "支撑位"), ("resistance", "压力位")):
        if not _bound_number_equal(output[field], expected[field]):
            raise LlmOutputValidationError(f"{label}绑定不一致")
    for field, label in (("actions", "行动"), ("invalidations", "失效条件")):
        if not isinstance(output[field], list) or output[field] != expected[field]:
            raise LlmOutputValidationError(f"{label}绑定不一致")


def _validated_explanation(
    explanation: Any,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> str:
    if not isinstance(explanation, str):
        raise LlmOutputValidationError("explanation必须是文本")
    explanation = " ".join(_clean_answer(explanation).splitlines())
    if not explanation:
        raise LlmOutputValidationError("explanation为空")
    if len(explanation) > 600:
        raise LlmOutputValidationError("explanation超过600字")
    if _AUTHORITY_LABEL_RE.search(explanation):
        raise LlmOutputValidationError("解释试图重写规则权威字段")
    if _conclusion_conflicts(explanation, rule_answer):
        raise LlmOutputValidationError("解释结论与规则引擎矛盾")
    if _contains_forbidden_directive(explanation):
        raise LlmOutputValidationError("解释包含越权买卖指令或确定性承诺")
    _validate_explanation_numbers(explanation, rule_answer, analysis)
    return explanation


def _decode_structured_output(raw_answer: Any) -> dict[str, Any]:
    if isinstance(raw_answer, dict):
        return raw_answer
    if not isinstance(raw_answer, str):
        raise LlmOutputValidationError("结构化输出不是文本JSON")
    text = raw_answer.strip()
    if not text:
        raise LlmOutputValidationError("模型输出为空")
    fenced = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if fenced:
        text = fenced.group(1)

    def reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise LlmOutputValidationError(f"JSON字段重复：{key}")
            result[key] = value
        return result

    def reject_non_finite(token: str) -> Any:
        raise LlmOutputValidationError(f"JSON包含非法数值：{token}")

    try:
        value = json.loads(
            text,
            object_pairs_hook=reject_duplicate_keys,
            parse_constant=reject_non_finite,
        )
    except LlmOutputValidationError:
        raise
    except (json.JSONDecodeError, TypeError, ValueError):
        raise LlmOutputValidationError("结构化输出不是有效JSON") from None
    if not isinstance(value, dict):
        raise LlmOutputValidationError("结构化输出顶层必须是JSON对象")
    return value


def _render_authoritative_answer(
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
    explanation: str,
) -> str:
    rule_text = _single_line(rule_answer.answer)
    actions = "；".join(_single_line(item) for item in rule_answer.actions if _single_line(item))
    invalidations = "；".join(_single_line(item) for item in rule_answer.invalidations if _single_line(item))
    return "\n".join(
        (
            f"规则结论：{_single_line(rule_answer.conclusion)}（规则建议强度 {rule_answer.confidence}/100）",
            f"规则答案：{rule_text}",
            f"大模型解释：{explanation}",
            f"规则行动：{actions or '规则引擎未给出额外行动项'}",
            f"关键位：支撑 {_format_price(analysis.support)} 元；压力 {_format_price(analysis.resistance)} 元",
            f"失效条件：{invalidations or '规则引擎未给出额外失效条件'}",
        )
    )


def _clean_answer(answer: str) -> str:
    lines = []
    for line in str(answer or "").replace("\r\n", "\n").replace("\r", "\n").splitlines():
        normalized = " ".join(line.strip().split())
        if normalized:
            lines.append(normalized)
    return "\n".join(lines)


def _single_line(value: Any) -> str:
    return " ".join(str(value or "").split())


def _format_price(value: float) -> str:
    return f"{float(value):.2f}"


def _bound_number_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or not isinstance(actual, (int, float)):
        return False
    actual_value = float(actual)
    expected_value = float(expected)
    return math.isfinite(actual_value) and math.isclose(actual_value, expected_value, rel_tol=0.0, abs_tol=1e-9)


def _conclusion_conflicts(explanation: str, rule_answer: StockQuestionAnswer) -> bool:
    if _CONCLUSION_REWRITE_RE.search(explanation):
        return True
    authority = " ".join((rule_answer.conclusion, rule_answer.answer, *rule_answer.actions))
    cautious = any(
        marker in authority
        for marker in ("等待", "观察", "暂不", "不追", "谨慎", "风险优先", "不抢", "回避", "减仓", "卖出", "清仓")
    )
    bullish = any(marker in authority for marker in ("建议买入", "可以参与", "适合买入", "加仓", "积极做多"))
    if cautious and _BULLISH_CONCLUSION_RE.search(explanation):
        return True
    if bullish and _BEARISH_CONCLUSION_RE.search(explanation):
        return True
    return False


def _contains_forbidden_directive(explanation: str) -> bool:
    for pattern in _FORBIDDEN_DIRECTIVE_RES:
        for match in pattern.finditer(explanation):
            prefix = _action_clause_prefix(explanation, match.start())
            if not _action_is_negated(prefix):
                return True
    for match in _DIRECT_ACTION_RE.finditer(explanation):
        prefix = _action_clause_prefix(explanation, match.start())
        suffix = explanation[match.end() : match.end() + 4].lstrip()
        if _action_is_negated(prefix) or suffix.startswith(_ACTION_NOUN_SUFFIXES):
            continue
        return True
    return False


def _action_is_negated(prefix: str) -> bool:
    if prefix.endswith(_NEGATION_SUFFIXES):
        return True
    if re.search(r"(?:不|未|没有|尚未|避免|无需|不能|不可|不宜|切勿|拒绝).{0,4}$", prefix):
        return True
    match = _LONG_NEGATION_RE.search(prefix)
    return bool(match and not _NEGATION_SCOPE_BREAK_RE.search(match.group("scope")))


def _action_clause_prefix(text: str, action_start: int) -> str:
    prefix = text[max(0, action_start - 36) : action_start].rstrip()
    return _ACTION_CLAUSE_BOUNDARY_RE.split(prefix)[-1].rstrip()


def _validate_explanation_numbers(
    explanation: str,
    rule_answer: StockQuestionAnswer,
    analysis: AnalysisResult,
) -> None:
    chinese_number = _CHINESE_NUMBER_WITH_UNIT_RE.search(explanation)
    if chinese_number:
        raise LlmOutputValidationError(f"数字单位无法绑定：{chinese_number.group()}")

    for match in _NUMBER_RE.finditer(explanation):
        raw = match.group()
        if _is_structural_number(explanation, match, raw, analysis):
            continue
        semantic = _number_semantic(explanation, match)
        unit = _number_unit(explanation, match)
        if semantic in {"target_price", "position_pct"}:
            raise LlmOutputValidationError("解释使用了规则未提供的目标价或仓位数字")
        if semantic is None:
            value = _parse_number(raw)
            if not any(_number_matches_allowed(value, item) for item in _allowed_numbers(rule_answer, analysis)):
                raise LlmOutputValidationError(f"包含规则上下文外数字：{raw}")
            raise LlmOutputValidationError(f"数字缺少字段语义绑定：{raw}")
        expected_unit = _expected_unit(semantic)
        if not _unit_is_compatible(unit, expected_unit):
            raise LlmOutputValidationError(f"单位/百分比使用错误：{raw}未按{_semantic_label(semantic)}表达")
        values = _semantic_values(semantic, rule_answer, analysis)
        if not any(_number_matches_semantic(raw, item, semantic) for item in values):
            raise LlmOutputValidationError(f"数字语义错配：{raw}不属于{_semantic_label(semantic)}")


def _number_semantic(text: str, match: re.Match[str]) -> str | None:
    before = text[max(0, match.start() - 18) : match.start()].lower()
    after = text[match.end() : match.end() + 10].lower()
    before_compact = re.sub(r"\s+", "", before)
    after_compact = re.sub(r"\s+", "", after)

    patterns: tuple[tuple[str, str], ...] = (
        (r"(?:目标价|目标位|止盈价|买入价|卖出价)(?:约|在|为|是|：|:|=)?$", "target_price"),
        (r"(?:仓位|持仓比例)(?:约|在|为|是|：|:|=)?$", "position_pct"),
        (r"(?:支撑|支撑位|下方支撑)(?:约|在|为|是|：|:|=)?$", "support"),
        (r"(?:压力|压力位|阻力|阻力位)(?:约|在|为|是|：|:|=)?$", "resistance"),
        (r"(?:规则)?置信度(?:约|在|为|是|：|:|=)?$", "confidence"),
        (r"(?:现价|当前价|最新价|最新价格)(?:约|在|为|是|：|:|=)?$", "current_price"),
        (r"(?:昨收|前收盘)(?:约|在|为|是|：|:|=)?$", "previous_close"),
        (r"(?:开盘价|开盘)(?:约|在|为|是|：|:|=)?$", "open_price"),
        (r"(?:最高价|最高)(?:约|在|为|是|：|:|=)?$", "high_price"),
        (r"(?:最低价|最低)(?:约|在|为|是|：|:|=)?$", "low_price"),
        (r"(?:涨跌额|价格变动)(?:约|在|为|是|：|:|=)?$", "price_change"),
        (r"(?:ma|ema)5(?:约|在|为|是|：|:|=)?$", "ma5"),
        (r"(?:ma|ema)10(?:约|在|为|是|：|:|=)?$", "ma10"),
        (r"(?:ma|ema)20(?:约|在|为|是|：|:|=)?$", "ma20"),
        (r"(?:涨跌幅|涨幅|跌幅|上涨|下跌)(?:约|在|为|是|：|:|=)?$", "change_pct"),
        (r"(?:换手率|换手)(?:约|在|为|是|：|:|=)?$", "turnover_rate"),
        (r"(?:趋势得分|趋势评分|趋势分)(?:约|在|为|是|：|:|=)?$", "trend_score"),
        (r"(?:数据质量得分|数据质量评分|质量得分|质量评分)(?:约|在|为|是|：|:|=)?$", "data_quality_score"),
        (r"(?:收益风险比|风险收益比|盈亏比)(?:仅|约|在|为|是|：|:|=)?$", "risk_reward_ratio"),
        (r"(?:环境|市场)?风险倍率(?:仅|约|在|为|是|：|:|=)?$", "risk_multiplier"),
        (r"(?:价格|价位|关键位)(?:约|在|为|是|：|:|=)?$", "generic_price"),
    )
    for pattern, semantic in patterns:
        if re.search(pattern, before_compact):
            return semantic

    after_patterns: tuple[tuple[str, str], ...] = (
        (r"^(?:元|块)?(?:附近|左右|一带)?(?:的)?(?:支撑|支撑位)", "support"),
        (r"^(?:元|块)?(?:附近|左右|一带)?(?:的)?(?:压力|压力位|阻力|阻力位)", "resistance"),
        (r"^(?:%|％|个百分点)?(?:的)?(?:置信度)", "confidence"),
        (r"^(?:元|块)?(?:的)?(?:现价|当前价|最新价)", "current_price"),
    )
    for pattern, semantic in after_patterns:
        if re.search(pattern, after_compact):
            return semantic
    return None


def _number_unit(text: str, match: re.Match[str]) -> str:
    before = text[max(0, match.start() - 5) : match.start()].rstrip()
    after = text[match.end() : match.end() + 8].lstrip()
    if before.endswith("百分之") or re.match(r"^(?:%|％|个百分点)", after):
        return "percent"
    if re.match(r"^成(?:仓)?", after):
        return "scaled_percent"
    if re.match(r"^(?:万|亿)元", after):
        return "scaled_currency"
    if before.endswith(("￥", "¥")) or re.match(r"^(?:元|块|人民币)", after):
        return "currency"
    if re.match(r"^分(?!钟)", after):
        return "score"
    if re.match(r"^(?:日|天|分钟|小时)", after):
        return "period"
    if re.match(r"^(?:个|条|项|类|种|层|点)", after):
        return "count"
    return "none"


def _expected_unit(semantic: str) -> str:
    if semantic in _PRICE_SEMANTICS:
        return "price"
    if semantic in _PERCENT_SEMANTICS:
        return "percent"
    if semantic in _SCORE_SEMANTICS:
        return "score"
    return "none"


def _unit_is_compatible(actual: str, expected: str) -> bool:
    if expected == "price":
        return actual in {"none", "currency"}
    if expected == "percent":
        return actual == "percent"
    if expected == "score":
        return actual in {"none", "score"}
    return actual == "none"


def _semantic_values(semantic: str, rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> tuple[float, ...]:
    quote = analysis.quote
    values: dict[str, tuple[Any, ...]] = {
        "current_price": (quote.price,),
        "previous_close": (quote.prev_close,),
        "open_price": (quote.open,),
        "high_price": (quote.high,),
        "low_price": (quote.low,),
        "price_change": (quote.change,),
        "support": (analysis.support,),
        "resistance": (analysis.resistance,),
        "ma5": (analysis.ma5,),
        "ma10": (analysis.ma10,),
        "ma20": (analysis.ma20,),
        "generic_price": (
            quote.price,
            quote.prev_close,
            quote.open,
            quote.high,
            quote.low,
            analysis.support,
            analysis.resistance,
            analysis.ma5,
            analysis.ma10,
            analysis.ma20,
        ),
        "change_pct": (quote.change_pct,),
        "turnover_rate": (quote.turnover_rate,),
        "confidence": (rule_answer.confidence,),
        "trend_score": (analysis.trend_score,),
        "data_quality_score": (analysis.data_quality.score,),
    }
    candidates = (*values.get(semantic, ()), *_rule_numeric_semantic_values(semantic, rule_answer))
    return tuple(
        float(item)
        for item in candidates
        if item is not None and not isinstance(item, bool) and _finite_number(item)
    )


def _rule_numeric_semantic_values(semantic: str, rule_answer: StockQuestionAnswer) -> tuple[float, ...]:
    pattern = _RULE_NUMERIC_SEMANTIC_RES.get(semantic)
    if pattern is None:
        return ()
    text = "\n".join(
        str(item)
        for item in (rule_answer.answer, *rule_answer.evidence, *rule_answer.actions, *rule_answer.invalidations)
    )
    return tuple(_parse_number(match.group("value")) for match in pattern.finditer(text))


def _semantic_label(semantic: str) -> str:
    labels = {
        "current_price": "现价",
        "previous_close": "昨收",
        "open_price": "开盘价",
        "high_price": "最高价",
        "low_price": "最低价",
        "price_change": "涨跌额",
        "support": "支撑位",
        "resistance": "压力位",
        "ma5": "MA5",
        "ma10": "MA10",
        "ma20": "MA20",
        "generic_price": "价格",
        "change_pct": "涨跌幅",
        "turnover_rate": "换手率",
        "confidence": "置信度",
        "trend_score": "趋势评分",
        "data_quality_score": "数据质量评分",
        "risk_reward_ratio": "收益风险比",
        "risk_multiplier": "风险倍率",
    }
    return labels.get(semantic, semantic)


def _number_matches_semantic(raw: str, expected: float, semantic: str) -> bool:
    value = _parse_number(raw)
    if semantic in _PRICE_SEMANTICS:
        decimal_text = raw.replace(",", "").lstrip("+-")
        decimal_places = len(decimal_text.partition(".")[2]) if "." in decimal_text else 0
        if decimal_places == 0:
            return math.isclose(value, round(expected), rel_tol=0.0, abs_tol=1e-9)
        if decimal_places <= 2:
            return math.isclose(value, round(expected, decimal_places), rel_tol=0.0, abs_tol=1e-9)
    return math.isclose(value, expected, rel_tol=0.0, abs_tol=1e-9)


def _is_structural_number(
    text: str,
    match: re.Match[str],
    raw: str,
    analysis: AnalysisResult,
) -> bool:
    value = _parse_number(raw)
    if _is_list_marker_number(text, match, value):
        return True
    if _is_stock_code_reference(text, match, raw.lstrip("+"), analysis.quote.code):
        return True
    suffix = text[match.end() : match.end() + 5].lstrip()
    prefix = text[max(0, match.start() - 4) : match.start()].lower().rstrip()
    if value in {5.0, 10.0, 20.0} and (
        re.match(r"^(?:日线|日均线)", suffix) or prefix.endswith(("ma", "ema"))
    ):
        return True
    return False


def _allowed_numbers(rule_answer: StockQuestionAnswer, analysis: AnalysisResult) -> set[float]:
    values: set[float] = set()
    for item in [
        analysis.quote.price,
        analysis.quote.prev_close,
        analysis.quote.open,
        analysis.quote.high,
        analysis.quote.low,
        analysis.quote.change,
        analysis.quote.change_pct,
        analysis.quote.turnover_rate,
        analysis.quote.pe,
        analysis.quote.pb,
        analysis.support,
        analysis.resistance,
        analysis.ma5,
        analysis.ma10,
        analysis.ma20,
        analysis.trend_score,
        analysis.data_quality.score,
        analysis.data_quality.kline_count,
        rule_answer.confidence,
    ]:
        _add_allowed_number(values, item)
    for text in [rule_answer.answer, *rule_answer.evidence, *rule_answer.actions, *rule_answer.invalidations]:
        for raw in _NUMBER_RE.findall(str(text)):
            _add_allowed_number(values, raw.replace(",", ""))
    return values


def _add_allowed_number(values: set[float], raw: Any, digits: int = 2) -> None:
    if raw is None or isinstance(raw, bool):
        return
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return
    if math.isfinite(value):
        values.add(round(value, digits))


def _number_matches_allowed(value: float, allowed: float) -> bool:
    tolerance = max(0.05, abs(allowed) * 0.005)
    return abs(value - allowed) <= tolerance


def _is_stock_code_reference(answer: str, match: re.Match[str], raw: str, stock_code: str) -> bool:
    if raw.replace(",", "") != stock_code:
        return False
    prefix = answer[max(0, match.start() - 8) : match.start()]
    suffix = answer[match.end() : match.end() + 4]
    return "代码" in prefix or suffix.upper().startswith((".SH", ".SZ", ".BJ"))


def _is_list_marker_number(answer: str, match: re.Match[str], value: float) -> bool:
    if value < 0 or value > 20 or not float(value).is_integer():
        return False
    suffix = answer[match.end() : match.end() + 1]
    if suffix not in {".", "、", ")", "）"}:
        return False
    prefix = answer[: match.start()].rstrip(" \t")
    return not prefix or prefix.endswith(("\n", "：", ":", "。", "；", ";", "！", "？"))


def _parse_number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _finite_number(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError):
        return False


def _nonempty(value: Any) -> bool:
    return bool(str(value).strip()) if value is not None else False
