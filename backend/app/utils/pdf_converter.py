"""
PDF 转换工具 - 使用 pdftoppm 将 PDF 分割为 PNG 图片
"""
import os
import shutil
import subprocess
import uuid
import json
import re
from pathlib import Path
from typing import List, Tuple, Optional

from app.config import settings


class PdfConverter:
    """PDF 转 PNG 转换器，使用系统 pdftoppm 命令"""

    def __init__(self, case_id: int):
        self.case_id = case_id
        # 转换结果存放目录
        self.output_dir = os.path.join(settings.UPLOAD_DIR, str(case_id), "pdf_pages")
        os.makedirs(self.output_dir, exist_ok=True)

    def convert(self, pdf_path: str, original_filename: str = None) -> Tuple[bool, List[dict], str]:
        """
        将 PDF 转换为 PNG 图片

        Args:
            pdf_path: PDF 文件的绝对路径
            original_filename: 原始 PDF 文件名（用于返回给前端展示）

        Returns:
            (success, pages, error_message)
            pages: [{page_number: 1, filename: "page-01.png", original_filename: "原始文件名"}, ...]
        """
        if not os.path.exists(pdf_path):
            return False, [], f"PDF 文件不存在: {pdf_path}"

        # 检查 pdftoppm 是否可用
        pdftoppm = self._find_pdftoppm()
        if not pdftoppm:
            return False, [], "未找到 pdftoppm。macOS 请安装 poppler：brew install poppler；如果已安装，请确认 /opt/homebrew/bin/pdftoppm 或 /usr/local/bin/pdftoppm 存在"

        # 生成输出文件名前缀
        prefix = f"case{self.case_id}_{uuid.uuid4().hex[:8]}"
        # 保存原始 PDF 文件名用于返回
        self._original_pdf_filename = original_filename or os.path.basename(pdf_path)
        # 保存元数据（原始 PDF 文件名）到同名 .json 文件
        meta_path = os.path.join(self.output_dir, prefix + "_meta.json")
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump({
                "original_pdf_filename": self._original_pdf_filename,
                "source_pdf_path": pdf_path,
            }, f)

        try:
            # 执行 pdftoppm 转换
            # pdftoppm -png -r 150 input.pdf output_prefix
            # 注意：不使用 -singlefile，这样多页 PDF 会生成 prefix-1.png, prefix-2.png 等文件
            cmd = [
                pdftoppm,
                "-png",           # 输出 PNG 格式
                "-r", "150",      # 分辨率 150 DPI
                pdf_path,
                os.path.join(self.output_dir, prefix)
            ]

            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=120  # 120秒超时
            )

            if result.returncode != 0:
                return False, [], f"pdftoppm 执行失败: {result.stderr}"

            # 查找生成的文件
            generated_files = []
            for f in os.listdir(self.output_dir):
                if f.startswith(prefix) and (f.endswith(".png") or f.endswith(".jpg")):
                    filepath = os.path.join(self.output_dir, f)
                    # 提取页码（从文件名中提取）
                    page_number = self._extract_page_number(f, prefix)
                    generated_files.append({
                        "page_number": page_number,
                        "filename": f,
                        "original_pdf_filename": self._original_pdf_filename,
                        "filepath": filepath,
                        "url": f"/uploads/{self.case_id}/pdf_pages/{f}"
                    })

            # 按页码排序
            generated_files.sort(key=lambda x: x["page_number"])

            return True, generated_files, ""

        except subprocess.TimeoutExpired:
            return False, [], "PDF 转换超时（超过120秒），文件可能太大"
        except Exception as e:
            return False, [], f"转换异常: {str(e)}"

    def _find_pdftoppm(self) -> Optional[str]:
        """查找 pdftoppm，兼容后台服务 PATH 不包含 Homebrew 路径的情况。"""
        candidates = [
            shutil.which("pdftoppm"),
            "/opt/homebrew/bin/pdftoppm",
            "/usr/local/bin/pdftoppm",
            "/usr/bin/pdftoppm",
        ]
        for candidate in candidates:
            if not candidate or not os.path.exists(candidate):
                continue
            try:
                result = subprocess.run(
                    [candidate, "-v"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=5,
                )
                if result.returncode == 0:
                    return candidate
            except Exception:
                continue
        return None

    def _extract_page_number(self, filename: str, prefix: str) -> int:
        """从文件名提取页码"""
        # 格式: case{id}_{hash}-1.png, case{id}_{hash}-2.png 等
        try:
            # 去掉前缀和扩展名，得到页码
            name_without_prefix = filename.replace(prefix, "")
            # name_without_prefix 应该是 "-1.png", "-2.png" 格式
            page_str = name_without_prefix.replace(".png", "").replace("-", "")
            return int(page_str)
        except:
            return 0

    def delete_page(self, filename: str) -> Tuple[bool, str]:
        """删除指定的转换页面文件"""
        filepath = os.path.join(self.output_dir, filename)
        if os.path.exists(filepath):
            os.remove(filepath)
            return True, "删除成功"
        return False, "文件不存在"

    def delete_all(self) -> int:
        """删除所有转换页面，返回删除数量"""
        count = 0
        for f in os.listdir(self.output_dir):
            filepath = os.path.join(self.output_dir, f)
            if os.path.isfile(filepath):
                os.remove(filepath)
                count += 1
        return count

    def list_pages(self) -> List[dict]:
        """列出所有转换页面"""
        import json
        pages = []
        for f in sorted(os.listdir(self.output_dir)):
            if f.endswith(".png") or f.endswith(".jpg"):
                # 提取前缀
                match = re.match(r"^(case\d+_\w+)-\d+\.png$", f)
                if not match:
                    continue
                prefix = match.group(1)
                # 读取元数据获取原始 PDF 文件名
                meta_path = os.path.join(self.output_dir, prefix + "_meta.json")
                original_pdf_filename = prefix + ".pdf"  # 默认值
                source_type = "pdf"
                original_image_filenames = []
                if os.path.exists(meta_path):
                    try:
                        with open(meta_path, "r", encoding="utf-8") as mf:
                            meta = json.load(mf)
                            original_pdf_filename = meta.get("original_pdf_filename", original_pdf_filename)
                            source_type = meta.get("source_type", source_type)
                            original_image_filenames = meta.get("original_image_filenames", [])
                    except:
                        pass
                filepath = os.path.join(self.output_dir, f)
                prefix = match.group(1)
                pages.append({
                    "page_number": self._extract_page_number(f, prefix),
                    "filename": f,
                    "original_pdf_filename": original_pdf_filename,
                    "source_type": source_type,
                    "original_image_filenames": original_image_filenames,
                    "filepath": filepath,
                    "url": f"/uploads/{self.case_id}/pdf_pages/{f}",
                    "size": os.path.getsize(filepath)
                })
        return pages


def convert_pdf(pdf_path: str, case_id: int) -> Tuple[bool, List[dict], str]:
    """便捷函数：转换单个 PDF"""
    converter = PdfConverter(case_id)
    return converter.convert(pdf_path)
