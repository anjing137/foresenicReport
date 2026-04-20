"""
OCR 识别工具 - Windows 兼容版
使用标准 OpenAI API 调用硅基流动 PaddleOCR-VL，无需 paddleocr 包

仅适用于 Windows 环境！
Mac/Linux 请使用 app/utils/ocr.py（使用 PaddleOCRVL 客户端）

调用方式：
    from app.utils.ocr2 import run_ocr_windows

    result = run_ocr_windows("path/to/image.png")
    # result 包含 markdown_texts（识别结果列表）和 full_response（原始响应）
"""
import os
import sys
import logging
import base64
from pathlib import Path
from typing import Dict, Any, List, Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# Windows 平台检测
IS_WINDOWS = sys.platform == "win32"


def _get_mime_type(image_path: str) -> str:
    """根据文件扩展名确定 MIME 类型"""
    ext = Path(image_path).suffix.lower()
    mime_types = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
    }
    return mime_types.get(ext, "image/jpeg")


def _image_to_base64(image_path: str) -> str:
    """将图片文件转为 base64 编码"""
    with open(image_path, "rb") as f:
        image_data = f.read()
    return base64.b64encode(image_data).decode("utf-8")


def _call_paddleocr_vl_api(image_path: str, timeout: float = 120.0) -> Dict[str, Any]:
    """
    通过标准 OpenAI API 调用硅基流动 PaddleOCR-VL-1.5

    Args:
        image_path: 图片文件路径
        timeout: 请求超时时间（秒）

    Returns:
        API 原始响应（包含 choices[0].message.content）
    """
    api_key = settings.SILICONFLOW_API_KEY
    if not api_key:
        raise ValueError(
            "未配置 SILICONFLOW_API_KEY！\n"
            "请在 backend/.env 文件中添加：\n"
            "SILICONFLOW_API_KEY=你的API密钥"
        )

    base_url = settings.SILICONFLOW_BASE_URL.rstrip("/")
    model = settings.OCR_MODEL

    # 读取图片
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"图片文件不存在: {image_path}")

    mime_type = _get_mime_type(image_path)
    base64_image = _image_to_base64(image_path)

    # 构建请求体（参考硅基流动官方文档）
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{base64_image}"
                        }
                    },
                    {
                        "type": "text",
                        "text": "<image>\n<|grounding|>Convert the document to markdown."
                    }
                ]
            }
        ],
        "extra_headers": {
            # 硅基流动可能需要此 header
        }
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    logger.info(f"调用 PaddleOCR-VL API: {base_url}/chat/completions")
    logger.info(f"图片: {image_path}, MIME: {mime_type}, 大小: {os.path.getsize(image_path) / 1024:.1f} KB")

    with httpx.Client(timeout=timeout) as client:
        response = client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload
        )

    if response.status_code != 200:
        logger.error(f"API 返回错误: {response.status_code}")
        logger.error(f"响应内容: {response.text}")
        raise Exception(f"PaddleOCR-VL API 调用失败: {response.status_code} - {response.text}")

    return response.json()


def _parse_ocr_response(response: Dict[str, Any]) -> Dict[str, Any]:
    """
    解析 PaddleOCR-VL API 响应

    Args:
        response: API 原始响应

    Returns:
        解析后的结果字典
    """
    try:
        content = response["choices"][0]["message"]["content"]

        # 尝试解析为 JSON（如果返回的是结构化结果）
        # PaddleOCR-VL 可能返回 markdown 格式的结果
        markdown_text = content.strip()

        # 检查是否是 JSON 格式
        if markdown_text.startswith("```json"):
            markdown_text = markdown_text[7:]
        if markdown_text.startswith("```"):
            # 去掉 markdown 代码块标记
            lines = markdown_text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            markdown_text = "\n".join(lines)

        return {
            "success": True,
            "markdown_texts": [markdown_text],  # 返回列表，兼容原有接口
            "full_text": markdown_text,
            "raw_content": content,
        }

    except (KeyError, IndexError) as e:
        logger.error(f"解析响应失败: {e}")
        logger.error(f"原始响应: {response}")
        raise Exception(f"解析 PaddleOCR-VL 响应失败: {e}")


def run_ocr_windows(image_path: str) -> Dict[str, Any]:
    """
    Windows 环境 OCR 识别入口函数

    Args:
        image_path: 图片文件路径

    Returns:
        {
            "success": bool,
            "markdown_texts": List[str],  # 识别出的文字列表
            "full_text": str,              # 所有文字合并后的文本
            "raw_content": str,             # API 原始返回内容
        }

    Example:
        result = run_ocr_windows("C:/Users/xxx/Documents/test.png")
        if result["success"]:
            for text in result["markdown_texts"]:
                print(text)
    """
    if not IS_WINDOWS:
        logger.warning("ocr2.py 是 Windows 专用模块，请在 Windows 上使用！")

    logger.info(f"开始 OCR 识别: {image_path}")

    try:
        # 1. 调用 API
        response = _call_paddleocr_vl_api(image_path)

        # 2. 解析结果
        result = _parse_ocr_response(response)

        # 3. 记录日志
        text_len = len(result.get("full_text", ""))
        logger.info(f"OCR 识别成功，文字长度: {text_len} 字符")

        return result

    except Exception as e:
        logger.error(f"OCR 识别失败: {e}")
        return {
            "success": False,
            "markdown_texts": [],
            "full_text": "",
            "error": str(e),
        }


def batch_ocr_windows(image_paths: List[str], delay: float = 1.0) -> List[Dict[str, Any]]:
    """
    批量 OCR 识别（Windows）

    Args:
        image_paths: 图片路径列表
        delay: 每次请求之间的延迟（秒），避免 API 限流

    Returns:
        结果列表
    """
    results = []

    for i, image_path in enumerate(image_paths):
        logger.info(f"处理进度: {i + 1}/{len(image_paths)}")
        result = run_ocr_windows(image_path)
        results.append({
            "path": image_path,
            "result": result
        })

        if i < len(image_paths) - 1 and delay > 0:
            import time
            time.sleep(delay)

    return results


# ============ 便捷函数 ============

def ocr_image(image_path: str) -> str:
    """
    便捷函数：直接返回识别文字

    Args:
        image_path: 图片路径

    Returns:
        识别出的文字内容
    """
    result = run_ocr_windows(image_path)
    if result["success"]:
        return result["full_text"]
    else:
        raise Exception(result.get("error", "OCR 识别失败"))


def ocr_base64(image_base64: str, mime_type: str = "image/jpeg") -> str:
    """
    通过 base64 数据进行 OCR 识别（不依赖文件）

    Args:
        image_base64: 图片的 base64 编码（不含 data URI 前缀）
        mime_type: MIME 类型

    Returns:
        识别出的文字内容
    """
    api_key = settings.SILICONFLOW_API_KEY
    if not api_key:
        raise ValueError("未配置 SILICONFLOW_API_KEY")

    base_url = settings.SILICONFLOW_BASE_URL.rstrip("/")
    model = settings.OCR_MODEL

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{mime_type};base64,{image_base64}"
                        }
                    },
                    {
                        "type": "text",
                        "text": "<image>\n<|grounding|>Convert the document to markdown."
                    }
                ]
            }
        ]
    }

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    with httpx.Client(timeout=120.0) as client:
        response = client.post(
            f"{base_url}/chat/completions",
            headers=headers,
            json=payload
        )

    if response.status_code != 200:
        raise Exception(f"API 返回错误: {response.status_code}")

    result = response.json()
    return result["choices"][0]["message"]["content"]


# ============ 测试代码 ============
if __name__ == "__main__":
    # 简单测试
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

    print("=" * 50)
    print("PaddleOCR-VL Windows 兼容版")
    print("=" * 50)

    # 检查配置
    if not settings.SILICONFLOW_API_KEY:
        print("\n⚠️  警告：未配置 SILICONFLOW_API_KEY")
        print("请在 backend/.env 文件中添加：")
        print("SILICONFLOW_API_KEY=你的API密钥\n")

    # 如果有命令行参数，当作图片路径处理
    if len(sys.argv) > 1:
        image_path = sys.argv[1]
        print(f"\n识别图片: {image_path}\n")

        result = run_ocr_windows(image_path)

        if result["success"]:
            print("=" * 50)
            print("识别结果：")
            print("=" * 50)
            print(result["full_text"])
        else:
            print(f"\n❌ 识别失败: {result.get('error')}")
    else:
        print("\n用法：python ocr2.py <图片路径>")
        print("示例：python ocr2.py C:/Users/xxx/test.png")
