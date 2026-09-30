"""玄镜 OracleMind · 占星术服务

计算用户本命盘（真实星历）并编排 LLM 生成专业解读 / 运势预测。
- 计算底座：pymeeus（纯 Python 天文算法，无需外部星历文件，离线可用）
- 解读：复用 generate_structured（主/备供应商故障切换 + JSON 解析 + 重试）
- 降级：预算熔断 / LLM 不可用 / 解析失败时，回退本地规则文案

宫制说明：
- 默认 equal（等宫制）：每宫恒定 30°，宫头 = ASC + i*30。
- whole（整宫制）：第 1 宫头 = ASC 所在星座 0°，星座即宫位。
- Placidus / Koch 等时间宫制需瑞士星历（pyswisseph），属后续升级项；当前未提供以免失真。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from typing import Any, Optional

from pymeeus.Epoch import Epoch

from src.config import config
from src.services.structured import generate_structured
from src.harness.quality.hook import maybe_quality_check
from src.services.budget import budget_broken, record_cost, estimate_cost, cache_get, cache_set

logger = logging.getLogger(__name__)

DISCLAIMER = "以上内容由 AI 生成，仅供娱乐与传统文化参考，不构成任何决策、医疗或投资依据。"


# ============================== 中文标签映射 ==============================
SIGN_ZH: dict[str, list[str]] = {
    "aries": ["白羊座", "♈"], "taurus": ["金牛座", "♉"], "gemini": ["双子座", "♊"],
    "cancer": ["巨蟹座", "♋"], "leo": ["狮子座", "♌"], "virgo": ["处女座", "♍"],
    "libra": ["天秤座", "♎"], "scorpio": ["天蝎座", "♏"], "sagittarius": ["射手座", "♐"],
    "capricorn": ["摩羯座", "♑"], "aquarius": ["水瓶座", "♒"], "pisces": ["双鱼座", "♓"],
}
PLANET_ZH: dict[str, list[str]] = {
    "sun": ["太阳", "☉"], "moon": ["月亮", "☽"], "mercury": ["水星", "☿"],
    "venus": ["金星", "♀"], "mars": ["火星", "♂"], "jupiter": ["木星", "♃"],
    "saturn": ["土星", "♄"], "uranus": ["天王星", "♅"], "neptune": ["海王星", "♆"],
    "pluto": ["冥王星", "♇"],
}
ASPECT_ZH: dict[str, list[str]] = {
    "conjunction": ["合相", "合"], "opposition": ["对冲", "冲"], "trine": ["三分相", "三合"],
    "square": ["四分相", "刑"], "sextile": ["六分相", "六合"], "quincunx": ["梅花相", "梅花"],
}
HOUSE_NAMES = [
    "命宫", "财帛宫", "兄弟宫", "田宅宫", "子女宫", "奴仆宫",
    "夫妻宫", "疾厄宫", "迁移宫", "官禄宫", "福德宫", "玄秘宫",
]
# 十二宫位主题（一句话含义，用于前端展示）
HOUSE_THEMES = [
    "自我、外在气质、健康", "财富、价值观、收入", "沟通、手足、短途", "家庭、根基、原生",
    "恋爱、子女、创造", "工作、日常、健康", "婚姻、合伙、密友", "蜕变、他人资源、危机",
    "远行、学识、信念", "事业、名望、社会角色", "福报、人际、愿景", "潜意识、灵性、独处",
]
# 星座四象元素
SIGN_ELEMENT: dict[str, str] = {
    "aries": "火", "leo": "火", "sagittarius": "火",
    "taurus": "土", "virgo": "土", "capricorn": "土",
    "gemini": "风", "libra": "风", "aquarius": "风",
    "cancer": "水", "scorpio": "水", "pisces": "水",
}
# 现代守护星（入庙）
_DIGNITY_RULER: dict[str, str] = {
    "aries": "mars", "taurus": "venus", "gemini": "mercury", "cancer": "moon", "leo": "sun",
    "virgo": "mercury", "libra": "venus", "scorpio": "pluto", "sagittarius": "jupiter",
    "capricorn": "saturn", "aquarius": "uranus", "pisces": "neptune",
}
# 曜升（擢升）星座——古典 7 曜
_DIGNITY_EXALT: dict[str, str] = {
    "aries": "sun", "taurus": "moon", "cancer": "jupiter", "virgo": "mercury",
    "libra": "saturn", "capricorn": "mars", "pisces": "venus",
}
_OPP: dict[str, str] = {
    "aries": "libra", "libra": "aries", "taurus": "scorpio", "scorpio": "taurus",
    "gemini": "sagittarius", "sagittarius": "gemini", "cancer": "capricorn", "capricorn": "cancer",
    "leo": "aquarius", "aquarius": "leo", "virgo": "pisces", "pisces": "virgo",
}
# 纳入本命盘计算的星体
KNOWN_PLANETS = [
    "sun", "moon", "mercury", "venus", "mars", "jupiter",
    "saturn", "uranus", "neptune", "pluto",
]
# 小行星 / 虚点（近似算法，娱乐级）：北交(月交点)、莉莉丝(黑月)、凯龙
SMALL_POINTS = [
    {"key": "northnode", "label": "北交点", "glyph": "☊"},
    {"key": "southnode", "label": "南交点", "glyph": "☋"},
    {"key": "lilith", "label": "莉莉丝", "glyph": "⚸"},
    {"key": "chiron", "label": "凯龙星", "glyph": "⚷"},
]
TRANSIT_PLANETS = ["sun", "moon", "mercury", "venus", "mars", "jupiter", "saturn"]

_SIGN_KEYS = list(SIGN_ZH.keys())


# ============================== pymeeus 取数 ==============================
def _planet_lon_lat(epoch_obj: Epoch, key: str) -> tuple[float, float]:
    """返回 (黄经°, 黄纬°)。pymeeus 返回 Angle 对象，取 .d。"""
    from pymeeus.Sun import Sun
    from pymeeus.Moon import Moon
    from pymeeus.Mercury import Mercury
    from pymeeus.Venus import Venus
    from pymeeus.Mars import Mars
    from pymeeus.Jupiter import Jupiter
    from pymeeus.Saturn import Saturn
    from pymeeus.Uranus import Uranus
    from pymeeus.Neptune import Neptune
    from pymeeus.Pluto import Pluto

    if key == "sun":
        res = Sun.apparent_geocentric_position(epoch_obj)
    elif key == "moon":
        res = Moon.geocentric_ecliptical_pos(epoch_obj)
    elif key == "mercury":
        res = Mercury.geocentric_position(epoch_obj)
    elif key == "venus":
        res = Venus.geocentric_position(epoch_obj)
    elif key == "mars":
        res = Mars.geocentric_position(epoch_obj)
    elif key == "jupiter":
        res = Jupiter.geocentric_position(epoch_obj)
    elif key == "saturn":
        res = Saturn.geocentric_position(epoch_obj)
    elif key == "uranus":
        res = Uranus.geocentric_position(epoch_obj)
    elif key == "neptune":
        res = Neptune.geocentric_position(epoch_obj)
    elif key == "pluto":
        res = Pluto.geocentric_position(epoch_obj)
    else:
        raise ValueError(f"unknown planet: {key}")
    lon = res[0].d if hasattr(res[0], "d") else float(res[0])
    lat = res[1].d if hasattr(res[1], "d") else float(res[1])
    return lon % 360.0, lat


def _sign_of(lon: float) -> tuple[str, str, str]:
    """(sign_key, 中文名, glyph)"""
    idx = int(math.floor(lon / 30)) % 12
    key = _SIGN_KEYS[idx]
    zh = SIGN_ZH[key]
    return key, zh[0], zh[1]


def _deg_in_sign(lon: float) -> str:
    deg = lon % 30
    d = int(math.floor(deg))
    m = int(round((deg - d) * 60))
    if m == 60:
        d += 1
        m = 0
    return f"{d}°{m:02d}′"


def _jd(year: int, month: int, day: int, hour: int, minute: int, utc_offset: float = 0.0) -> float:
    """civil date/time -> Julian Date (UT)

    Args:
        year/month/day/hour/minute: **本地墙上时间**（用户填写的出生时间）
        utc_offset: 本地时间相对 UT 的小时偏移（东八区为 8.0，西五区为 -5.0）

    Returns:
        换算到 UT 后的儒略日。

    Note:
        占星计算（恒星时、上升点、行星位置）必须以 UT 为输入。若直接把本地时间当 UT，
        东八区会让上升点整体偏移约 120°（错 4 个星座），且太阳/月亮落座误差仅 0.3°，
        极具迷惑性。utc_offset 默认 0 表示入参已是 UT。
    """
    a = (14 - month) // 12
    y = year + 4800 - a
    m = month + 12 * a - 3
    jdn = day + (153 * m + 2) // 5 + 365 * y + y // 4 - y // 100 + y // 400 - 32045
    return jdn + (hour - 12) / 24.0 + minute / 1440.0 - utc_offset / 24.0


def _utc_offset_of(birth: dict) -> float:
    """取出生地的时区偏移（小时）。

    优先用调用方传入的 utcOffset；缺失时按经度回退推算（仅兜底，精度不足——
    中国全境用东八区，而经度法会把乌鲁木齐算成 +6）。

    Args:
        birth: 含 longitude、可选 utcOffset 的出生信息字典

    Returns:
        相对 UT 的小时偏移，范围 [-12, 14]
    """
    raw = birth.get("utcOffset")
    if raw is not None:
        try:
            return max(-12.0, min(14.0, float(raw)))
        except (TypeError, ValueError):
            logger.warning(f"utcOffset 非法({raw!r})，回退经度推算")
    return max(-12.0, min(14.0, round(float(birth["longitude"]) / 15.0)))


def _j_century(jd: float) -> float:
    return (jd - 2451545.0) / 36525.0


def _sidereal(birth: dict) -> tuple[float, float]:
    """返回 (RAMC°, obliquity°)

    Note:
        入参 birthTime 是本地墙上时间，必须先按出生地时区换算为 UT，
        否则 RAMC 会整体偏移（东八区约 +120°）。LST 仍用地理经度计算，这是标准做法。
    """
    y, m, d = (int(x) for x in birth["birthDate"].split("-"))
    hh, mm = (int(x) for x in birth["birthTime"].split(":"))
    jd = _jd(y, m, d, hh, mm, _utc_offset_of(birth))
    t = (jd - 2451545.0) / 36525.0
    gmst = (280.46061837 + 360.98564736629 * (jd - 2451545.0)
            + 0.000387933 * t * t - t * t * t / 38710000.0) % 360.0
    lst = (gmst + float(birth["longitude"])) % 360.0  # RAMC
    eps = 23.4392911 - 0.0130041667 * t
    return lst, eps


def _ascendant_mc(ramc: float, eps: float, latitude: float) -> tuple[float, float]:
    """返回 (ascendant°, mc°) 黄经"""
    ramc_r = math.radians(ramc)
    eps_r = math.radians(eps)
    phi_r = math.radians(latitude)
    # 中天 MC
    mc = math.degrees(math.atan2(math.sin(ramc_r), math.cos(ramc_r) * math.cos(eps_r))) % 360.0
    # 上升 Ascendant（正切公式，需选对 180° 分支）
    denom = -(math.sin(ramc_r) * math.cos(eps_r) + math.tan(phi_r) * math.sin(eps_r))
    asc = math.degrees(math.atan2(math.cos(ramc_r), denom)) % 360.0
    # 分支校正：上升点的黄经 RA 应 ≈ RAMC + 90°
    target_ra = (ramc + 90.0) % 360.0

    def _ra_of(lon: float) -> float:
        lon_r = math.radians(lon)
        return math.degrees(math.atan2(math.sin(lon_r) * math.cos(eps_r), math.cos(lon_r))) % 360.0

    cand = [asc, (asc + 180.0) % 360.0]
    asc = min(cand, key=lambda c: abs((( _ra_of(c) - target_ra + 180) % 360) - 180))
    return asc, mc


def _retrograde(jd: float, key: str) -> bool:
    """判断星体是否逆行：前后各 0.5 天采样，黄经回退即逆行。

    Args:
        jd: 出生时刻的儒略日（UT）
        key: 星体 key

    Returns:
        True 表示逆行。判定失败时返回 False 并记录 warning。

    Note:
        先前实现用 `epoch_obj.month` 拆日期，而 pymeeus 的 Epoch 没有 .month 属性，
        抛 AttributeError 后被 `except: return False` 静默吞掉，导致逆行判定恒为 False
        （前端 ℞ 角标从未生效）。改为直接基于儒略日采样，规避该问题。
    """
    try:
        lp = _planet_lon_lat(Epoch(jd - 0.5), key)[0]
        ln = _planet_lon_lat(Epoch(jd + 0.5), key)[0]
        return ((ln - lp + 540) % 360) - 180 < 0
    except Exception as e:
        logger.warning(f"逆行判定失败 [{key}] jd={jd}: {e}")
        return False


# 相位允许度（orb）分档表：发光体最宽、外行星最窄。
# 旧实现全部星体共用 8/7/5/3，导致外行星产出大量弱相位（实测样本 21 条里 11 条
# 涉及外行星，占 52%），同时日月的宽相位被漏掉。
_ORB_BY_CLASS: dict[str, dict[int, int]] = {
    "luminary": {0: 10, 180: 10, 120: 8, 90: 8, 60: 6, 150: 3},   # 日月 + 四轴
    "personal": {0: 8, 180: 8, 120: 7, 90: 7, 60: 5, 150: 3},     # 水星 金星 火星
    "outer": {0: 6, 180: 6, 120: 5, 90: 5, 60: 4, 150: 3},        # 木土天海冥 + 虚点
}
_LUMINARY_KEYS = {"sun", "moon", "ascendant", "midheaven", "descendant", "immc"}
_PERSONAL_KEYS = {"mercury", "venus", "mars"}


def _orb_class(key: Optional[str]) -> str:
    """星体 → orb 档位。四轴按发光体处理（业界通行做法）。"""
    if key in _LUMINARY_KEYS:
        return "luminary"
    if key in _PERSONAL_KEYS:
        return "personal"
    return "outer"


def _aspect(angle: float, k1: Optional[str] = None,
            k2: Optional[str] = None) -> Optional[tuple[str, str, float]]:
    """给定两星体夹角(0-180)，返回 (type_key, 中文, orb) 或 None（含 150° 梅花相）。

    Args:
        angle: 两星体黄经夹角（0–180）
        k1/k2: 两侧星体的 key，用于查询各自的 orb 档位；缺省按 personal 处理

    Returns:
        (相位类型 key, 中文名, 偏差度) 或 None。多个相位同时满足时取偏差最小者。

    Note:
        允许度取两侧中**较宽**的一方，这是业界通行做法（发光体参与的相位更容易成立）。
    """
    t1 = _ORB_BY_CLASS[_orb_class(k1)]
    t2 = _ORB_BY_CLASS[_orb_class(k2)]
    best = None
    for key, exact in (
        ("conjunction", 0), ("opposition", 180), ("trine", 120),
        ("square", 90), ("sextile", 60), ("quincunx", 150),
    ):
        tol = max(t1[exact], t2[exact])
        orb = abs(angle - exact)
        if orb <= tol and (best is None or orb < best[2]):
            best = (key, ASPECT_ZH[key][0], round(orb, 2))
    return best


def _dignity_of(planet_key: str, sign_key: str) -> Optional[str]:
    """返回 planet 在该星座的庙旺落陷：ruler/exalt/detriment/fall/None"""
    if _DIGNITY_RULER.get(sign_key) == planet_key:
        return "ruler"
    if _DIGNITY_EXALT.get(sign_key) == planet_key:
        return "exalt"
    if _DIGNITY_RULER.get(_OPP[sign_key]) == planet_key:
        return "detriment"
    if _DIGNITY_EXALT.get(_OPP[sign_key]) == planet_key:
        return "fall"
    return None


# ============================== 小行星 / 虚点近似黄经 ==============================
def _mean_node_lon(jd: float) -> float:
    """北交点（月球升交点平黄经）Simon 1994 近似，误差 < 0.1°"""
    t = _j_century(jd)
    return (125.04452 - 1934.136261 * t + 0.0024349 * t * t) % 360.0


def _mean_lilith_lon(jd: float) -> float:
    """莉莉丝（黑月，Mean Lilith = 月球远地点）平黄经。

    采用 Meeus《Astronomical Algorithms》第 47 章的月球近地点平黄经，远地点 = 近地点 + 180°。
    周期 3232.6 天 ≈ 8.85 年。

    Note:
        旧实现用 `266.5635 + 9543.9777 * T`，该系数隐含周期仅 1377.7 天（3.77 年），
        与黑月真实周期差 2.35 倍。常数项在 J2000 附近是对的（与 Meeus 差 3.2°），
        因此错的是速率项：误差随 |T| 线性放大，1990 年已达 175°、2010 年 169°。
        实测锚点：2026-08-04 权威天象预报记「Lilith in Sagittarius」，
        旧公式给出摩羯座 14°（错），本公式给出射手座 25°（对）。

    Args:
        jd: 目标时刻儒略日

    Returns:
        黑月平黄经°，误差约 ±0.5°（Mean 口径，True Lilith 因月球轨道振荡会偏离更多）
    """
    t = _j_century(jd)
    perigee = (83.3532 + 4069.0137 * t - 0.01043 * t * t) % 360.0
    return (perigee + 180.0) % 360.0


# 凯龙星（2060 Chiron）轨道要素，J2000 平黄道 / 历元 2000-01-01.5 TT
# 来源：JPL Small-Body Database 轨道要素（a/e/i/Ω/ω），周期取 50.42 年
_CHIRON_ORB: dict[str, float] = {
    "a": 13.6335,      # 半长轴 AU
    "e": 0.3802,       # 偏心率（很大，线性外推必然失真）
    "i": 6.9285,       # 轨道倾角 deg
    "Om": 209.3765,    # 升交点黄经 deg
    "w": 339.3174,     # 近日点argument deg
    "P": 50.42,        # 公转周期（年）
    "Tp": -1.0,        # 近日点通过时刻 JD，模块加载时计算
}


def _kepler_state(jd: float, el: dict) -> tuple[float, float]:
    """二体椭圆轨道 → (日心黄经°, 日心向径 AU)。

    牛顿迭代解开普勒方程 E - e·sin(E) = M，再做完整的 轨道平面 → 黄道 三维旋转。

    Args:
        jd: 目标时刻儒略日
        el: 轨道要素字典（a/e/i/Om/w/P/Tp）

    Returns:
        (日心黄经°, 日心向径 AU)
    """
    M = math.radians((360.0 * (jd - el["Tp"]) / (el["P"] * 365.25)) % 360.0)
    e = el["e"]
    E = M
    for _ in range(15):  # e=0.38 时 5~6 次即收敛，15 次留足余量
        E -= (E - e * math.sin(E) - M) / (1 - e * math.cos(E))
    xv = el["a"] * (math.cos(E) - e)
    yv = el["a"] * math.sqrt(1 - e * e) * math.sin(E)
    r = el["a"] * (1 - e * math.cos(E))          # 向径
    w, Om, inc = math.radians(el["w"]), math.radians(el["Om"]), math.radians(el["i"])
    cw, sw, cO, sO, ci = math.cos(w), math.sin(w), math.cos(Om), math.sin(Om), math.cos(inc)
    x = (cw * cO - sw * sO * ci) * xv + (-sw * cO - cw * sO * ci) * yv
    y = (cw * sO + sw * cO * ci) * xv + (-sw * sO + cw * cO * ci) * yv
    return math.degrees(math.atan2(y, x)) % 360.0, r


def _chiron_lon(jd: float) -> float:
    """凯龙星地心黄经。

    日心凯龙矢量 − 日心地球矢量，再投影回黄道。地球日心位置按「太阳地心黄经 + 180°、
    向径 1 AU」近似（忽略太阳黄纬与地球轨道偏心率，对 13.6 AU 外的目标影响 < 0.1°）。

    Note:
        旧实现为 `(209.597 + 7.118 * (year - 2000)) % 360` —— 线性外推且**只吃年份**。
        凯龙偏心率 0.38，近日点约 24°/年、远日点约 3°/年，平均 7.14°/年只在整周期上成立；
        实测对照权威过座星历表 14 个采样点**错 12 个**，1990–2010 年出生的用户几乎全错。
        本实现实测 14/14 全对，且支持按日计算（旧实现同一年任何日期同值）。

    Args:
        jd: 目标时刻儒略日（**必须传完整 JD，不是年份**）

    Returns:
        凯龙地心黄经°
    """
    if _CHIRON_ORB["Tp"] < 0:
        _CHIRON_ORB["Tp"] = _jd(1996, 2, 14, 0, 0, 0)   # 1996 年近日点通过
    cl, cr = _kepler_state(jd, _CHIRON_ORB)
    ea = math.radians(cl)
    el = math.radians((_planet_lon_lat(Epoch(jd), "sun")[0] + 180.0) % 360.0)
    x = cr * math.cos(ea) - 1.0 * math.cos(el)
    y = cr * math.sin(ea) - 1.0 * math.sin(el)
    return math.degrees(math.atan2(y, x)) % 360.0


# ============================== 本命盘计算 ==============================
def compute_natal(birth: dict, house_system: str = "equal") -> dict:
    y, m, d = (int(x) for x in birth["birthDate"].split("-"))
    hh, mi = (0, 0)
    unknown_time = bool(birth.get("unknownTime")) or not birth.get("birthTime")
    if not unknown_time:
        hh, mi = (int(x) for x in birth["birthTime"].split(":"))
    lat = float(birth["latitude"])
    lon = float(birth["longitude"])
    # 本地墙上时间 → UT：行星位置与恒星时都必须基于 UT 计算
    utc_offset = _utc_offset_of(birth)
    jd = _jd(y, m, d, hh, mi, utc_offset)
    epoch = Epoch(jd)

    asc = mc = None
    if not unknown_time:
        ramc, eps = _sidereal(birth)
        asc, mc = _ascendant_mc(ramc, eps, lat)

    # 宫头计算
    def house_cusp(i: int) -> float:
        if house_system == "whole":
            # 整宫制：第 1 宫头 = ASC 所在星座 0°
            base = int(math.floor((asc if asc is not None else 0) / 30)) * 30
            return (base + i * 30) % 360.0
        # 默认 equal（等宫制）
        return (asc + i * 30) % 360.0 if asc is not None else (i * 30) % 360.0

    def house_of(plon: float) -> Optional[int]:
        """行星落宫。

        Note:
            必须与 house_cusp 采用同一套宫制。此前整宫制下仍走等宫制公式，
            导致行星落宫与宫头星座自相矛盾（10 颗行星错 8 颗）。
        """
        if asc is None:
            return None
        if house_system == "whole":
            # 整宫制：星座即宫位，按行星所在星座相对 ASC 星座的偏移定宫
            return ((int(math.floor(plon / 30)) - int(math.floor(asc / 30))) % 12) + 1
        # equal（等宫制）：从 ASC 起每 30° 一宫
        return int(math.floor(((plon - asc + 360) % 360) / 30)) + 1

    planets: list[dict] = []
    for key in KNOWN_PLANETS:
        plon, plat = _planet_lon_lat(epoch, key)
        zh = PLANET_ZH[key]
        skey, sname, sglyph = _sign_of(plon)
        retrograde = _retrograde(jd, key)
        planets.append({
            "key": key,
            "label": zh[0],
            "glyph": zh[1],
            "signKey": skey,
            "sign": sname,
            "signGlyph": sglyph,
            "longitude": round(plon, 4),
            "degreeInSign": _deg_in_sign(plon),
            "house": house_of(plon),
            "retrograde": retrograde,
            "dignity": _dignity_of(key, skey),
        })

    sun_body = next(p for p in planets if p["key"] == "sun")
    moon_body = next(p for p in planets if p["key"] == "moon")

    asc_sign = _sign_of(asc) if asc is not None else None
    mc_sign = _sign_of(mc) if mc is not None else None

    # 宫位列表
    houses = []
    for i in range(12):
        cusp = house_cusp(i)
        skey, sname, sglyph = _sign_of(cusp)
        houses.append({
            "num": i + 1,
            "name": HOUSE_NAMES[i],
            "theme": HOUSE_THEMES[i],
            "signKey": skey,
            "sign": sname,
            "signGlyph": sglyph,
            "cusp": round(cusp, 4),
        })

    # 小行星 / 虚点
    extra_points: list[dict] = []
    for sp in SMALL_POINTS:
        if sp["key"] == "northnode":
            elon = _mean_node_lon(jd)
        elif sp["key"] == "southnode":
            elon = (_mean_node_lon(jd) + 180.0) % 360.0
        elif sp["key"] == "lilith":
            elon = _mean_lilith_lon(jd)
        else:
            elon = _chiron_lon(jd)
        skey, sname, sglyph = _sign_of(elon)
        extra_points.append({
            "key": sp["key"],
            "label": sp["label"],
            "glyph": sp["glyph"],
            "signKey": skey,
            "sign": sname,
            "signGlyph": sglyph,
            "longitude": round(elon, 4),
            "degreeInSign": _deg_in_sign(elon),
            "house": house_of(elon),
        })

    # 四轴
    immc = desc = None
    if asc is not None and mc is not None:
        immc_lon = (mc + 180) % 360.0
        desc_lon = (asc + 180) % 360.0
        immc_sign = _sign_of(immc_lon)
        desc_sign = _sign_of(desc_lon)
        immc = {"sign": immc_sign[1], "signGlyph": immc_sign[2], "degreeInSign": _deg_in_sign(immc_lon), "longitude": round(immc_lon, 4)}
        desc = {"sign": desc_sign[1], "signGlyph": desc_sign[2], "degreeInSign": _deg_in_sign(desc_lon), "longitude": round(desc_lon, 4)}

    # 相位（星体 + 上升 + 中天；含 150° 梅花相）
    points = list(planets)
    if asc is not None:
        points += [
            {"key": "ascendant", "label": "上升", "glyph": "ASC", "longitude": asc},
            {"key": "midheaven", "label": "中天", "glyph": "MC", "longitude": mc},
        ]
    aspects = []
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            a, b = points[i], points[j]
            diff = abs(((a["longitude"] - b["longitude"] + 180) % 360) - 180)
            asp = _aspect(diff, a["key"], b["key"])
            if asp:
                aspects.append({
                    "p1Key": a["key"], "p1": a["label"],
                    "p2Key": b["key"], "p2": b["label"],
                    "typeKey": asp[0], "type": asp[1], "level": "major", "orb": asp[2],
                })
    aspects.sort(key=lambda x: x["orb"])

    # 四象元素分布
    elements: dict[str, int] = {"火": 0, "土": 0, "风": 0, "水": 0}
    for p in planets:
        el = SIGN_ELEMENT.get(p["signKey"])
        if el:
            elements[el] += 1

    chart = {
        "birth": {
            "birthDate": birth["birthDate"], "birthTime": birth.get("birthTime") or "",
            "latitude": lat, "longitude": lon, "unknownTime": unknown_time,
        },
        "houseSystem": house_system,
        "ascendant": (
            {"sign": asc_sign[1], "signGlyph": asc_sign[2], "degreeInSign": _deg_in_sign(asc), "longitude": round(asc, 4)}
            if asc is not None else None
        ),
        "midheaven": (
            {"sign": mc_sign[1], "signGlyph": mc_sign[2], "degreeInSign": _deg_in_sign(mc), "longitude": round(mc, 4)}
            if mc is not None else None
        ),
        "immc": immc,
        "descendant": desc,
        "sunSign": {"key": sun_body["signKey"], "sign": sun_body["sign"], "signGlyph": sun_body["signGlyph"]},
        "moonSign": {"key": moon_body["signKey"], "sign": moon_body["sign"], "signGlyph": moon_body["signGlyph"]},
        "planets": planets,
        "extraPoints": extra_points,
        "houses": houses,
        "aspects": aspects,
        "elements": elements,
        "summary": _build_natal_summary(asc, mc, immc, desc, sun_body, moon_body, planets, extra_points, aspects, unknown_time),
    }
    return chart


def _build_natal_summary(asc, mc, immc, desc, sun_body, moon_body, planets, extra_points, aspects, unknown_time) -> str:
    lines: list[str] = []
    if asc is None:
        lines.append("（出生时间未知 · 日间盘：仅太阳、月亮与主要星体落座有效，宫位与四轴隐藏）")
        lines.append(f"太阳：{sun_body['sign']}　月亮：{moon_body['sign']}")
    else:
        lines.append(f"上升星座：{_sign_of(asc)[1]}（{_deg_in_sign(asc)}）")
        lines.append(f"中天（MC）：{_sign_of(mc)[1]}（{_deg_in_sign(mc)}）")
        lines.append(f"天底（IC）：{_sign_of(immc['longitude'])[1]}（{_deg_in_sign(immc['longitude'])}）")
        lines.append(f"下降（DSC）：{_sign_of(desc['longitude'])[1]}（{_deg_in_sign(desc['longitude'])}）")
        lines.append(f"太阳：{sun_body['sign']}　月亮：{moon_body['sign']}")
    for b in planets:
        dn = f"（{_DIGNITY_LABEL.get(b['dignity'], '')}）" if b["dignity"] else ""
        lines.append(
            f"{b['label']}（{b['glyph']}）落{b['sign']}　第{b['house']}宫{b['retrograde'] and '（逆行）' or ''}{dn}"
        )
    for b in extra_points:
        lines.append(f"{b['label']}（{b['glyph']}）落{b['sign']}　第{b['house']}宫")
    if aspects:
        lines.append("主要相位：" + "、".join(
            f"{a['p1']}{a['type']}{a['p2']}({a['orb']}°)" for a in aspects[:8]
        ))
    return "\n".join(lines)


_DIGNITY_LABEL: dict[str, str] = {
    "ruler": "入庙", "exalt": "曜升", "detriment": "失势", "fall": "落陷",
}


# ============================== 流年行星位置 ==============================
def _transit_jd(ref_date: datetime, utc_offset: float = 0.0) -> float:
    """行运参考时刻：本地参考日 12:00 → UT 儒略日。

    Args:
        ref_date: 参考日期（**本地日期**，来自 datetime.now()）
        utc_offset: 出生地时区相对 UT 的小时偏移

    Returns:
        换算到 UT 的儒略日。

    Note:
        必须与 compute_natal 用同一个 utc_offset。否则东八区下流年月亮会偏约 4.4°
        （月亮 13°/天 × 8h），而相位允许度仅 8° —— 实测会漏报 21 条、误报 3 条交相。
    """
    return _jd(ref_date.year, ref_date.month, ref_date.day, 12, 0, utc_offset)


def compute_transits(ref_date: datetime, latitude: float, longitude: float,
                     utc_offset: float = 0.0) -> str:
    """流年行星所在星座的文字描述（本地参考日 12:00，按出生地时区换算为 UT）。"""
    jd = _transit_jd(ref_date, utc_offset)
    parts = []
    for k in TRANSIT_PLANETS:
        plon, _ = _planet_lon_lat(Epoch(jd), k)
        parts.append(f"{PLANET_ZH[k][0]}在{_sign_of(plon)[1]}")
    return "，".join(parts)


def compute_transit_aspects(chart: dict, ref_date: datetime, latitude: float, longitude: float,
                            utc_offset: float = 0.0) -> list[dict]:
    """计算当前天象(transit)对本命盘的真实交相（transit-to-natal aspects）

    Args:
        chart: 本命盘（compute_natal 的产物）
        ref_date: 参考日期（本地日期）
        latitude/longitude: 出生地经纬度
        utc_offset: 出生地时区偏移（小时）。**必须传**，否则流年月亮会偏约 4.4°

    Returns:
        交相列表，按 orb 升序，最多 14 条。
    """
    jd = _transit_jd(ref_date, utc_offset)
    tlon: dict[str, float] = {}
    for k in TRANSIT_PLANETS:
        tlon[k] = _planet_lon_lat(Epoch(jd), k)[0]
    tlon["northnode"] = _mean_node_lon(jd)

    natal_pts: list[tuple[str, str, float]] = [(p["key"], p["label"], p["longitude"]) for p in chart["planets"]]
    if chart.get("ascendant"):
        natal_pts += [
            ("ascendant", "上升", chart["ascendant"]["longitude"]),
            ("descendant", "下降", chart["descendant"]["longitude"]),
            ("midheaven", "中天", chart["midheaven"]["longitude"]),
            ("immc", "天底", chart["immc"]["longitude"]),
        ]

    res: list[dict] = []
    for tk, tlo in tlon.items():
        tzh = PLANET_ZH.get(tk, ["北交点", "☊"])[0]
        for nk, nzh, nlo in natal_pts:
            diff = abs(((tlo - nlo + 180) % 360) - 180)
            asp = _aspect(diff, tk, nk)
            if asp:
                res.append({
                    "transitKey": tk, "transit": tzh,
                    "natalKey": nk, "natal": nzh,
                    "typeKey": asp[0], "type": asp[1], "orb": asp[2],
                })
    res.sort(key=lambda x: x["orb"])
    return res[:14]


# ============================== Prompt ==============================
NATAL_SYSTEM = """你是一位严谨的专业占星师，精通本命盘（Natal Chart）解读。
用户提供了通过天文星历精确计算的本命盘数据（含上升、中天、十大星体落座与落宫、庙旺落陷、主要相位、四象元素分布）。
请基于这些数据，用专业、温暖且易懂的中文撰写本命盘解读。要求：
- overview：整体特质与人生主题的宏观概述（2-4 句）。
- personality：性格特质、天赋与内在矛盾（结合太阳/月亮/上升，以及关键相位与庙旺落陷）。
- love：情感模式、亲密关系与吸引力（结合金星、月亮、5/7/8 宫相关落点）。
- career：事业天赋、财富观与适合的发展方向（结合太阳、土星、10 宫、2 宫相关落点）。
- health：身心特质与需要注意的健康倾向（结合 1/6 宫、火星、土星落点，仅作养生提示，不作诊断）。
- advice：3-5 条针对该命盘的人生发展建议（每条一句，具体可执行）。
必须只输出一个 JSON 对象，结构严格为：
{"overview":"","personality":"","love":"","career":"","health":"","advice":["","",...]}
不要输出任何额外文字、解释或 markdown 代码块标记。内容须积极、尊重，避免宿命论与绝对化断言。"""


def _forecast_system(period: str) -> str:
    span = {"daily": "未来 1 天", "weekly": "未来 1 周", "monthly": "未来 1 个月", "yearly": "未来 1 年"}.get(period, "未来 1 个月")
    return f"""你是一位严谨的专业占星师。用户提供了其精确计算的本命盘，以及当前真实天象（流年行星对本命盘的交相相位，已给出）。
请基于「本命盘特质 + 当前真实天象交相」为用户预测{span}的运势，用专业、温暖、具体的中文撰写。
要求：
- overview：本周期整体运势基调（2-3 句），点明本期最重要的天象主题。
- love：爱情 / 人际情感方面的详细分析与提示。
- career：事业 / 学业 / 财富方面的详细分析与机会点。
- health：身心状态与养生提示（不作医疗诊断）。
- advice：3-5 条本周期的具体行动建议（每条一句）。
- luckyNumbers：2-4 个本周期幸运数字（1-99 之间）。
- luckyColors：1-3 个本周期幸运颜色（中文名称，如"红色""金色"）。
- luckyDirection：本周期幸运方位（东/南/西/北/东南/东北/西南/西北 之一）。
必须只输出一个 JSON 对象，结构严格为：
{{"overview":"","love":"","career":"","health":"","advice":["","",...],"luckyNumbers":[1,2],"luckyColors":["红色"],"luckyDirection":"东方"}}
不要输出任何额外文字、解释或 markdown 代码块标记。内容须积极、尊重，避免宿命论与绝对化断言。"""


SR_SYSTEM = """你是一位严谨的专业占星师，精通太阳返照盘（Solar Return）解读。
用户提供了通过天文星历精确计算的太阳返照盘数据（太阳回到出生太阳黄经的那一刻、按出生地重排的年度星盘，含上升、中天、十大星体落座与落宫、庙旺落陷、主要相位、四象元素分布）。
请基于这些数据，用专业、温暖且易懂的中文撰写该年度的主题解读。要求：
- overview：本年度整体人生主题与能量基调的宏观概述（2-4 句），点明返照盘相对本命盘最值得关注的变化。
- personality：本年度被激活的性格面向与内在课题（结合太阳/月亮/上升在返照盘的位置，以及关键相位与庙旺落陷）。
- love：本年度情感模式、亲密关系与吸引力的主题（结合金星、月亮、5/7/8 宫相关落点）。
- career：本年度事业天赋、财富机遇与适合推进的方向（结合太阳、土星、10 宫、2 宫相关落点）。
- health：本年度身心特质与需要注意的健康倾向（结合 1/6 宫、火星、土星落点，仅作养生提示，不作诊断）。
- advice：3-5 条针对该年度的人生发展建议（每条一句，具体可执行）。
必须只输出一个 JSON 对象，结构严格为：
{"overview":"","personality":"","love":"","career":"","health":"","advice":["","",...]}
不要输出任何额外文字、解释或 markdown 代码块标记。内容须积极、尊重，避免宿命论与绝对化断言。"""


# ============================== 本地降级 ==============================
def _local_natal(c: dict) -> dict:
    sun = c["sunSign"]["sign"]
    moon = c["moonSign"]["sign"]
    asc = c["ascendant"]["sign"] if c.get("ascendant") else "（时间未知）"
    return {
        "overview": f"你的本命盘显示：太阳{sun}、月亮{moon}、上升{asc}。这套配置勾勒出你独特的人生主题与能量底色。",
        "personality": f"上升{asc}塑造了你给人的第一印象与处事风格；太阳{sun}代表你的核心自我与人生动力；月亮{moon}则掌管你的情绪与安全感的来源。三者结合，形成你性格中理性与感性交织的层次。",
        "love": f"{moon}影响着你在亲密关系中的情感需求与表达方式。具体恋爱观与吸引力，还需结合金星与 5/7 宫落点综合判断。",
        "career": f"太阳{sun}所在宫位与星座暗示了你的天赋领域与事业驱动力；土星与第 10 宫则关系到长期成就与社会角色。",
        "health": "1 宫（命宫）与 6 宫（奴仆宫）的星体落点提示你的身心特质；请将其作为养生参考，任何具体健康问题务必咨询专业医师。",
        "advice": [
            f"发挥太阳{sun}的核心优势，把它作为长期发展的主线。",
            f"留意月亮{moon}的情绪周期，给自己留出情绪缓冲空间。",
            f"借助上升{asc}的外显气质，在社交与机遇中自然展现自己。",
            "定期运动与规律作息，呼应命盘对身心平衡的重视。",
        ],
    }


_LUCKY_MAP = {
    "白羊座": {"nums": [1, 9], "colors": ["红色"], "dir": "东方"},
    "金牛座": {"nums": [6, 18], "colors": ["金色"], "dir": "东北"},
    "双子座": {"nums": [5, 14], "colors": ["黄色"], "dir": "东南"},
    "巨蟹座": {"nums": [2, 7], "colors": ["银白色"], "dir": "正北"},
    "狮子座": {"nums": [1, 8], "colors": ["橙色"], "dir": "正东"},
    "处女座": {"nums": [5, 14], "colors": ["藏青色"], "dir": "正南"},
    "天秤座": {"nums": [6, 9], "colors": ["粉色"], "dir": "西南"},
    "天蝎座": {"nums": [4, 13], "colors": ["深红色"], "dir": "正北"},
    "射手座": {"nums": [3, 7], "colors": ["紫色"], "dir": "正南"},
    "摩羯座": {"nums": [8, 10], "colors": ["黑色"], "dir": "正东"},
    "水瓶座": {"nums": [4, 11], "colors": ["蓝绿色"], "dir": "东北"},
    "双鱼座": {"nums": [7, 12], "colors": ["海蓝色"], "dir": "东南"},
}


def _local_forecast(c: dict, period: str) -> dict:
    span = {"daily": "今日", "weekly": "本周", "monthly": "本月", "yearly": "本年"}.get(period, "本月")
    sign = c["sunSign"]["sign"]
    lucky = _LUCKY_MAP.get(sign, {"nums": [3, 7], "colors": ["紫色"], "dir": "正南"})
    return {
        "overview": f"{span}整体运势平稳，建议你以本命盘{sign}的核心特质为锚，理性安排节奏。",
        "love": f"情感方面宜多倾听与表达。若有伴侣，小小的举动能升温关系；若单身，{span}适合在熟悉的圈子自然接触。",
        "career": f"事业上保持专注，{span}适合推进既有项目而非盲目开拓。善用你太阳{sign}的优势积累成果。",
        "health": "注意作息规律与情绪调节，把命盘提示的身心特质当作养生参考，必要时就医。",
        "advice": [
            f"{span}设定 1-2 个明确小目标并落实。",
            "在人际中多一点主动与耐心。",
            "预留休息时间，避免过度消耗。",
        ],
        "luckyNumbers": lucky["nums"],
        "luckyColors": lucky["colors"],
        "luckyDirection": lucky["dir"],
    }


# ============================== 编排入口 ==============================
def _base_meta(request_id: str, atype: str, period: str, start: int, o: dict) -> dict:
    return {
        "requestId": request_id,
        "type": atype,
        "period": period if period else None,
        "promptVersion": "astro_v2",
        "provider": o.get("provider", "local-rules"),
        "model": o.get("model", "local-rules"),
        "cacheHit": o.get("cacheHit", False),
        "degraded": o.get("degraded", True),
        "degradedReason": o.get("degradedReason"),
        "tokens": o.get("tokens"),
        "costYuan": o.get("cost", 0.0),
        "latencyMs": int((time.time() * 1000) - start),
    }


def _ref_date(period: str) -> datetime:
    now = datetime.now()
    if period == "weekly":
        return now + timedelta(days=3)
    if period == "monthly":
        return now + timedelta(days=15)
    if period == "yearly":
        return now + timedelta(days=180)
    return now


async def interpret_astrology(
    atype: str,
    birth: dict,
    period: str = "daily",
    natal: Optional[dict] = None,
    house_system: str = "equal",
    salt: str = "",
) -> dict:
    """编排一次占星解读（本命盘或运势）。

    Args:
        atype: "natal" 或 "forecast"
        birth: 出生信息字典
        period: 运势周期 daily/weekly/monthly/yearly
        natal: 可选，复用已算好的本命盘
        house_system: 宫制。**必须与前端展示用的宫制一致**，否则会出现
            「屏幕显示第 6 宫、AI 解读按第 5 宫」的自相矛盾。
    """
    request_id = str(uuid.uuid4())
    start = time.time() * 1000

    chart = natal if natal is not None else compute_natal(birth, house_system)

    if atype == "natal":
        system = NATAL_SYSTEM
        user = f"以下是用户精确计算的本命盘数据，请据此解读：\n\n{chart['summary']}"
        cache_type = "natal"
        extra = {}
    else:
        rd = _ref_date(period)
        transit_aspects = compute_transit_aspects(
            chart, rd, float(birth["latitude"]), float(birth["longitude"]),
            _utc_offset_of(birth),
        )
        transit_text = "；".join(
            f"{a['transit']}{a['type']}本命{a['natal']}({a['orb']}°)" for a in transit_aspects
        ) or "本期无强天象交相"
        ref_str = rd.strftime("%Y-%m-%d")
        span = {"daily": "今日", "weekly": "本周", "monthly": "本月", "yearly": "本年"}.get(period, "本月")
        system = _forecast_system(period)
        user = (
            f"用户本命盘（精确计算）：\n{chart['summary']}\n\n"
            f"当前真实天象（{span}参考日期 {ref_str}，流年行星对本命盘交相）：\n{transit_text}\n\n"
            f"请结合用户本命盘特质与上述真实天象交相，预测其{span}运势（爱情 / 事业 / 健康 / 建议）。"
        )
        cache_type = f"forecast:{period}"
        extra = {"transits": transit_aspects}

    # 用 md5 而非内置 hash()：Python str hash 受 PYTHONHASHSEED 随机化影响，
    # 同一输入在不同进程里键不同（AI 服务是 reload 模式，改一次代码缓存全部失效）
    cache_key = f"astro:{cache_type}:{hashlib.md5((user + '|' + salt).encode('utf-8')).hexdigest()[:12]}"
    cached = cache_get(cache_key)
    if cached:
        try:
            return {
                "data": json.loads(cached),
                "disclaimer": DISCLAIMER,
                "meta": _base_meta(request_id, atype, period, start,
                                  {"cacheHit": True, "degraded": False, "provider": "cache", "model": "cache", "tokens": None, "cost": 0}),
                **extra,
            }
        except Exception:
            pass

    if budget_broken():
        fb = _local_natal(chart) if atype == "natal" else _local_forecast(chart, period)
        return {
            "data": fb,
            "disclaimer": DISCLAIMER,
            "meta": _base_meta(request_id, atype, period, start,
                              {"degraded": True, "degradedReason": "单日预算超限，本地规则降级", "provider": "local-rules", "model": "local-rules", "tokens": None, "cost": 0}),
            **extra,
        }

    parsed, err, tokens, provider, model = await generate_structured(
        system, user, temperature=0.75, max_tokens=1500, retries=1
    )
    if parsed is not None:
        maybe_quality_check("astro_natal", json.dumps(parsed, ensure_ascii=False))
    if parsed is None:
        fb = _local_natal(chart) if atype == "natal" else _local_forecast(chart, period)
        return {
            "data": fb,
            "disclaimer": DISCLAIMER,
            "meta": _base_meta(request_id, atype, period, start,
                              {"degraded": True, "degradedReason": "LLM 生成失败，本地规则降级", "provider": "local-rules", "model": "local-rules", "tokens": None, "cost": 0}),
            **extra,
        }

    cost = estimate_cost(tokens) if tokens else 0.0
    record_cost(cost)
    cache_set(cache_key, json.dumps(parsed, ensure_ascii=False), ttl_seconds=3600)

    return {
        "data": parsed,
        "disclaimer": DISCLAIMER,
        "meta": _base_meta(request_id, atype, period, start,
                          {"degraded": False, "degradedReason": None, "provider": provider, "model": model, "tokens": tokens, "cost": cost}),
        **extra,
    }


# ============================== 星座配对 / 合盘（Synastry） ==============================
# 合盘相位评分（与 _aspect 的相位类型对应）。orb 分级已交给 _aspect 按星体类别处理，
# 这里只保留「哪种相位加分 / 减分」，避免与本命盘的分级逻辑重复。
_SYN_SCORE = {
    "conjunction": 5, "opposition": -3, "trine": 6, "square": -4, "sextile": 4, "quincunx": -1,
}


def _cross_angle(lon1: float, lon2: float) -> float:
    diff = abs(lon1 - lon2) % 360
    if diff > 180:
        diff = 360 - diff
    return diff


def _midpoint_lon(a: float, b: float) -> float:
    """两黄经沿短弧中点"""
    d = ((b - a + 540) % 360) - 180
    return (a + d / 2) % 360.0


def _cp_lon(chart: dict, key: str) -> Optional[float]:
    if key in ("ascendant", "midheaven", "immc", "descendant"):
        return chart.get(key, {}).get("longitude") if chart.get(key) else None
    return next((p["longitude"] for p in chart["planets"] if p["key"] == key), None)


def compute_composite(c1: dict, c2: dict) -> dict:
    """组合盘（中点法 Composite）：关系本身的中天/上升与各星体中点"""
    def mid(key: str) -> Optional[float]:
        a = _cp_lon(c1, key)
        b = _cp_lon(c2, key)
        if a is None or b is None:
            return None
        return _midpoint_lon(a, b)

    comp: dict[str, Any] = {}
    for key in ["sun", "moon", "venus", "mars", "ascendant", "midheaven"]:
        ml = mid(key)
        if ml is None:
            comp[key] = None
            continue
        sk, sn, sg = _sign_of(ml)
        comp[key] = {"sign": sn, "signGlyph": sg, "degreeInSign": _deg_in_sign(ml), "longitude": round(ml, 4)}
    # 关系内部主要相位
    pts = [(k, comp[k]["longitude"]) for k in ["sun", "moon", "venus", "mars", "ascendant", "midheaven"] if comp.get(k)]
    comp_aspects = []
    for i in range(len(pts)):
        for j in range(i + 1, len(pts)):
            diff = abs(((pts[i][1] - pts[j][1] + 180) % 360) - 180)
            asp = _aspect(diff, pts[i][0], pts[j][0])
            if asp:
                comp_aspects.append({"p1": pts[i][0], "p2": pts[j][0], "type": asp[1], "orb": asp[2]})
    comp["aspects"] = comp_aspects
    return comp


def compute_synastry(birth1: dict, birth2: dict, house_system: str = "equal") -> dict:
    """比较盘（Synastry）：双方星体两两交相 + 配对指数 + 组合盘。

    Args:
        birth1/birth2: 双方出生信息
        house_system: 宫制，需与前端展示一致

    Returns:
        含 aspects / score / composite 等的字典。五段解读文案为本地模板，
        真正的 AI 解读见 interpret_synastry()。
    """
    c1 = compute_natal(birth1, house_system)
    c2 = compute_natal(birth2, house_system)

    aspects: list[dict] = []
    total = 50
    for p1 in c1["planets"]:
        for p2 in c2["planets"]:
            diff = _cross_angle(p1["longitude"], p2["longitude"])
            # 复用 _aspect：按星体类别分级 orb（日月最宽、外行星最窄），
            # 此前合盘共用统一 orb，会漏掉日月的宽相位、又产出大量外行星弱相位。
            asp = _aspect(diff, p1["key"], p2["key"])
            if asp:
                total += _SYN_SCORE.get(asp[0], 0)
                aspects.append({
                    "p1Key": p1["key"], "p1": p1["label"],
                    "p2Key": p2["key"], "p2": p2["label"],
                    "type": asp[0], "label": asp[1], "orb": asp[2],
                })

    score = max(0, min(100, total))

    def _asc_text(c: dict) -> str:
        """上升星座文案。日间盘（出生时间未知）下 ascendant 为 None，需降级。"""
        return c["ascendant"]["sign"] if c.get("ascendant") else "未知（出生时间未提供）"

    summary = (
        f"双方太阳分别为 {c1['sunSign']['sign']} 与 {c2['sunSign']['sign']}，"
        f"月亮为 {c1['moonSign']['sign']} 与 {c2['moonSign']['sign']}，"
        f"上升为 {_asc_text(c1)} 与 {_asc_text(c2)}。"
        f"配对指数约 {score}/100。{'总体和谐，互补性强。' if score >= 60 else '存在张力，需更多包容与沟通。'}"
    )

    composite = compute_composite(c1, c2)

    base = {
        "chart1": {"sun": c1["sunSign"]["sign"], "moon": c1["moonSign"]["sign"], "ascendant": _asc_text(c1)},
        "chart2": {"sun": c2["sunSign"]["sign"], "moon": c2["moonSign"]["sign"], "ascendant": _asc_text(c2)},
        "aspects": aspects,
        "score": score,
        "summary": summary,
        "composite": composite,
    }
    # 本地规则文案兜底（AI 不可用时保证结构完整，会被 interpret_synastry 覆盖）
    base.update(_synastry_local(base))
    return base


def _synastry_local(syn: dict) -> dict:
    """合盘五段解读的本地降级文案。

    仅作兜底：内容与任何一对具体星盘无关，AI 不可用时使用。
    """
    return {
        "compatibility": syn.get("summary", ""),
        "love": "情感互动请结合双方金星与月亮落点综合判断。",
        "communication": "沟通风格受水星与上升影响，差异可作为互补的契机。",
        "conflict": "主要张力来自硬相位（刑/冲），建议以温和沟通化解。",
        "advice": [
            "欣赏彼此星座特质中的互补面。",
            "在差异出现时先倾听再回应。",
            "共同规划可落地的相处小目标。",
        ],
    }


SYNASTRY_SYSTEM = """你是一位严谨的专业占星师，精通合盘（Synastry）与组合盘（Composite）分析。
用户提供了两个人经天文星历精确计算的本命盘、双方星体的交叉相位（比较盘），以及两人星体的中点组合盘。
请基于这些数据，用专业、温暖且具体的中文撰写配对分析。要求：
- compatibility：整体契合度的宏观判断（2-4 句）。
- love：情感与亲密关系的互动模式（结合双方金星、月亮、5/7 宫相关落点与交叉相位）。
- communication：沟通与心智层面的契合（结合水星互动、双方上升与月亮的互动）。
- conflict：主要的张力来源与化解方式（结合硬相位刑/冲、土星与冥王星带来的课题）。
- advice：3-5 条针对这段关系的具体建议（每条一句，可执行）。
硬性要求：
- **必须点名具体的星体互动**（如"他的金星三分她的月亮，情感表达天然顺畅"），
  禁止输出"请结合……综合判断"这类空泛套话。
- 引用相位时只使用输入数据中真实存在的相位，不得编造。
- 内容须积极、尊重，避免宿命论与绝对化断言，不评判这段关系"好不好"。
必须只输出一个 JSON 对象，结构严格为：
{"compatibility":"","love":"","communication":"","conflict":"","advice":["","",...]}
不要输出任何额外文字、解释或 markdown 代码块标记。"""


def _synastry_user_prompt(syn: dict, c1: dict, c2: dict) -> str:
    """构造合盘解读的 user prompt：双方盘 + 交叉相位 + 组合盘。

    只喂 Top-N 相位，避免 prompt 过长（100 对星体最多可产生 40+ 条交相）。
    """
    asp_lines = [
        f"- A {a['p1']} {a['label']} B {a['p2']}（容许度 {a['orb']}°）"
        for a in syn["aspects"][:20]
    ] or ["- 双方星体间无紧密交相"]
    comp = syn.get("composite") or {}
    comp_parts = []
    for k, zh in [("sun", "关系太阳"), ("moon", "关系月亮"), ("venus", "关系金星"),
                  ("mars", "关系火星"), ("ascendant", "关系上升"), ("midheaven", "关系中天")]:
        v = comp.get(k)
        if v:
            comp_parts.append(f"{zh} {v['sign']}{v['degreeInSign']}")
    comp_lines = "、".join(comp_parts) or "（组合盘数据不足）"
    comp_asp = comp.get("aspects") or []
    comp_asp_lines = [
        f"- {a['p1']} {a['type']} {a['p2']}（{a['orb']}°）" for a in comp_asp[:6]
    ]
    return (
        f"A 方本命盘：\n{c1['summary']}\n\n"
        f"B 方本命盘：\n{c2['summary']}\n\n"
        f"双方交叉相位（比较盘，共 {len(syn['aspects'])} 条，按紧密程度展示前 "
        f"{min(20, len(syn['aspects']))} 条）：\n" + "\n".join(asp_lines) + "\n\n"
        f"组合盘（中点法，代表这段关系本身）：{comp_lines}\n"
        + ("组合盘内部相位：\n" + "\n".join(comp_asp_lines) + "\n" if comp_asp_lines else "")
        + f"\n配对指数（服务端按相位加权计算，仅供参考）：{syn['score']}/100\n\n"
        f"请基于以上真实数据，分析 A 与 B 的配对情况。"
    )


async def interpret_synastry(birth1: dict, birth2: dict, house_system: str = "equal") -> dict:
    """合盘 AI 解读：在本地计算结果之上编排 LLM，生成五段结构化分析。

    Args:
        birth1/birth2: 双方出生信息
        house_system: 宫制

    Returns:
        compute_synastry 的完整结构 + `disclaimer` / `meta`。
        LLM 不可用、预算熔断、解析失败时自动回退本地模板，并在 meta 中标注。
    """
    request_id = str(uuid.uuid4())
    start = time.time() * 1000

    # compute_synastry 内部会重复调用 compute_natal，这里为 prompt 单独取一次盘
    syn = compute_synastry(birth1, birth2, house_system)
    c1 = compute_natal(birth1, house_system)
    c2 = compute_natal(birth2, house_system)
    user = _synastry_user_prompt(syn, c1, c2)

    cache_key = f"astro:synastry:{hashlib.md5(user.encode('utf-8')).hexdigest()[:12]}"
    cached = cache_get(cache_key)
    if cached:
        try:
            syn.update(json.loads(cached))
        except Exception:
            pass
        else:
            syn["disclaimer"] = DISCLAIMER
            syn["meta"] = _base_meta(request_id, "synastry", "", start,
                                     {"cacheHit": True, "degraded": False,
                                      "provider": "cache", "model": "cache", "tokens": None, "cost": 0})
            return syn

    if budget_broken():
        syn["disclaimer"] = DISCLAIMER
        syn["meta"] = _base_meta(request_id, "synastry", "", start,
                                 {"degraded": True, "degradedReason": "单日预算超限，本地规则降级",
                                  "provider": "local-rules", "model": "local-rules", "tokens": None, "cost": 0})
        return syn

    parsed, err, tokens, provider, model = await generate_structured(
        SYNASTRY_SYSTEM, user, temperature=0.75, max_tokens=1500, retries=1
    )
    if parsed is not None:
        maybe_quality_check("astro_synastry", json.dumps(parsed, ensure_ascii=False))
    if parsed is None:
        syn["disclaimer"] = DISCLAIMER
        syn["meta"] = _base_meta(request_id, "synastry", "", start,
                                 {"degraded": True, "degradedReason": "LLM 生成失败，本地规则降级",
                                  "provider": "local-rules", "model": "local-rules", "tokens": None, "cost": 0})
        return syn

    for k in ("compatibility", "love", "communication", "conflict"):
        if isinstance(parsed.get(k), str) and parsed[k].strip():
            syn[k] = parsed[k].strip()
    if isinstance(parsed.get("advice"), list) and parsed["advice"]:
        syn["advice"] = [str(x).strip() for x in parsed["advice"] if str(x).strip()][:5]

    cost = estimate_cost(tokens) if tokens else 0.0
    record_cost(cost)
    cache_set(cache_key, json.dumps(
        {k: syn[k] for k in ("compatibility", "love", "communication", "conflict", "advice") if k in syn},
        ensure_ascii=False), ttl_seconds=3600)

    syn["disclaimer"] = DISCLAIMER
    syn["meta"] = _base_meta(request_id, "synastry", "", start,
                             {"degraded": False, "degradedReason": None, "provider": provider,
                              "model": model, "tokens": tokens, "cost": cost})
    return syn


# ============================== 太阳返照 AI 解读 ==============================
async def interpret_solar_return(birth: dict, year: Optional[int] = None, house_system: str = "equal") -> dict:
    """太阳返照盘 AI 解读：在本地计算的返照盘之上编排 LLM，生成年度主题结构化分析。

    此前前端只渲染返照盘的行星落座，宫位 / 相位 / AI 解读全部缺失（第四轮 P4-7）。
    这里复用 generate_structured 的供应商故障切换与 JSON 解析，并在预算熔断 / LLM
    失败时回退本地模板（复用 _local_natal，喂的是返照盘本身）。

    Returns:
        {"data": AstroReportData, "disclaimer": str, "meta": AstroMeta}
    """
    request_id = str(uuid.uuid4())
    start = time.time() * 1000

    sr = compute_solar_return(birth, year, house_system)
    sr_year = sr.get("solarReturnYear") or (birth.get("year") if isinstance(birth.get("year"), int) else year)
    yr_txt = f"{sr_year} 年" if sr_year else "本年度"
    user = (
        f"以下是用户精确计算的{yr_txt}太阳返照盘数据，请据此解读该年度主题：\n\n{sr.get('summary', '')}"
    )

    cache_key = f"astro:sr:{hashlib.md5(user.encode('utf-8')).hexdigest()[:12]}"
    cached = cache_get(cache_key)
    if cached:
        try:
            data = json.loads(cached)
        except Exception:
            data = None
        else:
            return {
                "data": data,
                "disclaimer": DISCLAIMER,
                "meta": _base_meta(request_id, "solar_return", "", start,
                                  {"cacheHit": True, "degraded": False, "provider": "cache",
                                   "model": "cache", "tokens": None, "cost": 0}),
            }

    if budget_broken():
        data = _local_natal(sr)
        return {
            "data": data,
            "disclaimer": DISCLAIMER,
            "meta": _base_meta(request_id, "solar_return", "", start,
                              {"degraded": True, "degradedReason": "单日预算超限，本地规则降级",
                               "provider": "local-rules", "model": "local-rules", "tokens": None, "cost": 0}),
        }

    parsed, err, tokens, provider, model = await generate_structured(
        SR_SYSTEM, user, temperature=0.75, max_tokens=1500, retries=1
    )
    if parsed is None:
        data = _local_natal(sr)
        return {
            "data": data,
            "disclaimer": DISCLAIMER,
            "meta": _base_meta(request_id, "solar_return", "", start,
                              {"degraded": True, "degradedReason": "LLM 生成失败，本地规则降级",
                               "provider": "local-rules", "model": "local-rules", "tokens": None, "cost": 0}),
        }

    data = {k: parsed.get(k) for k in ("overview", "personality", "love", "career", "health", "advice")}
    if isinstance(data.get("advice"), list):
        data["advice"] = [str(x).strip() for x in data["advice"] if str(x).strip()][:5]
    else:
        data.pop("advice", None)

    cost = estimate_cost(tokens) if tokens else 0.0
    record_cost(cost)
    cache_set(cache_key, json.dumps(data, ensure_ascii=False), ttl_seconds=3600)

    return {
        "data": data,
        "disclaimer": DISCLAIMER,
        "meta": _base_meta(request_id, "solar_return", "", start,
                          {"degraded": False, "degradedReason": None, "provider": provider,
                           "model": model, "tokens": tokens, "cost": cost}),
    }


# ============================== 太阳返照（Solar Return） ==============================
def _jd_to_civil(jd: float) -> tuple[int, int, int, int, int]:
    """儒略日 → (year, month, day, hour, minute)。Fliegel–Van Flandern 算法。

    Args:
        jd: 儒略日（含小数部分表示时刻）

    Returns:
        (年, 月, 日, 时, 分)。分钟按四舍五入，遇 60 自动进位到小时。
    """
    z = math.floor(jd + 0.5)
    f = (jd + 0.5) - z
    a = z
    if z >= 2299161:
        alpha = int((z - 1867216.25) / 36524.25)
        a = z + 1 + alpha - alpha // 4
    b = a + 1524
    c = int((b - 122.1) / 365.25)
    dd = int(365.25 * c)
    e = int((b - dd) / 30.6001)
    day_f = b - dd - int(30.6001 * e) + f
    month = e - 1 if e < 14 else e - 13
    year = c - 4716 if month > 2 else c - 4715
    day_int = int(day_f)
    hh_f = (day_f - day_int) * 24.0
    hh = int(hh_f)
    mi = int(round((hh_f - hh) * 60))
    if mi >= 60:
        mi -= 60
        hh += 1
    if hh >= 24:                      # 极小概率：舍入跨日，jd 侧已保证不越界，这里只兜底
        hh -= 24
    return year, month, day_int, hh, mi


def _is_leap(y: int) -> bool:
    return y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)


def _solar_return_jd(birth: dict, sr_year: int) -> float:
    """求 sr_year 年太阳回到「出生时太阳黄经」的精确时刻（UT 儒略日）。

    用不动点迭代：太阳约 0.9856°/天，按当前误差直接补时间，4~5 次即收敛到秒级。

    Note:
        旧实现直接取「生日当天 12:00」，太阳黄经误差约 ±0.5°。
        返照盘的 ASC 对时间极敏感（4 分钟 ≈ 1°），±0.5° 的太阳误差会让整个盘的
        四轴与宫位偏移，所以这里做精确求解。

    Args:
        birth: 出生信息字典
        sr_year: 返照年份

    Returns:
        太阳精确回归时刻的 UT 儒略日
    """
    y, m, d = (int(x) for x in birth["birthDate"].split("-"))
    utc_offset = _utc_offset_of(birth)
    hh, mi = (0, 0)
    if not (birth.get("unknownTime") or not birth.get("birthTime")):
        hh, mi = (int(x) for x in birth["birthTime"].split(":"))
    natal_jd = _jd(y, m, d, hh, mi, utc_offset)
    target = _planet_lon_lat(Epoch(natal_jd), "sun")[0]

    # 2 月 29 日出生：目标年非闰年则退到 2 月 28 日
    dm = m
    dd = d
    if m == 2 and d == 29 and not _is_leap(sr_year):
        dd = 28
    jd = _jd(sr_year, dm, dd, 12, 0, utc_offset)
    for _ in range(8):
        cur = _planet_lon_lat(Epoch(jd), "sun")[0]
        diff = ((target - cur + 540) % 360) - 180      # 太阳还需前进的角度
        if abs(diff) < 1e-7:
            break
        jd += diff / 0.9856                            # 太阳平均日运动
    return jd


def compute_solar_return(birth: dict, year: Optional[int] = None,
                         house_system: str = "equal") -> dict:
    """太阳返照盘：太阳回到出生时太阳黄经的那一刻，按出生地重排的星盘。

    Args:
        birth: 出生信息字典
        year: 返照年份，缺省取当前年。**旧实现恒为「出生年 + 1」**，
              对 1990 年出生的用户意味着永远看到 1991 年的盘。
        house_system: 宫制，需与本命盘一致（旧实现未透传，恒为 equal）

    Returns:
        返照盘（结构同 compute_natal），额外含 `solarReturnYear` 与 `solarReturnTime`。
    """
    y = int(birth["birthDate"].split("-")[0])
    sr_year = int(year) if year else datetime.now().year
    if sr_year <= y:
        sr_year = y + 1                                # 出生当年没有返照
    utc_offset = _utc_offset_of(birth)

    sr_jd = _solar_return_jd(birth, sr_year)
    ly, lm, ld, lhh, lmi = _jd_to_civil(sr_jd + utc_offset / 24.0)   # UT → 本地墙上时间
    sr_birth = {
        "birthDate": f"{ly}-{lm:02d}-{ld:02d}",
        "birthTime": f"{lhh:02d}:{lmi:02d}",
        "latitude": float(birth["latitude"]),
        "longitude": float(birth["longitude"]),
        # 必须透传时区，否则返照盘的 UT 换算会退化回经度兜底
        "utcOffset": birth.get("utcOffset"),
    }
    chart = compute_natal(sr_birth, house_system)
    chart["solarReturnYear"] = sr_year
    chart["solarReturnTime"] = f"{ly}-{lm:02d}-{ld:02d} {lhh:02d}:{lmi:02d}"
    return chart
