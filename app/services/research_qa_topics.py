from __future__ import annotations

import re


# Whole-question patterns bound this feature to current stock research intents.
# Unrecognised wording is deliberately unavailable, even when it contains a topic word.
QUESTION_TOPIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (topic, re.compile(pattern)) for topic, pattern in (
        ("做T", r"(?:适合|适不适合|能不能|能否|可以|能)做t|做t(?:怎么样|怎么做)|(?:低吸区和高抛区|高抛低吸区间)在哪里|什么情况停止做t|没有底仓能不能做t|(?:适合|适不适合|能不能|能否)(?:t\+0|t0|高抛低吸)"),
        ("主题概念", r"(?:有什么|有哪些|属于什么|属于哪些)(?:概念|题材|主题|概念题材)|(?:概念|题材|主题)(?:是什么|有哪些|怎么样)|题材热度能不能支撑走势|概念热但个股弱怎么办"),
        ("风险收益", r"(?:风险收益比|收益风险比|性价比|赔率|盈亏比)(?:够不够|是否合适|怎么样|如何|高不高|是否合理)|上方空间和下方防守在哪里|什么情况性价比会失效|值不值得参与"),
        ("风险", r"(?:最大的)?风险(?:在哪里|是什么|有哪些|怎么样|如何|大不大|高不高)|哪些信号能解除风险|止损(?:位置在哪里|条件是什么)|跌破哪个位置结论失效"),
        ("买点", r"(?:能不能|能否|可以|能|要不要|是否适合)(?:低吸)?(?:买|买入|加仓|进场|入场|低吸)(?:一点|一些)?|(?:买点|买入条件|入场条件)(?:在哪里|是什么|有哪些|怎么样)|(?:有没有|是否有)买点|什么时候(?:可以买|可以买入|能买|能入场)"),
        ("卖点", r"(?:能不能|能否|可以|能|要不要|是否应该)(?:冲高)?(?:卖|卖出|减仓|止盈)(?:一点|一些)?|压力位附近怎么处理|什么情况可以继续观察|(?:卖点|卖出条件|止盈条件)(?:在哪里|是什么|有哪些)"),
        ("同行龙头", r"同行里算不算龙头|(?:相对)?同行(?:强不强|强|怎么样|如何)|估值在同行里贵不贵|行业里谁更强|(?:行业|板块)(?:排名|地位)(?:怎么样|如何)|是不是(?:行业|板块)?龙头"),
        ("事件", r"事件(?:有什么影响|偏利好还是利空|怎么样|如何)|事件会不会改变买卖点|还缺哪些数据"),
        ("短线观察", r"怎么看|重点看什么|(?:支撑压力|支撑|压力)在哪里|(?:上方空间|下方防守)在哪里"),
        ("综合判断", r"综合(?:说一下|分析|分析一下|判断|判断一下|怎么看)|整体(?:怎么看|怎么样|如何)|总体(?:怎么看|怎么样|如何)|分析一下|评价一下|判断一下|总建议是什么"),
    )
)



RELATED_QUESTIONS: dict[str, list[str]] = {
    "买点": ["明天重点看什么？", "跌破哪个位置结论失效？", "当前风险收益比够不够？"],
    "卖点": ["压力位附近怎么处理？", "什么情况可以继续观察？", "止损条件是什么？"],
    "做T": ["低吸区和高抛区在哪里？", "什么情况停止做T？", "没有底仓能不能做T？"],
    "风险": ["最大的风险是什么？", "哪些信号能解除风险？", "止损位置在哪里？"],
    "风险收益": ["当前风险收益比够不够？", "上方空间和下方防守在哪里？", "什么情况性价比会失效？"],
    "主题概念": ["它有哪些概念？", "题材热度能不能支撑走势？", "概念热但个股弱怎么办？"],
    "同行龙头": ["它相对同行强吗？", "估值在同行里贵不贵？", "行业里谁更强？"],
    "事件": ["近期事件偏利好还是利空？", "事件会不会改变买卖点？", "还缺哪些数据？"],
    "短线观察": ["明天重点看什么？", "支撑压力在哪里？", "能不能低吸？"],
}


DEFAULT_RELATED_QUESTIONS = ["现在能不能买？", "风险在哪里？", "适不适合做T？"]


def stock_question_topic(question: str, *, stock_name: str = "", stock_code: str = "") -> str:
    text = re.sub(r"\s+", "", question.lower()).strip("。？！?!")
    text = re.sub(r"^(?:请问|请|帮我|麻烦)+", "", text)
    subjects = ["这只股票", "这只个股", "这只股", "该股票", "该股", "个股", "它", "这个股票"]
    subjects.extend(item.lower() for item in (stock_name, stock_code) if item)
    subject = "|".join(re.escape(item) for item in sorted(subjects, key=len, reverse=True))
    text = re.sub(rf"^(?:(?:{subject})(?:\.(?:sh|sz|bj))?(?:的)?|现在|目前|当前|今天|今日|近期|最近|明天|短线)+", "", text)
    text = text.rstrip("吗呢呀啊")
    for topic, pattern in QUESTION_TOPIC_PATTERNS:
        if pattern.fullmatch(text):
            return topic
    return "不支持的问题"


def related_questions(topic: str) -> list[str]:
    return RELATED_QUESTIONS.get(topic, DEFAULT_RELATED_QUESTIONS)


__all__ = [
    "DEFAULT_RELATED_QUESTIONS",
    "QUESTION_TOPIC_PATTERNS",
    "RELATED_QUESTIONS",
    "related_questions",
    "stock_question_topic",
]
