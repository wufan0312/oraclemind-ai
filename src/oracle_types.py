"""玄镜 OracleMind · Pydantic 类型定义

对应 TS 版 src/types.ts，排盘结果输入类型与前端字段对齐。
AI 服务只消费「已计算好的确定性结果」，绝不自行推算。

注意：模块名使用 oracle_types 避免与 Python 标准库 types 冲突。
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ============================ 八字 ============================
class BaziPillar(BaseModel):
    label: str
    gan: str
    zhi: str
    el: str
    elCls: str = Field(alias="elCls")
    note: str
    gold: Optional[bool] = None

    model_config = {"populate_by_name": True}


class BaziShishen(BaseModel):
    name: str
    val: int
    wuxing: str


class BaziWuxing(BaseModel):
    label: str
    pct: float
    icon: str


class BaziDayun(BaseModel):
    age: str
    gan: str
    note: str
    gold: Optional[bool] = None
    primary: Optional[bool] = None
    highlight: Optional[bool] = None


class BaziResult(BaseModel):
    solar: str
    lunar: str
    shengxiao: str
    timeText: str
    dayMaster: str
    dayMasterWuxing: str
    pillars: list[BaziPillar]
    shiShen: list[BaziShishen]
    wuxing: list[BaziWuxing]
    dayun: list[BaziDayun]
    liunian: list[dict]  # {yr, gan, note}
    analysis: str
    yongshen: dict  # {xi: [], ji: []}


# ============================ 紫微斗数 ============================
class ZiweiPalace(BaseModel):
    name: str
    icon: str
    color: str
    star: str
    sub: str
    pos: str
    gan: str
    highlight: bool
    misc: Optional[list[str]] = None
    changsheng: Optional[str] = None
    borrow: Optional[list[str]] = None


class ZiweiSihua(BaseModel):
    star: str
    hua: str
    palace: str


class ZiweiPattern(BaseModel):
    name: str
    desc: str


class ZiweiResult(BaseModel):
    solar: str
    lunar: str
    timeText: str
    mingGong: str
    shenGong: str
    wuxingJu: str
    ziwei: str
    palaces: list[ZiweiPalace]
    sihua: list[ZiweiSihua]
    analysis: str
    patterns: Optional[list[ZiweiPattern]] = None


# ============================ 六爻 ============================
class HexagramView(BaseModel):
    name: str
    symbol: str
    desc: str
    gong: Optional[str] = None
    gongWuxing: Optional[str] = None


class LiuyaoLine(BaseModel):
    pos: str
    idx: int
    yao: str
    text: str
    gan: str
    zhi: str
    wuxing: str
    shishen: str
    name: str
    gold: bool
    liushen: Optional[str] = None


class LiuyaoResult(BaseModel):
    solar: str
    lunar: str
    timeText: str
    qigua: dict
    benGua: HexagramView
    bianGua: Optional[HexagramView] = None
    dongYao: int
    lines: list[LiuyaoLine]
    yongshen: dict  # {name, meaning, ...}
    analysis: list[dict]  # [{title, text}]


# ============================ 梅花易数 ============================
class TrigramView(BaseModel):
    name: str
    symbol: str
    num: int
    wuxing: str
    meaning: str
    virtue: str


class MeihuaHexagram(BaseModel):
    name: str
    symbol: str
    desc: str
    gong: Optional[str] = None
    upper: TrigramView
    lower: TrigramView


class MeihuaResult(BaseModel):
    solar: str
    lunar: str
    timeText: str
    qigua: str
    benGua: MeihuaHexagram
    bianGua: Optional[MeihuaHexagram] = None
    huGua: Optional[MeihuaHexagram] = None
    dongYao: int
    ti: TrigramView
    yong: TrigramView
    tiYong: dict  # {relation, ji, desc, dongNote}
    analysis: list[dict]


# ============================ 奇门遁甲 ============================
class QimenPalace(BaseModel):
    dir: str
    star: str
    starColor: str
    door: str
    doorColor: str
    god: str
    comb: str
    tianpan: str
    dipan: str
    jixiong: str


class QimenQi(BaseModel):
    title: str
    text: str
    color: str


class QimenResult(BaseModel):
    solar: str
    lunar: str
    timeText: str
    dateText: str
    jieqi: str
    type: str
    valueFu: str
    valueShi: str
    shiGan: str
    shiGanZhi: str
    riGanZhi: str
    palaces: list[QimenPalace]
    qi: list[QimenQi]


# ============================ 数字命理 / 塔罗 / 解梦 / 风水 ============================
class NumerologyResult(BaseModel):
    lifePath: int
    birthdayNum: int
    counts: dict[str, int]
    missing: list[int]
    years: list[dict]
    data: dict


class TarotResult(BaseModel):
    spreadKey: str
    spreadName: str
    question: str
    count: int
    positions: list[str]
    cards: list[dict]


class DreamRequest(BaseModel):
    dream: str
    context: Optional[str] = None
    perspective: Optional[Literal["jung", "freud", "cognitive", "fortune"]] = None


class FengshuiResult(BaseModel):
    dimension: Literal["wealth", "health", "love", "career"]
    stars: list[dict]
    mingGua: Optional[dict] = None
    bazhai: Optional[list[dict]] = None
    bazi: Optional[dict] = None
    xingSha: Optional[list[dict]] = None
    facing: Optional[str] = None
    year: int
    birthDate: Optional[dict] = None


# ============================ 解读请求/响应 ============================
InterpretModule = Literal[
    "bazi", "ziwei", "liuyao", "meihua", "qimen",
    # 三式补齐（P2-1 / P2-2）
    "liuren", "taiyi",
    "numerology", "tarot", "dream", "fengshui",
    # 天使数字（P2-3）
    "angel",
]


class InterpretRequest(BaseModel):
    module: InterpretModule
    result: Any  # 排盘结果 JSON（已计算好的确定性结果）；超大 payload 由调用方控制体积
    requestId: Optional[str] = None
    reportId: Optional[int] = None
    focus: Optional[str] = Field(default=None, max_length=2000)


class InterpretMeta(BaseModel):
    requestId: str
    module: str
    promptVersion: str
    provider: str
    model: str
    cacheHit: bool = False
    degraded: bool = False
    degradedReason: Optional[str] = None
    tokens: Optional[dict] = None  # {prompt, completion}
    costYuan: Optional[float] = None
    latencyMs: int = 0
    truncated: bool = False
    retrieval: Optional[dict] = None  # {enabled, count, sources, text}


class InterpretResponse(BaseModel):
    text: str
    disclaimer: str
    meta: InterpretMeta


# ============================ Agent ============================
class MingAgentRequest(BaseModel):
    message: str
    birthHint: Optional[dict] = None
    requestId: Optional[str] = None
