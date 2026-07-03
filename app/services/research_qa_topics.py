from __future__ import annotations


QUESTION_TOPIC_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("主题概念", ("概念", "题材", "主题", "风口", "热点")),
    ("风险收益", ("风险收益", "收益风险", "性价比", "赔率", "盈亏比", "空间够", "空间大", "值不值得")),
    ("风险", ("风险", "止损", "跌破", "亏", "雷", "回撤", "危险")),
    ("买点", ("买", "加仓", "进场", "入场", "低吸", "能不能上")),
    ("卖点", ("卖", "减仓", "离场", "止盈", "压力", "冲高", "高抛")),
    ("同行龙头", ("同行", "行业", "板块", "龙头", "强不强", "排名")),
    ("事件", ("事件", "消息", "公告", "异动", "利好", "利空")),
    ("短线观察", ("明天", "今天", "短线", "支撑", "压力", "怎么看")),
]


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


def stock_question_topic(question: str) -> str:
    text = question.lower()
    if _asks_t_strategy(text):
        return "做T"
    for topic, keywords in QUESTION_TOPIC_KEYWORDS:
        if any(word in text for word in keywords):
            return topic
    return "综合判断"


def related_questions(topic: str) -> list[str]:
    return RELATED_QUESTIONS.get(topic, DEFAULT_RELATED_QUESTIONS)


def _asks_t_strategy(text: str) -> bool:
    return any(word in text for word in ("做t", "做 t", "t+0", "t0", "高抛低吸")) or ("高抛" in text and "低吸" in text)


__all__ = [
    "DEFAULT_RELATED_QUESTIONS",
    "QUESTION_TOPIC_KEYWORDS",
    "RELATED_QUESTIONS",
    "related_questions",
    "stock_question_topic",
]
