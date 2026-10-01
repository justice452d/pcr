from __future__ import annotations

from .common import calculate_percentile_rank, clamp, window_start


DEFAULT_WEIGHTS = {"dividend": 45.0, "spread": 35.0, "pb": 20.0}
WINDOWS = {"5": 5, "10": 10, "all": None}
MIN_SPREAD_PERCENTILE_SAMPLES = 252


def temperature_state(value: float | None) -> str:
    if value is None:
        return "数据不完整"
    value = clamp(float(value))
    if value < 20:
        return "极度便宜"
    if value < 40:
        return "便宜"
    if value < 60:
        return "正常"
    if value < 75:
        return "偏贵"
    if value < 90:
        return "贵"
    return "极贵"


def pension_reference(value: float | None) -> str:
    state = temperature_state(value)
    return {
        "极度便宜": "当年剩余计划资金可以明显加速投入",
        "便宜": "提高投入速度",
        "正常": "正常定投",
        "偏贵": "正常小额定投，保留部分资金",
        "贵": "降低新增投入速度",
        "极贵": "暂停额外加仓，以正常基础定投为主",
    }.get(state, "数据完整后再生成机械参考")


def fixed_spread_temperature(spread: float | None) -> float | None:
    if spread is None:
        return None
    spread = float(spread)
    if spread > 4.0:
        return 10.0
    if spread >= 3.5:
        return 20.0
    if spread >= 3.0:
        return 30.0
    if spread >= 2.5:
        return 40.0
    if spread >= 2.0:
        return 55.0
    if spread >= 1.5:
        return 70.0
    if spread >= 1.0:
        return 85.0
    return 100.0


def validate_weights(weights=None) -> dict[str, float]:
    result = dict(DEFAULT_WEIGHTS if weights is None else weights)
    if set(result) != set(DEFAULT_WEIGHTS):
        raise ValueError("权重必须包含 dividend、spread、pb")
    result = {key: float(value) for key, value in result.items()}
    if any(value < 0 or value > 100 for value in result.values()):
        raise ValueError("单项权重必须在 0–100 之间")
    if abs(sum(result.values()) - 100.0) > 1e-8:
        raise ValueError("三项权重总和必须等于 100%")
    return result


def explain(row: dict) -> list[str]:
    messages = []
    dividend = row.get("dividend_temperature")
    spread = row.get("spread_temperature")
    pb = row.get("pb_temperature")
    if dividend is not None:
        messages.append("当前股息率处于历史较低位置。" if dividend >= 60 else
                        "当前股息率处于历史较高位置，长期估值吸引力较强。" if dividend <= 40 else
                        "当前股息率接近自身历史中枢。")
    if spread is not None:
        messages.append("相对10年国债，红利资产仍具有较高收益优势。" if spread <= 40 else
                        "相对10年国债，当前股息收益优势偏弱。" if spread >= 70 else
                        "当前股债利差处于中性区域。")
    if row.get("pb_sample_count", 0) < 12:
        messages.append("PB历史快照不足，当前分位代表性有限。")
    elif pb is not None:
        messages.append("当前PB已处于自身历史较高位置。" if pb >= 60 else
                        "当前PB处于自身历史较低位置。" if pb <= 40 else
                        "当前PB接近自身历史中枢。")
    return messages[:3]


def calculate_series(raw_rows: list[dict], window="10", weights=None) -> list[dict]:
    if str(window).lower() not in WINDOWS:
        raise ValueError("统计窗口必须为 5、10 或 all")
    weights = validate_weights(weights)
    years = WINDOWS[str(window).lower()]
    rows = sorted((dict(row) for row in raw_rows), key=lambda row: row["date"])
    output = []
    for index, row in enumerate(rows):
        cutoff = window_start(row["date"], years)
        history = [item for item in rows[:index + 1]
                   if cutoff is None or item["date"] >= cutoff.isoformat()]
        dividend = row.get("dividend_yield")
        pb = row.get("pb")
        cn10y = row.get("cn10y")
        spread = (float(dividend) - float(cn10y)
                  if dividend is not None and cn10y is not None else None)
        dividend_pct = calculate_percentile_rank(
            [item.get("dividend_yield") for item in history], dividend)
        pb_samples = {}
        for item in history:
            if item.get("pb") is not None:
                pb_samples[item.get("pb_asof") or item["date"]] = item.get("pb")
        pb_pct = calculate_percentile_rank(pb_samples.values(), pb)
        spread_history = [float(item["dividend_yield"]) - float(item["cn10y"])
                          for item in history
                          if item.get("dividend_yield") is not None and item.get("cn10y") is not None]
        spread_pct = calculate_percentile_rank(spread_history, spread)
        dividend_temperature = 100.0 - dividend_pct if dividend_pct is not None else None
        pb_temperature = pb_pct
        use_percentile = len(spread_history) >= MIN_SPREAD_PERCENTILE_SAMPLES
        spread_temperature = (100.0 - spread_pct if use_percentile and spread_pct is not None
                              else fixed_spread_temperature(spread))
        missing = [name for name, value in (("股息率", dividend), ("PB", pb),
                                             ("中国10年国债收益率", cn10y),
                                             ("指数价格", row.get("index_price"))) if value is None]
        complete = not missing and None not in (dividend_temperature, spread_temperature, pb_temperature)
        valuation = None
        if complete:
            valuation = clamp((dividend_temperature * weights["dividend"] +
                               spread_temperature * weights["spread"] +
                               pb_temperature * weights["pb"]) / 100.0)
            valuation = round(valuation, 1)
        enriched = dict(row)
        enriched.update({
            "spread": round(spread, 6) if spread is not None else None,
            "dividend_yield_percentile": round(dividend_pct, 4) if dividend_pct is not None else None,
            "dividend_temperature": round(dividend_temperature, 4) if dividend_temperature is not None else None,
            "pb_percentile": round(pb_pct, 4) if pb_pct is not None else None,
            "pb_temperature": round(pb_temperature, 4) if pb_temperature is not None else None,
            "pb_sample_count": len(pb_samples),
            "spread_percentile": round(spread_pct, 4) if spread_pct is not None else None,
            "spread_temperature": round(spread_temperature, 4) if spread_temperature is not None else None,
            "spread_model": "historical_percentile" if use_percentile else "fixed_bands",
            "valuation_temperature": valuation,
            "state": temperature_state(valuation),
            "missing": missing,
            "sample_count": len(history),
            "window_start": history[0]["date"] if history else None,
            "window_end": history[-1]["date"] if history else None,
            "weights": weights,
        })
        enriched["explanation"] = explain(enriched)
        enriched["pension_reference"] = pension_reference(valuation)
        output.append(enriched)
    return output

