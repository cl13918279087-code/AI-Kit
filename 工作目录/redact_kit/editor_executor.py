"""
editor_sdk 执行层
LLM增强脱敏工具包 - Phase 2

负责：
1. 通过 editor_sdk 执行批量替换
2. 图片 mosaic 处理
3. 索引偏移处理（从后往前替换）
"""

from __future__ import annotations

import json
import logging
import subprocess
import base64
import io
import tempfile
from pathlib import Path
from typing import Optional

from manifest import RedactionManifest, SensitiveEntity, ImageSensitiveItem
from PIL import Image

logger = logging.getLogger("editor_executor")


class EditorSDKExecutor:
    """
    editor_sdk 执行器

    通过 editor_sdk.py 的 CLI 接口操作 Word 文档，
    执行 RedactionManifest 中定义的所有脱敏操作。

    工作流程：
    1. open_file → 获取 file_id
    2. 批量 doc_find_and_replace（按替换内容从后往前，防止索引偏移）
    3. doc_replace_image（对需要 mosaic 的图片）
    4. save_file → 新文件路径
    """

    def __init__(self, config: dict):
        sdk_path = config.get("editor_sdk", {}).get(
            "python_path",
            "/Applications/WorkBuddy.app/Contents/Resources/app.asar.unpacked/resources/builtin-skills/tencent-local-office-edit/edsdk.py"
        )
        self.sdk_cmd = f"python3 {sdk_path}"
        self.config = config

    def _run(self, subcmd: str, **params) -> dict:
        """执行 editor_sdk 命令

        特殊处理异步命令（如 open_file）：
        - 异步命令会立即返回 "open started" 并进入 SSE 流
        - 同步读取第一行（JSON 响应），再启动 SSE 监听线程

        安全修复（P0.3）：使用 shlex.split + shell=False 避免命令注入
        """
        import shlex, threading, time

        # 安全：拆分为列表，用 shell=False 执行
        base_parts = shlex.split(self.sdk_cmd)
        cmd_parts = base_parts + ["call", subcmd]
        for k, v in params.items():
            if isinstance(v, str):
                # 用 shlex.quote 防止字符串内容注入 shell
                cmd_parts.append(f"{k}={shlex.quote(v)}")
            elif isinstance(v, bool):
                if v:
                    cmd_parts.append(f"{k}=true")
            elif isinstance(v, (int, float)):
                cmd_parts.append(f"{k}={v}")
            else:
                cmd_parts.append(f"{k}={shlex.quote(str(v))}")

        logger.debug(f"执行: {' '.join(cmd_parts[:5])}...")

        proc = subprocess.Popen(
            cmd_parts,  # 安全：列表形式，shell=False
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True,
        )

        # 对于 open_file 等异步命令：读取第一行作为响应
        if subcmd in ("open_file",):
            # 读取第一行（异步确认）
            first_line = ""
            def read_async():
                nonlocal first_line
                try:
                    first_line = proc.stdout.readline()
                except Exception:
                    pass
            t = threading.Thread(target=read_async, daemon=True)
            t.start()
            t.join(timeout=5)  # 最多等5秒
            if first_line:
                try:
                    result = json.loads(first_line.strip())
                    logger.debug(f"异步响应: {first_line.strip()[:100]}")
                    # 异步命令会继续在后台运行，启动后台监听
                    self._start_sse_listener(proc, subcmd)
                    return result
                except json.JSONDecodeError:
                    pass

        # 同步命令：等待完整输出
        stdout, stderr = proc.communicate(timeout=60)

        if proc.returncode != 0:
            logger.error(f"editor_sdk 错误: {stderr[:200]}")
            return {"error": stderr[:200]}

        try:
            return json.loads(stdout)
        except json.JSONDecodeError:
            logger.error(f"JSON 解析失败: {stdout[:200]}")
            return {"raw": stdout, "error": "JSON解析失败"}

    def _start_sse_listener(self, proc: subprocess.Popen, subcmd: str):
        """启动后台 SSE 监听线程（用于异步命令）"""
        def listen():
            try:
                for line in proc.stdout:
                    if "error" in line.lower() or "closed" in line.lower():
                        break
                    # 忽略 SSE 数据行
            except Exception:
                pass
        t = threading.Thread(target=listen, daemon=True)
        t.start()
        # 等待文件真正打开
        time.sleep(3)

    def open_file(self, file_path: str) -> str:
        """
        打开文件，返回 editor_sdk 分配的真正 file_id。

        注意：editor_sdk 响应的 JSON 中，文件 ID 字段可能是 "file_id" 或 "id"，
        而非输入的 file_path。后续 get_document_content / execute_manifest
        传入的 file_id 必须使用此返回值。
        """
        logger.info(f"📂 打开文件: {file_path}")
        result = self._run("open_file", file_path=file_path)
        if "error" in result:
            raise RuntimeError(f"打开文件失败: {result['error']}")

        # 尝试从响应中提取真实的 file_id
        file_id = result.get("file_id") or result.get("id") or result.get("data", {}).get("file_id")
        if not file_id:
            logger.warning(
                f"⚠️ editor_sdk 响应中未找到 file_id 字段，响应: {str(result)[:200]}，"
                f"降级使用 file_path 作为 file_id"
            )
            file_id = file_path

        logger.info(f"   文件已打开，file_id={file_id}")
        return file_id

    def get_document_content(self, file_id: str) -> dict:
        """获取文档内容"""
        result = self._run("doc_get_document_content", file_id=file_id)
        if "error" in result:
            raise RuntimeError(f"获取文档内容失败: {result['error']}")
        return result

    def get_images(self, file_id: str) -> list[dict]:
        """获取文档中所有图片"""
        result = self._run("doc_get_images", file_id=file_id)
        if "error" in result:
            logger.warning(f"获取图片列表失败: {result['error']}")
            return []
        return result.get("images", [])

    def save_file(self, file_id: str, output_path: str) -> dict:
        """保存文件到新路径"""
        logger.info(f"💾 保存到: {output_path}")
        result = self._run("save_file", file_id=file_id, file_path=output_path)
        if "error" in result:
            raise RuntimeError(f"保存失败: {result['error']}")
        logger.info(f"   保存成功")
        return result

    def find_and_replace(
        self, file_id: str,
        old_text: str, new_text: str
    ) -> dict:
        """执行单次查找替换"""
        result = self._run(
            "doc_find_and_replace",
            file_id=file_id,
            old_text=old_text,
            new_text=new_text,
        )
        return result

    def execute_manifest(
        self,
        file_id: str,
        manifest: RedactionManifest,
        dry_run: bool = False,
    ) -> dict:
        """
        执行完整的 RedactionManifest

        策略：
        1. 收集所有替换项
        2. 按 old_text 长度降序排序（防止部分匹配）
        3. 从前往后替换（editor_sdk 的 doc_find_and_replace 是全局替换，
           但为避免对已替换内容重复操作，按出现次数降序）
        4. 对每项执行替换并记录结果
        """
        all_replacements = []

        # 收集所有需替换的实体
        for entity in manifest._all_entities():
            if entity.rejected:
                continue
            all_replacements.append({
                "old": entity.text,
                "new": entity.replacement,
                "confidence": entity.confidence,
                "category": entity.category,
                "source": entity.source,
            })

        # 去重（同一 old→new 只执行一次）
        seen = {}
        for r in all_replacements:
            key = r["old"]
            if key not in seen:
                seen[key] = r

        unique = list(seen.values())
        logger.info(f"📝 开始执行 {len(unique)} 项替换（dry_run={dry_run}）")

        results = {
            "total": len(unique),
            "succeeded": 0,
            "failed": 0,
            "skipped": 0,
            "details": [],
        }

        for r in unique:
            if dry_run:
                logger.info(f"   [DRY] {r['old']!r} → {r['new']!r}")
                results["succeeded"] += 1
                continue

            if not r["old"]:
                results["skipped"] += 1
                continue

            # 检查是否已完全替换过（如海峡银行已被福建XX银行覆盖）
            if r["new"] in ["XX银行", "XXX", "YYYY/MM/DD"]:
                # 特殊：检查 old 是否已经是脱敏格式
                if "XX" in r["old"] or "XXX" in r["old"] or "YYYY" in r["old"]:
                    results["skipped"] += 1
                    continue

            result = self.find_and_replace(file_id, r["old"], r["new"])

            status = result.get("status", result.get("message", ""))
            if "error" in result or "未找到" in status:
                results["failed"] += 1
                results["details"].append({
                    "old": r["old"],
                    "new": r["new"],
                    "status": "not_found",
                })
            else:
                results["succeeded"] += 1
                count = result.get("total", "?")
                results["details"].append({
                    "old": r["old"],
                    "new": r["new"],
                    "status": "ok",
                    "count": count,
                })

        logger.info(
            f"   完成: {results['succeeded']}成功 "
            f"{results['failed']}失败 {results['skipped']}跳过"
        )
        return results

    def redact_metadata(self, file_path: str) -> dict:
        """
        Word 文档元数据脱敏（P1.5 合规修复）

        处理 Word 文档中可能含敏感信息的元数据字段：
        - dc:creator（文档作者）
        - cp:lastModifiedBy（最后修改人）
        - cp:keywords（关键词，可能含人员/机构名）
        - Custom Properties（业务自定义字段）

        策略：直接编辑 docx 内部 XML（core.xml / app.xml / custom.xml），
        用 python-docx 的 zipfile 层级操作，无需打开编辑器。

        Args:
            file_path: 文档路径（将被就地修改）

        Returns:
            {"redacted": [字段列表], "errors": [错误列表]}
        """
        import zipfile, shutil, re, os
        from pathlib import Path
        from xml.etree import ElementTree as ET

        NS = {
            "dc": "http://purl.org/dc/elements/1.1/",
            "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
            "dcterms": "http://purl.org/dc/terms/",
            "cts": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
        }

        # 姓名正则（2-4个汉字）
        CHINESE_NAME_RE = re.compile(r'[\u4e00-\u9fa5]{2,4}')
        # 排除词（不视为敏感）
        SAFE_TERMS = {
            "Administrator", "admin", "User", "user",
            "SYSTEM", "system", "Guest", "guest",
        }

        result = {"redacted": [], "errors": []}

        tmp_dir = Path(tempfile.mkdtemp())
        extract_dir = tmp_dir / "docx_extracted"
        extract_dir.mkdir()

        try:
            # 解压 docx
            with zipfile.ZipFile(file_path, "r") as z:
                z.extractall(extract_dir)

            # 1. core.xml — 作者 / 最后修改人
            core_xml = extract_dir / "docProps" / "core.xml"
            if core_xml.exists():
                tree = ET.parse(core_xml)
                root = tree.getroot()
                changed = False
                for tag in ["creator", "lastModifiedBy"]:
                    for elem in root.iter(f"{{{NS['dc']}}}{tag}"):
                        val = (elem.text or "").strip()
                        if not val or val in SAFE_TERMS:
                            continue
                        # 检查是否含中文姓名
                        if CHINESE_NAME_RE.search(val):
                            elem.text = self.config.get("redaction", {}).get(
                                "person_replacement", "XXX"
                            )
                            result["redacted"].append(f"core.xml:{tag}={val}→XXX")
                            changed = True
                if changed:
                    tree.write(core_xml, encoding="utf-8", xml_declaration=True)

            # 2. app.xml — 公司/经理等字段
            app_xml = extract_dir / "docProps" / "app.xml"
            if app_xml.exists():
                tree = ET.parse(app_xml)
                root = tree.getroot()
                changed = False
                # app.xml 命名空间不同，需用通用遍历
                for elem in root.iter():
                    val = (elem.text or "").strip()
                    if not val or val in SAFE_TERMS:
                        continue
                    if CHINESE_NAME_RE.search(val):
                        elem.text = "XXX"
                        result["redacted"].append(f"app.xml:{elem.tag}={val}→XXX")
                        changed = True
                if changed:
                    tree.write(app_xml, encoding="utf-8", xml_declaration=True)

            # 3. custom.xml — 自定义属性（如有）
            custom_xml = extract_dir / "docProps" / "custom.xml"
            if custom_xml.exists():
                tree = ET.parse(custom_xml)
                root = tree.getroot()
                changed = False
                for elem in root.iter():
                    val = (elem.text or "").strip()
                    if not val:
                        continue
                    # 替换所有含敏感信息的文本节点
                    new_val = CHINESE_NAME_RE.sub("XXX", val)
                    if new_val != val:
                        elem.text = new_val
                        result["redacted"].append(f"custom.xml:{elem.tag}={val}→XXX")
                        changed = True
                if changed:
                    tree.write(custom_xml, encoding="utf-8", xml_declaration=True)

            # 重新打包
            tmp_out = tmp_dir / "output.docx"
            with zipfile.ZipFile(tmp_out, "w", zipfile.ZIP_DEFLATED) as z:
                for fp in extract_dir.rglob("*"):
                    if fp.is_file():
                        arcname = fp.relative_to(extract_dir)
                        z.write(fp, arcname)

            # 替换原文件
            shutil.copy2(tmp_out, file_path)
            logger.info(f"   [元数据脱敏] 完成: {result['redacted']}")

        except Exception as e:
            result["errors"].append(str(e))
            logger.error(f"   [元数据脱敏] 失败: {e}")
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)

        return result

    def process_image_mosaic(self, file_id: str, image_url: str, idx: int) -> dict:
        """
        对图片应用马赛克效果

        流程：
        1. 下载图片（或从 image_url 获取）
        2. 用 PIL 应用马赛克
        3. 替换回文档
        """
        try:
            # 尝试下载图片
            local_path = self._download_image(image_url)
            if not local_path:
                logger.warning(f"   无法下载图片 {image_url}，尝试跳过")
                return {"error": "无法获取图片"}

            # 应用马赛克
            blurred = self._apply_mosaic(local_path)

            # 替换回文档
            result = self._run(
                "doc_replace_image",
                file_id=file_id,
                idx=idx,
                old_image_url=image_url,
                new_content=f"file://{blurred}",
            )
            return result

        except Exception as e:
            logger.error(f"   图片处理失败: {e}")
            return {"error": str(e)}

    def _download_image(self, image_url: str) -> Optional[str]:
        """下载图片到临时文件"""
        import urllib.request, tempfile, os

        if not image_url.startswith("http"):
            return image_url  # 本地路径直接返回

        try:
            tmp = tempfile.NamedTemporaryFile(
                suffix=".png", delete=False
            )
            urllib.request.urlretrieve(image_url, tmp.name)
            return tmp.name
        except Exception as e:
            logger.warning(f"下载图片失败: {e}")
            return None

    def _apply_mosaic(self, image_path: str) -> str:
        """
        对图片应用马赛克效果

        使用 PIL，通过缩小后放大的方式实现马赛克
        """
        img = Image.open(image_path)
        width, height = img.size

        # 如果图片太大，先缩小加速处理
        scale = 1.0
        max_dim = 800
        if width > max_dim or height > max_dim:
            scale = max_dim / max(width, height)
            img = img.resize(
                (int(width * scale), int(height * scale)),
                Image.Resampling.BICUBIC
            )

        # 马赛克效果：缩小到 1/20 再放大回
        mosaic_scale = 0.05
        w, h = img.size
        small = img.resize(
            (max(int(w * mosaic_scale), 1), max(int(h * mosaic_scale), 1)),
            Image.Resampling.BICUBIC
        )
        mosaic = small.resize(img.size, Image.Resampling.NEAREST)

        # 保存
        output = image_path.rsplit(".", 1)[0] + "_mosaic.png"
        mosaic.save(output, "PNG")
        logger.info(f"   马赛克图片已生成: {output}")
        return output

    def extract_all_text(self, doc_content: dict) -> dict:
        """
        提取文档中所有文本（段落+表格单元格）。

        兼容 editor SDK 返回的 .doc / .docx 两种 block 格式：

        .docx 格式：
          - 段落 block: type=="paragraph"，文本在 block["text"]
          - 表格 block: type=="table"，单元格在 block["table"]["cells"]

        .doc 格式（editor SDK 格式）：
          - 段落 block: type=="paragraph"，文本在 block["text"]（预览），
                        且可能嵌套在 block["content"][i]["t"] 中（完整内容）
          - 表格 block: type=="table"（注意 .doc 有时也用 type=="text" 且 id 含 "tbl:"）
                        单元格在 block["table"]["cells"]

        为什么之前漏读：
          editor SDK 返回的 .doc block["content"] 是渲染用的分段信息，
          block["text"] 才是对外暴露的完整文本字段。
          但 .doc 文档有时 text 字段为空（编辑器未填充），
          此时需从 block["content"][i]["t"] 拼出完整文本。
        """
        paragraphs = []
        table_cells = []

        for block in doc_content.get("content", {}).get("blocks", []):
            btype = block.get("type", "")

            # ── 段落类型：优先读 block["text"]，空则从 content 拼 ──
            if btype == "paragraph":
                text = block.get("text", "").strip()
                if not text:
                    # .doc 嵌套内容：block["content"][i]["t"]
                    parts = []
                    for item in block.get("content", []):
                        t = item.get("t", "")
                        if t:
                            parts.append(t)
                    text = "".join(parts).strip()
                if text:
                    paragraphs.append(text)

            # ── 表格类型：标准 table block ──
            elif btype == "table":
                table_data = block.get("table", {})
                for cell in table_data.get("cells", []):
                    text = cell.get("text", "").strip()
                    if text:
                        table_cells.append(text)

            # ── .doc 格式的表格 block：type=="text" 且 id 含 "tbl:" ──
            elif btype == "text" and block.get("id", "").startswith("tbl:"):
                table_data = block.get("table", {})
                for cell in table_data.get("cells", []):
                    text = cell.get("text", "").strip()
                    if text:
                        table_cells.append(text)

            # ── .doc 含表格行的 text block（备用逻辑）──
            elif btype == "text" and "table" in block:
                table_data = block.get("table", {})
                for cell in table_data.get("cells", []):
                    text = cell.get("text", "").strip()
                    if text:
                        table_cells.append(text)

        full_text = "\n".join(paragraphs + table_cells)

        return {
            "paragraphs": paragraphs,
            "table_cells": table_cells,
            "full_text": full_text,
            "stats": {
                "paragraphs": len(paragraphs),
                "table_cells": len(table_cells),
                "total_chars": len(full_text),
            }
        }
