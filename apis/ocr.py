"""阿里云 OCR(qwen-vl-plus)识别饰品图片文字。

用于「图像识别」:上传饰品截图/照片 → 识别出饰品名称(中文或英文),
再交给 SteamDT 全量数据(item_search)匹配标准名称。
"""

import os
import json
import base64
import re
from typing import List, Optional

from util.logger import logger

OCR_PROMPT = """识别这张 CS2 饰品图片中的所有饰品名称。

要求：
1. 找出图片中出现的每一个饰品名称(可能中文或英文,如 "法玛斯 | ZX81 彩色 (崭新出厂)" 或 "FAMAS | ZX Spectron (Factory New)")。
2. 一张图里可能有 1 个或多个不同饰品,请全部列出;同一个饰品只保留一个。
3. 只保留饰品名称本身,去掉价格、数量、日期、按钮文字等其他内容。
4. 如果图片里没有任何饰品名称,返回空数组 []。

输出严格 JSON：
{
  "item_names": ["饰品名称1", "饰品名称2"]
}
"""

# 允许的图片类型 -> data URI 使用的 MIME
_MIME_ALIASES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
    "image/bmp": "image/bmp",
}


def _normalize_mime(mime: str) -> str:
    """将上传文件 MIME 归一化为支持的图片 MIME;未知回退 png。"""
    return _MIME_ALIASES.get((mime or "").lower().split(";")[0].strip(), "image/png")


def _ocr_call(image_b64: str, mime: str = "image/png") -> Optional[str]:
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
            {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{image_b64}"}},
        ]
    )
    try:
        resp = llm.invoke([msg])
        return getattr(resp, "content", None) or str(resp)
    except Exception as e:
        logger.error(f"ocr: vision call failed: {e}")
        return None


def _clean_name(s: str) -> str:
    """清理单个饰品名:去引号/空白/噪声。"""
    s = re.sub(r"\s+", " ", s or "").strip().strip('"\'`').rstrip(",.，。:：")
    return s


def _parse_names(text: str) -> List[str]:
    """从模型输出中解析出所有饰品名列表。"""
    def _from_obj(obj) -> List[str]:
        if not isinstance(obj, dict):
            return []
        v = obj.get("item_names")
        if isinstance(v, list):
            return [str(x) for x in v if str(x).strip()]
        # 兼容旧的单值字段
        v2 = obj.get("item_name")
        if isinstance(v2, str) and v2.strip() and v2.strip().lower() != "null":
            return [v2]
        return []

    # 1) 优先解析代码块内的 JSON
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if m:
        try:
            names = _from_obj(json.loads(m.group(1)))
            if names:
                return names
        except json.JSONDecodeError:
            pass
    # 2) 整个文本就是 JSON
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            names = _from_obj(json.loads(stripped))
            if names:
                return names
        except json.JSONDecodeError:
            pass
    # 3) 非 JSON:按行/逗号取像名字的文本
    names = []
    for part in re.split(r"[,，\n]", text):
        line = _clean_name(part)
        if not line or len(line) <= 3 or "item_name" in line or line.startswith("{"):
            continue
        if any(kw in line.lower() for kw in ("json", "```")):
            continue
        names.append(line[:200])
    return names


def recognize_item_names(image_bytes: bytes, mime: str = "png") -> List[str]:
    """识别图片中的所有饰品名称(去重),失败返回 []。

    Args:
        image_bytes: 上传的图片原始字节(PNG/JPG/WebP 等均可)
        mime: 上传文件的 MIME,如 "image/jpeg";默认按 png 处理
    """
    b64 = base64.b64encode(image_bytes).decode("ascii")
    mime = _normalize_mime(mime)
    text = _ocr_call(b64, mime)
    if not text:
        return []

    seen, out = set(), []
    for n in _parse_names(text):
        n = _clean_name(n)
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append(n)
    return out
