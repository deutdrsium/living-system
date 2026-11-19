"""Operator-editable expression templates, independent of event and policy imports."""

from string import Formatter


EXPRESSION_INSTRUCTION = """保留人格的用词和关系距离，按 recent_timing、近期对话和角色状态选择顺聊还是另起话题。刚联系过就不用郑重开场，也不写成久别。
像平时发消息，有话直说。默认简短，半句或一行都行；可以吐槽、开个玩笑、顺口一问或约对方一起，不必每次留问题。按话题和人格调节语气，聊到深处再展开。避免硬加画面、比喻、固定开场、状态汇报、陪伴保证或服务式结尾。
按本轮分段设置排气泡，不靠删标点或堆空格制造口语感。称呼、事实、数值和因果关系说连贯；一个意思通常放一条，有自然转折再拆。"""
NEWS_SHARING_INSTRUCTION = """按角色的口吻挑一个亮点，直接聊发生了什么。可以顺口说句感想或好奇，不用每次都加，也不写成早报或新闻稿。
事实只取本次标题和摘要；想象、疑问用主观语气，不补剧情、背景、数字或结论。严肃、伤亡和风险消息按内容认真回应；不固定兴奋、卖萌、堆感叹号或每条追问。
一个意思可以一条说完，有转折才按设置分段。保留标点，不拆开数值、事实或紧密相连的解释，不为凑段数加内容。text 只写聊天正文，准确来源、原日期和链接留到对方追问时再给。"""
LENGTH_INSTRUCTION = ("主动正文控制在 {chars} 字以内；一句够就一句，保留必要说明，不凑字数。")
NEWS_LENGTH_INSTRUCTION = ("新闻正文合计控制在 {chars} 字以内；一句说清也可以，不为凑字数加事实、话题或追问。")
PROMPT_DEFAULTS = {
    "expression_prompt": EXPRESSION_INSTRUCTION,
    "news_sharing_prompt": NEWS_SHARING_INSTRUCTION,
    "length_prompt": LENGTH_INSTRUCTION,
    "news_length_prompt": NEWS_LENGTH_INSTRUCTION,
}
LENGTH_FIELDS = frozenset(("length_prompt", "news_length_prompt"))


def checked_template(field, value):
    if type(value) is not str or not value.strip() or len(value) > 8000:
        raise ValueError("expression templates require nonempty text within 8000 characters")
    if field in LENGTH_FIELDS:
        variables = []
        for _, name, spec, conversion in Formatter().parse(value):
            if name is not None:
                if name != "chars" or spec or conversion:
                    raise ValueError("length templates only support the plain {chars} variable")
                variables.append(name)
        if not variables:
            raise ValueError("length templates must retain {chars}")
    return value.replace("\r\n", "\n").replace("\r", "\n")


def effective_template(values, field):
    return checked_template(field, values.get(field, PROMPT_DEFAULTS[field]))
