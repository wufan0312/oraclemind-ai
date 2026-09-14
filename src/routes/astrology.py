"""占星路由"""

from __future__ import annotations

import logging
import re

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.services.astrology import (
    compute_natal, interpret_astrology, compute_synastry, interpret_synastry,
    compute_transit_aspects, compute_solar_return, interpret_solar_return,
    _ref_date, _utc_offset_of,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/v1/astrology", tags=["astrology"])

# pymeeus 的 Epoch 仅支持 1885-2099 年（实测 1885/2098 可用，1884/2099 抛 ValueError），
# 超出范围必须在入口拦住，否则用户只会看到"计算失败"却不知原因
EPOCH_MIN_YEAR = 1885
EPOCH_MAX_YEAR = 2098
_DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class AstroBirth(BaseModel):
    birthDate: str  # YYYY-MM-DD
    birthTime: str  # HH:mm（本地墙上时间，时间未知时传空串）
    latitude: float
    longitude: float
    houseSystem: str | None = None
    unknownTime: bool | None = None  # 出生时间未知 → 日间盘降级
    # 出生地时区相对 UT 的小时偏移（东八区=8.0）。缺省时后端按经度兜底推算（精度不足）
    utcOffset: float | None = None
    # 太阳返照年份（仅 /solar-return 使用），缺省取当前年
    year: int | None = None
    # 解读盐值：客户端「换一版」时用随机串绕开同 prompt 缓存，触发重新生成
    salt: str | None = None


class AstroForecastRequest(AstroBirth):
    period: str = "daily"  # daily / weekly / monthly / yearly


class SynastryRequest(BaseModel):
    birth1: dict
    birth2: dict
    houseSystem: str | None = None


def _err(code: str, message: str) -> dict:
    return {"error": code, "message": message}


def _parse_birth(body: dict) -> tuple[dict | None, str | None]:
    """校验并规范化出生信息。

    Returns:
        (birth_dict, None) 校验通过；(None, 错误消息) 校验失败。
    """
    if not body:
        return None, "缺少出生信息"
    bd = body.get("birthDate")
    bt = body.get("birthTime") or ""
    unknown = bool(body.get("unknownTime"))
    lat = body.get("latitude")
    lng = body.get("longitude")

    if not isinstance(bd, str) or not _DATE_RE.match(bd):
        return None, "birthDate 格式须为 YYYY-MM-DD"
    y, m, d = (int(x) for x in bd.split("-"))
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None, "birthDate 月/日超出合法范围"
    if not (EPOCH_MIN_YEAR <= y <= EPOCH_MAX_YEAR):
        return None, f"本引擎支持 {EPOCH_MIN_YEAR}-{EPOCH_MAX_YEAR} 年出生的星盘计算"

    if not unknown:
        if not isinstance(bt, str) or not _TIME_RE.match(bt):
            return None, "birthTime 格式须为 HH:mm（00:00-23:59），时间未知请传 unknownTime=true"

    try:
        lat = float(lat)
        lng = float(lng)
    except (TypeError, ValueError):
        return None, "latitude / longitude 须为数字"
    if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
        return None, "经度须在 ±180、纬度须在 ±90 之间"

    return {
        "birthDate": bd,
        "birthTime": bt,
        "latitude": lat,
        "longitude": lng,
        "houseSystem": body.get("houseSystem"),
        "unknownTime": unknown,
        "utcOffset": body.get("utcOffset"),
    }, None


def _house_system(req) -> str:
    return (getattr(req, "houseSystem", None) or "equal")


@router.post("/natal")
async def post_natal(req: AstroBirth):
    """本命盘计算（用于前端星盘渲染，不调用 LLM）"""
    birth, err = _parse_birth(req.model_dump())
    if err:
        return _err("INVALID_BIRTH", err)
    try:
        return compute_natal(birth, _house_system(req))
    except Exception as e:
        logger.error(f"natal 计算失败: {e}")
        return _err("NATAL_FAIL", "占星服务处理失败，请稍后重试")


@router.post("/report")
async def post_report(req: AstroBirth):
    """本命盘 AI 解读（结构化 JSON）"""
    birth, err = _parse_birth(req.model_dump())
    if err:
        return _err("INVALID_BIRTH", err)
    try:
        return await interpret_astrology("natal", birth, house_system=_house_system(req), salt=req.salt or "")
    except Exception as e:
        logger.error(f"report 解读失败: {e}")
        return _err("REPORT_FAIL", "占星服务处理失败，请稍后重试")


@router.post("/forecast")
async def post_forecast(req: AstroForecastRequest):
    """每日/每周/每月/年度运势预测（结构化 JSON）"""
    birth, err = _parse_birth(req.model_dump())
    if err:
        return _err("INVALID_BIRTH", err)
    period = req.period if req.period in ("daily", "weekly", "monthly", "yearly") else "daily"
    try:
        return await interpret_astrology(
            "forecast", birth, period, house_system=_house_system(req)
        )
    except Exception as e:
        logger.error(f"forecast 预测失败: {e}")
        return _err("FORECAST_FAIL", "占星服务处理失败，请稍后重试")


@router.post("/synastry")
async def post_synastry(req: SynastryRequest):
    """星座配对 / 合盘分析（比较盘 + 组合盘 + AI 解读）"""
    b1, e1 = _parse_birth(req.birth1)
    b2, e2 = _parse_birth(req.birth2)
    if e1 or e2:
        return _err("INVALID_BIRTH", f"birth1: {e1 or 'OK'}；birth2: {e2 or 'OK'}")
    try:
        return await interpret_synastry(b1, b2, req.houseSystem or "equal")
    except Exception as e:
        logger.error(f"synastry 计算失败: {e}")
        return _err("SYNASTRY_FAIL", "占星服务处理失败，请稍后重试")


@router.post("/solar-return")
async def post_solar_return(req: AstroBirth):
    """太阳返照盘：太阳回到出生时太阳黄经的那一刻，按出生地重排的星盘"""
    birth, err = _parse_birth(req.model_dump())
    if err:
        return _err("INVALID_BIRTH", err)
    year = req.year
    if year is not None and not (EPOCH_MIN_YEAR <= year <= EPOCH_MAX_YEAR):
        return _err("INVALID_YEAR",
                    f"返照年份须在 {EPOCH_MIN_YEAR}-{EPOCH_MAX_YEAR} 之间")
    try:
        return compute_solar_return(birth, year, _house_system(req))
    except Exception as e:
        logger.error(f"solar-return 计算失败: {e}")
        return _err("SR_FAIL", "占星服务处理失败，请稍后重试")


@router.post("/solar-return/report")
async def post_solar_return_report(req: AstroBirth):
    """太阳返照盘 AI 解读（结构化 JSON，年度主题）"""
    birth, err = _parse_birth(req.model_dump())
    if err:
        return _err("INVALID_BIRTH", err)
    year = req.year
    if year is not None and not (EPOCH_MIN_YEAR <= year <= EPOCH_MAX_YEAR):
        return _err("INVALID_YEAR",
                    f"返照年份须在 {EPOCH_MIN_YEAR}-{EPOCH_MAX_YEAR} 之间")
    try:
        return await interpret_solar_return(birth, year, _house_system(req))
    except Exception as e:
        logger.error(f"solar-return report 解读失败: {e}")
        return _err("SR_REPORT_FAIL", "占星服务处理失败，请稍后重试")


@router.post("/transits")
async def post_transits(req: AstroForecastRequest):
    """当前真实天象：流年行星对本命盘的交相相位（用于运势关键天象展示）"""
    birth, err = _parse_birth(req.model_dump())
    if err:
        return _err("INVALID_BIRTH", err)
    try:
        chart = compute_natal(birth, _house_system(req))
        # 复用 service 层的 _ref_date，避免逻辑两份（旧版在此复制了一份，易漏改）
        rd = _ref_date(req.period)
        transits = compute_transit_aspects(
            chart, rd, float(birth["latitude"]), float(birth["longitude"]),
            _utc_offset_of(birth),   # 与本命盘同一套时区口径
        )
        return {"period": req.period, "refDate": rd.strftime("%Y-%m-%d"), "transits": transits}
    except Exception as e:
        logger.error(f"transits 计算失败: {e}")
        return _err("TRANSIT_FAIL", "占星服务处理失败，请稍后重试")
