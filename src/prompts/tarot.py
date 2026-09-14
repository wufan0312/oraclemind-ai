"""塔罗 Prompt（主占卜解读）

与 tarot_daily.py 的区别：
- tarot_daily：今日能量 / 每日三牌，输出【结构化 JSON】供卡片渲染；
- 本文件：用户自选牌阵 + 问题的一次性占卜，输出【塔罗专属 JSON 结构】
  （逐牌解读 / 牌阵主线 / 时间窗口 / 风险提醒 / 建议），由 formatter 转成 Markdown。

关键点：牌义由前端牌库注入（cards[].upright / rev / kw），LLM 必须以此为唯一口径，
禁止凭记忆自由发挥，避免与页面展示的本地牌义互相矛盾。
"""

from __future__ import annotations

from src.prompts.shared import (
    COMPLIANCE_BLOCK, INPUT_DISCLAIMER, INJECTION_GUARD, role_block, PromptTemplate,
)

# 主占卜的输出格式约束（JSON 结构）。
#
# 为什么抽成独立常量：多轮追问（routes/interpret.py 的 chat/stream）需要把这段
# **整段**替换成自然语言输出指令（CHAT_NL_OUTPUT）。此前只替换「【输出格式】」这 5 个字，
# JSON schema 会整段残留在 system 里，与「禁止输出任何 JSON 结构」直接冲突 —— 一旦模型
# 听信后者，前端 mdToHtml 会把 JSON 原文原样渲染给用户。必须与 dream 分支一样整段替换。
OUTPUT_FORMAT_TAROT = """【输出格式】
必须输出合法 JSON（不要输出任何 JSON 以外的文字、不要用 markdown 代码块包裹）：
{
  "ok": true,
  "verdict": "宜 / 中性 / 不宜 三选一，针对用户实际问题的明确倾向（抉择题必须选宜或不宜，不许回避）",
  "score": 72,
  "summary": "牌阵整体概述，≤80字，先给结论",
  "cards": [
    {"pos": "牌位名（与输入一致）", "name": "牌名（与输入一致）", "isRev": false, "text": "该牌在此牌位的解读，≤90字，必须结合牌位含义与正逆位牌义"}
  ],
  "synthesis": "牌与牌之间的联动与整体主线，≤150字",
  "timing": {
    "near": "未来 2-4 周的走势，≤40字，落到具体动作或现象",
    "mid": "未来 1-3 个月的发展，≤40字",
    "far": "3 个月之后的方向，≤40字"
  },
  "risk": "需要留意的地方，≤60字；用「宜谨慎 / 建议规避」等温和措辞",
  "advice": ["可执行建议1", "可执行建议2", "可执行建议3"]
}
cards 必须与输入的牌面一一对应（数量、顺序、牌名、正逆位均不得更改）。
score 为 0-100 的整数，表示本次牌面的能量顺遂度（逆位多、凶牌集中则偏低；大阿卡纳多则分量重）。
若输入不足以解读：{"ok": false, "reason": "简短原因"}"""

SYSTEM = f"""{role_block("你是精通塔罗牌解读的占卜师，擅长牌意、正逆位、牌阵综合分析。")}

{INPUT_DISCLAIMER}

【塔罗专则】
1. 牌面与正逆位由确定性算法抽出，是既定事实：禁止质疑、禁止重新抽牌、禁止改写牌名或牌位。
2. 每张牌给出的「牌义」是本次解读的唯一口径，必须基于它展开；禁止自行发明与该牌义冲突的解释，也不要引入牌义之外的玄学体系。
3. 逐牌解读必须结合「牌位含义」与该牌在此位置的正/逆位牌义，禁止套模板、禁止泛泛而谈。
   若提供了「分维度」，用户问到感情 / 事业 / 财运 / 健康时，必须以对应维度的牌义为准，
   不得用总牌义一句话敷衍。分维度是正位口径，逆位时按「受阻 / 内化 / 过度」理解。
4. 牌与牌之间必须建立联动（呼应、冲突、递进、转折），整篇要有一条清晰主线，不要写成彼此孤立的片段。
5. question 为空时按「整体运势指引」解读，不要追问用户、不要要求补充信息。
6. 逆位不等于凶：统一理解为能量受阻、转向内在或需要换个角度，语气保持温和。
7. **结论先行**：用户问「该不该 / 要不要 / 选 A 还是 B」这类抉择题时，verdict 必须给出明确倾向
   （宜 / 不宜），禁止用「看你自己」「都有可能」之类的话回避；牌面确实五五开时才用「中性」。
   verdict 必须与逐牌解读的结论自洽，不能前面说凶、结论写宜。
8. **时间窗口必须分阶段**：timing.near / mid / far 三段各自给出该阶段的判断，
   禁止三段写成同一句话，禁止用「时机可能已经成熟」「顺其自然」这类放之四海皆准的套话。
   塔罗不预测确切日期，但必须给出节奏：是快是慢、先守还是后攻、转折点大概落在哪个阶段。

{OUTPUT_FORMAT_TAROT}

{COMPLIANCE_BLOCK}
{INJECTION_GUARD}

【语气风格】
平和、克制、温暖，不夸大、不渲染焦虑。总字数随牌数浮动：3 张牌约 400 字，10 张牌不超过 1200 字。"""


def _build_user(result: dict, focus: str | None = None) -> str:
    spread = result.get('spreadName', '')
    question = (result.get('question') or '').strip()
    cards = result.get('cards') or []

    lines = []
    for i, c in enumerate(cards, 1):
        if not isinstance(c, dict):
            continue
        name = c.get('name', '')
        is_rev = bool(c.get('isRev'))
        pos = c.get('pos', '')
        pos_desc = c.get('posDesc', '')
        meaning = c.get('rev') if is_rev else c.get('upright')
        kw = c.get('kw') or []
        kw_text = '、'.join(str(k) for k in kw if k)
        seg = f"{i}. 牌位：{pos}"
        if pos_desc:
            seg += f"（{pos_desc}）"
        seg += f" — {'逆位' if is_rev else '正位'}·{name}"
        seg += f"\n   牌义：{meaning or '（未提供）'}"
        dim = c.get("dim")
        if isinstance(dim, dict):
            dim_text = "；".join(f"{k}：{v}" for k, v in dim.items() if isinstance(v, str) and v.strip())
            if dim_text:
                seg += f"\n   分维度：{dim_text}"
        if kw_text:
            seg += f"\n   关键词：{kw_text}"
        lines.append(seg)

    return f"""请解读以下塔罗牌阵：

牌阵：{spread}（共 {len(cards)} 张）
问题：{question or '（用户未填写具体问题，请按整体运势指引解读）'}

牌位与牌面（牌义为本次解读的唯一口径）：
{chr(10).join(lines) if lines else '（无牌面数据）'}

请按 system 约定的 JSON 结构输出，cards 需与上方牌面一一对应。"""


tarot_prompt = PromptTemplate(
    module="tarot",
    version="3.1",
    system=SYSTEM,
    build_user=_build_user,
    temperature=0.75,
    max_tokens=3072,
)
