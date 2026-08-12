"""阿里云 OCR(qwen-vl-plus)识别饰品图片文字。

用于「扫码识别」:上传饰品截图/照片 → 识别出饰品名称(中文或英文),
再交给 SteamDT 全量数据(item_search)匹配标准名称。
"""

import os
import json
import base64
import re
from typing import Optional

from util.logger import logger

OCR_PROMPT = """识别这张 CS2 饰品图片中的饰品名称。

要求：
1. 从图片中找出饰品名称（可能是中文名或英文名，如 "法玛斯 | ZX81 彩色 (崭新出厂)" 或 "FAMAS | ZX Spectron (Factory New)"）。
2. 只保留饰品名称本身，去掉价格、数量、日期、按钮文字等其他内容。
3. 如果图片里没有明确的饰品名，返回 null。

输出严格 JSON：
{
  "item_name": "饰品名称或 null"
}
"""


def _ocr_call(image_b64: str) -> Optional[str]:
    """调用 qwen-vl-plus 识别图片文字,返回模型输出文本。"""
    api_key = os.getenv("DASHSCOPE_API_KEY")
    base_url = os.getenv(
        "DASHSCOPE_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"
    )
    model = os.getenv("VISION_MODEL", "qwen-vl-plus")
    if not api_key:
        return None
    try:
        from langchain_openai import ChatOpenAI
        from langchain_core.messages import HumanMessage
    except ImportError as e:
        logger.error(f"ocr: langchain_openai not available: {e}")
        return None
    llm = ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0.1,
        max_retries=2,
    )
    msg = HumanMessage(
        content=[
            {"type": "text", "text": OCR_PROMPT},
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
        ]
    )
    try:
        resp = llm.invoke([msg])
        return getattr(resp, "content", None) or str(resp)
    except Exception as e:
        logger.error(f"ocr: vision call failed: {e}")
        return None


def recognize_item_name(image_bytes: bytes) -> Optional[str]:
    """识别图片中的饰品名称,失败返回 None。

    Args:
        image_bytes: 上传的图片原始字节(PNG/JPG 均可)
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    text = _ocr_call(b64)
    if not text:
        return None

    def _parse_json(s: str) -> Optional[str]:
        """解析 JSON 中的 item_name;明确为 null 时返回 ''(区分未识别),解析失败返回 None。"""
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            return None
        v = obj.get("item_name")
        if v is None:
            return ""  # 模型明确表示无饰品名
        if isinstance(v, str) and v.strip() and v.strip().lower() != "null":
            return v.strip()
        return ""

    # 优先解析代码块内的 JSON
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        r = _parse_json(m.group(1))
        if r is not None:
            return r or None
    # 再尝试整个文本就是 JSON
    if text.strip().startswith("{"):
        r = _parse_json(text.strip())
        if r is not None:
            return r or None

    # 非 JSON 文本:取第一个像名字的行(去除噪声)
    for line in text.splitlines():
        line = line.strip().strip('"\'`').rstrip(",.，。")
        if not line or len(line) <= 3 or "item_name" in line or line.startswith("{"):
            continue
        if any(kw in line.lower() for kw in ("json", "```")):
            continue
        return line[:200]
    return None
