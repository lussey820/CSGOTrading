"""Vision analyst agent - analyzes chart screenshots with qwen-vl-plus.

Pipeline:
    1. capture_chart_screenshots(ticker) -> {kline: PNG, line: PNG}
       Captures BOTH the candlestick (K-line) chart and the line/area chart
       by toggling the SteamDT switch button. Both views are then fed to
       the LLM so it can cross-reference candlestick patterns against the
       overall trend line.
    2. base64-encode each captured PNG
    3. call qwen-vl-plus via the DashScope OpenAI-compatible multimodal
       endpoint with one text prompt + N image_url parts (N=1 or 2)
    4. parse the LLM response into an AnalystSignal (Bullish/Bearish/Neutral)
    5. persist the signal via db.save_signal(...)

The signal flows through the existing workflow into the portfolio manager
alongside other analyst signals.
"""

import os
import re
import json
import base64
import logging
from typing import Optional, Tuple, List

from graph.constants import AgentKey, Signal
from graph.schema import FundState, AnalystSignal
from llm.prompt import VISION_PROMPT
from apis.cs2market.chart_screenshot import capture_chart_screenshots
from util.cs2_db_helper import get_cs2_db
from util.logger import logger
from util import screenshot_cache as sc

logger_v = logging.getLogger("cs2.vision")


def _encode_image_b64(image_path: str) -> str:
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("ascii")


def _vision_call_multi(
    image_b64_list: List[str], prompt: str
) -> Tuple[Optional[str], Optional[Exception]]:
    """Call qwen-vl-plus with one prompt and one or more images.

    Uses langchain_openai.ChatOpenAI in OpenAI-compatible multimodal mode
    against the DashScope endpoint (DASHSCOPE_API_KEY / DASHSCOPE_BASE_URL /
    VISION_MODEL from .env).

    Returns (text_response, error).
    """
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = os.getenv("VISION_MODEL", "qwen-vl-plus")
    if not api_key:
        return None, RuntimeError("DASHSCOPE_API_KEY not set in .env")

    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except ImportError as e:
        return None, RuntimeError(f"langchain_openai not available: {e}")

    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.3,
        max_retries=2,
    )
    content: List[dict] = [{"type": "text", "text": prompt}]
    for b64 in image_b64_list:
        content.append(
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{b64}"},
            }
        )
    msg = HumanMessage(content=content)
    try:
        resp = llm.invoke([msg])
        text = getattr(resp, "content", None) or str(resp)
        return text, None
    except Exception as e:
        return None, e


# Backward-compat alias used by older callers / tests
_vision_call = _vision_call_multi


# Patterns for tolerant parsing of LLM responses
_SIGNAL_PATTERN = re.compile(
    r'"?signal"?\s*[:=]\s*"?(bullish|bearish|neutral)"?',
    re.IGNORECASE,
)
_JUST_PATTERN = re.compile(
    r'"?justification"?\s*[:=]\s*"(.+?)"(?:\s*[,}])',
    re.IGNORECASE | re.DOTALL,
)
_SUPPORT_PATTERN = re.compile(r'"?support"?\s*[:=]\s*"?([\d.]+)"?', re.IGNORECASE)
_RESISTANCE_PATTERN = re.compile(r'"?resistance"?\s*[:=]\s*"?([\d.]+)"?', re.IGNORECASE)


def _parse_vision_response(text: str) -> AnalystSignal:
    """Parse the LLM response into an AnalystSignal.

    Tries JSON first, then regex extraction, then a keyword fallback.
    Also extracts optional support / resistance price levels.
    """
    if not text:
        return AnalystSignal(
            signal=Signal.NEUTRAL, justification="Empty vision LLM response"
        )

    def _fill_levels(signal: AnalystSignal, obj_or_text: dict) -> AnalystSignal:
        for key, attr in (("support", "support"), ("resistance", "resistance")):
            if isinstance(obj_or_text, dict):
                val = obj_or_text.get(key)
            else:
                m = _SUPPORT_PATTERN.search(obj_or_text) if key == "support" else _RESISTANCE_PATTERN.search(obj_or_text)
                val = m.group(1) if m else None
            if val is None:
                continue
            try:
                setattr(signal, attr, float(val))
            except (TypeError, ValueError):
                continue
        return signal

    candidates: List[str] = []

    # Strip code fences ```json ... ``` or ``` ... ```
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        candidates.append(m.group(1))

    # First {...} block
    m = re.search(r"\{[^{}]*\}", text, re.DOTALL)
    if m:
        candidates.append(m.group(0))

    # Whole text trimmed
    candidates.append(text.strip())

    for c in candidates:
        try:
            obj = json.loads(c)
        except json.JSONDecodeError:
            continue
        sig = str(obj.get("signal", "")).strip().capitalize()
        just = str(obj.get("justification", "")).strip()
        if sig in ("Bullish", "Bearish", "Neutral") and just:
            return _fill_levels(AnalystSignal(signal=Signal(sig), justification=just[:1000]), obj)

    # Regex extraction
    m = _SIGNAL_PATTERN.search(text)
    if m:
        sig = m.group(1).strip().capitalize()
        jm = _JUST_PATTERN.search(text)
        just = (jm.group(1).strip() if jm else text[:300])[:1000]
        if sig in ("Bullish", "Bearish", "Neutral"):
            return _fill_levels(AnalystSignal(signal=Signal(sig), justification=just), text)

    # Keyword fallback
    lower = text.lower()
    if "bullish" in lower and "bearish" not in lower:
        sig = Signal.BULLISH
    elif "bearish" in lower and "bullish" not in lower:
        sig = Signal.BEARISH
    else:
        sig = Signal.NEUTRAL
    return _fill_levels(AnalystSignal(signal=sig, justification=text[:1000]), text)


def vision_agent(state: FundState):
    """Vision analyst agent: capture K-line + Line screenshots and analyze with qwen-vl-plus."""
    agent_name = AgentKey.VISION
    ticker = state["ticker"]
    portfolio_id = state["portfolio"].id

    db = get_cs2_db()

    logger.log_agent_status(agent_name, ticker, "Capturing chart screenshots (kline + line)")

    # Step 1: use cached screenshots if fresh (populated by startup warmup);
    # otherwise capture on-demand and write back to cache so later runs can reuse.
    shots = {"kline": None, "line": None}
    cached = sc.get_cached_paths(ticker)
    if cached:
        logger.log_agent_status(agent_name, ticker, "Using cached chart screenshots")
        shots = cached
    else:
        logger.log_agent_status(agent_name, ticker, "No fresh cache, capturing now")
        try:
            shots = capture_chart_screenshots(ticker)
            if shots.get("kline") or shots.get("line"):
                sc.upsert_cache_row(
                    ticker,
                    status="done",
                    kline_path=shots.get("kline"),
                    line_path=shots.get("line"),
                )
        except Exception as e:
            logger.error(f"Screenshot exception for {ticker}: {e}")
            sc.upsert_cache_row(
                ticker, status="error", error_msg=str(e)[:500]
            )
            shots = {"kline": None, "line": None}

    kline_path = shots.get("kline")
    line_path = shots.get("line")

    if not kline_path and not line_path:
        prompt = VISION_PROMPT.format(ticker=ticker, error="screenshot capture failed for both chart types")
        signal = AnalystSignal(
            signal=Signal.NEUTRAL,
            justification=f"Chart screenshots unavailable for {ticker}; vision analysis skipped.",
        )
        logger.log_signal(agent_name, ticker, signal)
        db.save_signal(portfolio_id, agent_name, ticker, prompt, signal)
        return {"analyst_signals": [signal]}

    # Step 2: encode each available image
    image_b64_list: List[str] = []
    captured_summary: List[str] = []
    try:
        if kline_path:
            image_b64_list.append(_encode_image_b64(kline_path))
            captured_summary.append(f"kline={kline_path}")
        if line_path:
            image_b64_list.append(_encode_image_b64(line_path))
            captured_summary.append(f"line={line_path}")
    except Exception as e:
        logger.error(f"Failed to encode screenshots for {ticker}: {e}")
        prompt = VISION_PROMPT.format(ticker=ticker, error="image encoding failed")
        signal = AnalystSignal(
            signal=Signal.NEUTRAL, justification=f"Image encoding error: {e}"
        )
        logger.log_signal(agent_name, ticker, signal)
        db.save_signal(portfolio_id, agent_name, ticker, prompt, signal)
        return {"analyst_signals": [signal]}

    # Adapt the prompt note to how many images we actually captured
    if len(image_b64_list) == 1:
        error_note = "Note: only one chart view was captured (the other failed); analyze based on the available image."
    else:
        error_note = ""

    prompt = VISION_PROMPT.format(ticker=ticker, error=error_note)
    logger.log_agent_status(
        agent_name, ticker,
        f"Calling vision LLM (qwen-vl-plus) with {len(image_b64_list)} image(s)",
    )

    # Step 3: call vision LLM
    text, err = _vision_call_multi(image_b64_list, prompt)
    if err:
        logger.error(f"Vision LLM call failed for {ticker}: {err}")
        signal = AnalystSignal(
            signal=Signal.NEUTRAL, justification=f"Vision LLM error: {err}"
        )
        full_prompt = f"{prompt}\n\n[screenshots: {', '.join(captured_summary)}]"
        logger.log_signal(agent_name, ticker, signal)
        db.save_signal(portfolio_id, agent_name, ticker, full_prompt, signal)
        return {"analyst_signals": [signal]}

    # Step 4: parse response
    signal = _parse_vision_response(text)
    full_prompt = (
        f"{prompt}\n\n[screenshots: {', '.join(captured_summary)}]\n"
        f"[LLM response: {text}]"
    )
    logger.log_signal(agent_name, ticker, signal)
    db.save_signal(portfolio_id, agent_name, ticker, full_prompt, signal)
    return {"analyst_signals": [signal]}
