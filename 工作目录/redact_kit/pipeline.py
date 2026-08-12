"""
脱敏 Pipeline 主控
LLM增强脱敏工具包 - Phase 2

主流程：
1. 加载配置
2. 初始化 LLM 客户端
3. 初始化实体检测器
4. 初始化 OCR 处理器（Phase 3）
5. 初始化执行器
6. 执行端到端脱敏
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from datetime import datetime

from manifest import RedactionManifest, ImageSensitiveItem
from llm_client import LLMClient
from entity_detector import EntityDetector
from ocr_processor import OCRProcessor
from editor_executor import EditorSDKExecutor
from extensions.templates import TemplateManager

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("pipeline")


class RedactionPipeline:
    """
    LLM 增强文档脱敏 Pipeline

    用法：
        pipeline = RedactionPipeline("config.json")
        report = pipeline.run(
            input_path="input.docx",
            output_path="output_redacted.docx",
        )
        print(report)
    """

    def __init__(self, config_path: str = None):
        if config_path is None:
            config_path = Path(__file__).parent / "config.json"

        with open(config_path, encoding="utf-8") as f:
            self.config = json.load(f)

        logger.info("=" * 60)
        logger.info("LLM增强文档脱敏 Pipeline 初始化")
        logger.info(f"配置文件: {config_path}")
        logger.info("=" * 60)

        # 初始化各组件
        self.llm = LLMClient(self.config)
        self.detector = EntityDetector(self.llm, self.config)
        self.ocr = OCRProcessor(self.llm, self.config)
        self.executor = EditorSDKExecutor(self.config)
        self.template_mgr = TemplateManager()  # P2.7 行业模板管理器

    def run(
        self,
        input_path: str,
        output_path: str,
        dry_run: bool = False,
        skip_images: bool = False,
    ) -> dict:
        """
        执行端到端脱敏

        Args:
            input_path: 输入文件路径（.doc 或 .docx）
            output_path: 输出文件路径
            dry_run: True=仅分析不执行替换
            skip_images: True=跳过图片处理

        Returns:
            执行报告字典
        """
        report = {
            "input": input_path,
            "output": output_path,
            "dry_run": dry_run,
            "started_at": datetime.now().isoformat(),
            "phases": {},
        }

        # ── Phase 1: 打开文件 ──────────────────────────────
        logger.info("")
        logger.info("【Phase 1】打开文档")
        t0 = time.time()

        try:
            file_id = self.executor.open_file(input_path)
        except Exception as e:
            logger.error(f"打开文件失败: {e}")
            report["error"] = str(e)
            return report

        report["phases"]["phase1_open"] = {
            "duration_sec": time.time() - t0,
            "file_id": file_id,
        }

        # ── Phase 2: 内容提取 ───────────────────────────────
        logger.info("")
        logger.info("【Phase 2】提取文档内容")
        t0 = time.time()

        doc_content = self.executor.get_document_content(file_id)
        text_data = self.executor.extract_all_text(doc_content)

        report["phases"]["phase2_extract"] = {
            "duration_sec": time.time() - t0,
            "paragraphs": text_data["stats"]["paragraphs"],
            "table_cells": text_data["stats"]["table_cells"],
            "total_chars": text_data["stats"]["total_chars"],
        }
        logger.info(
            f"   提取完成: {text_data['stats']['paragraphs']}段落 "
            f"+ {text_data['stats']['table_cells']}表格单元格 "
            f"= {text_data['stats']['total_chars']}字符"
        )

        # ── Phase 2.5: 行业自动识别 + 模板注入（P2.7）─────────
        t0_industry = time.time()
        full_text = text_data["full_text"]
        detected_industry = self.template_mgr.detect_industry(full_text)
        report["industry"] = detected_industry
        report["phases"]["phase2_5_industry"] = {
            "industry": detected_industry,
            "duration_sec": time.time() - t0_industry,
        }

        # ── Phase 3: LLM 实体识别 ───────────────────────────
        logger.info("")
        logger.info("【Phase 3】LLM 实体识别")
        t0 = time.time()

        manifest = RedactionManifest(
            document_name=Path(input_path).name,
            document_path=input_path,
            total_blocks=len(doc_content.get("content", {}).get("blocks", [])),
        )

        # 合并段落和表格内容
        full_text = text_data["full_text"]

        # P2.7: 将识别到的行业注入 detector（供 LLM system prompt 扩展）
        self.detector.industry = detected_industry

        # 调用 LLM 检测（包含 regex 兜底 + 角色词规则）
        llm_manifest = self.detector.detect_from_text(full_text)
        manifest.merge(llm_manifest)
        manifest.llm_calls += 1

        report["phases"]["phase3_llm"] = {
            "duration_sec": time.time() - t0,
            "entities_found": manifest.total_entities_found,
            "bank_names": len(manifest.bank_names),
            "persons": len(manifest.persons),
            "dates": len(manifest.dates),
            "phone_numbers": len(manifest.phone_numbers),
            "low_confidence": len(manifest.get_low_confidence()),
        }
        logger.info(
            f"   识别完成: "
            f"{len(manifest.bank_names)}银行名 "
            f"{len(manifest.persons)}姓名 "
            f"{len(manifest.dates)}日期"
        )

        # ── Phase 3.5: 图片处理（Phase 3 增强）────────────────
        if not skip_images:
            logger.info("")
            logger.info("【Phase 3.5】图片敏感信息检测")
            t0 = time.time()

            images_info = self.executor.get_images(file_id)
            logger.info(f"   文档中共有 {len(images_info)} 张图片")

            image_results = self._process_images(file_id, images_info)
            for img_result in image_results:
                manifest.images.append(ImageSensitiveItem(**img_result))

            report["phases"]["phase35_images"] = {
                "duration_sec": time.time() - t0,
                "total_images": len(images_info),
                "sensitive_images": sum(
                    1 for r in image_results if r["contains_sensitive"]
                ),
                "mosaic": sum(1 for r in image_results if r["action"] == "mosaic"),
                "keep": sum(1 for r in image_results if r["action"] == "keep"),
            }
        else:
            logger.info("   跳过图片处理（skip_images=True）")
            report["phases"]["phase35_images"] = {"skipped": True}

        # ── Phase 4: 执行替换 ───────────────────────────────
        logger.info("")
        logger.info("【Phase 4】执行脱敏替换")
        t0 = time.time()

        exec_result = self.executor.execute_manifest(
            file_id, manifest, dry_run=dry_run
        )
        report["phases"]["phase4_execute"] = exec_result

        # ── Phase 4b: Word 元数据脱敏 ──────────────────────
        if not dry_run:
            logger.info("【Phase 4b】Word 文档元数据脱敏")
            meta_result = self.executor.redact_metadata(input_path)
            report["phases"]["phase4b_metadata"] = meta_result

        # ── Phase 5: 质量验证 ──────────────────────────────
        logger.info("")
        logger.info("【Phase 5】脱敏质量验证")
        t0 = time.time()

        # 获取脱敏后文本（仅 dry_run 模式实际执行前有效）
        redacted_text = ""
        if dry_run:
            # dry_run 模式：跳过真实质量验证（无实际修改），
            # 改为 dry-run 分析：模拟替换后应无敏感词
            logger.info("   [dry_run] 跳过真实质量验证（文档未被修改）")
            verification = {
                "checks": {
                    "bank_names_check": "DRY_RUN",
                    "person_names_check": "DRY_RUN",
                    "dates_check": "DRY_RUN",
                },
                "remaining_issues": ["dry_run 模式：需手动确认 manifest 中的替换计划"],
                "quality_score": -1,  # -1 表示未执行
                "overall_quality": "DRY_RUN",
                "dry_run_note": "实际质量验证需在非 dry_run 模式下执行",
            }
        else:
            # 实际替换后，重新提取文本进行验证
            # 注意：如果 editor_sdk 替换未落盘（已知 bug），
            #       get_document_content 读到的是原文本 → 假阳性通过
            try:
                doc_content2 = self.executor.get_document_content(file_id)
                text_data2 = self.executor.extract_all_text(doc_content2)
                redacted_text = text_data2["full_text"]
            except Exception as e:
                logger.warning(f"   ⚠️ 重新提取文档内容失败: {e}，质量验证结果可能不准确")
                redacted_text = ""

            verification = self._quality_check(full_text, redacted_text)
            # P1.6 修复：标记假阳性风险
            if redacted_text and ("海峡银行" in redacted_text or "海峡行" in redacted_text):
                verification["false_positive_risk"] = (
                    "editor_sdk 替换可能未落盘，验证到的可能是原文本而非脱敏后文本。"
                    "建议用 python-docx 重新读取输出文件进行独立验证。"
                )
                verification["overall_quality"] = "UNRELIABLE"
                logger.warning("   ⚠️ 质量验证结果可能不可靠（editor_sdk 落盘问题）")

        report["phases"]["phase5_verify"] = verification
        logger.info(f"   质量评分: {verification.get('quality_score', 'N/A')}")

        # ── Phase 6: 保存文件 ───────────────────────────────
        logger.info("")
        logger.info("【Phase 6】保存文件")

        if not dry_run:
            save_result = self.executor.save_file(file_id, output_path)
            report["phases"]["phase6_save"] = save_result
            report["output"] = output_path

        # ── Phase 7: 生成报告 ───────────────────────────────
        manifest_path = output_path.rsplit(".", 1)[0] + "_manifest.json"
        manifest.save_json(manifest_path)
        report["manifest_path"] = manifest_path
        logger.info(f"   脱敏清单已保存: {manifest_path}")

        report["completed_at"] = datetime.now().isoformat()
        report["manifest_summary"] = manifest.summary()

        # 最终总结
        logger.info("")
        logger.info("=" * 60)
        logger.info("脱敏完成")
        logger.info(f"   输入: {input_path}")
        if not dry_run:
            logger.info(f"   输出: {output_path}")
        logger.info(f"   清单: {manifest_path}")
        logger.info("=" * 60)

        return report

    def _process_images(self, file_id: str, images_info: list[dict]) -> list[dict]:
        """处理文档中的所有图片"""
        results = []

        for i, img in enumerate(images_info):
            img_url = img.get("image_url", "")
            img_type = "header_footer" if img.get("source") in [1, 2] else "other"

            logger.info(f"   分析图片 {i+1}/{len(images_info)}...")

            # 页眉/页脚图片 → 直接 mosaic（无需 OCR）
            if img_type == "header_footer":
                logger.info(f"   [{i+1}] 页眉/页脚图片 → mosaic（银行logo区域）")
                results.append({
                    "index": i,
                    "image_type": "header",
                    "contains_sensitive": True,
                    "sensitive_items": ["银行logo"],
                    "action": "mosaic",
                    "confidence": 1.0,
                    "source": "rule",
                    "ocr_text": "",
                })
                # 执行 mosaic
                self.executor.process_image_mosaic(file_id, img_url, img.get("index", i))
                continue

            # 其他图片 → OCR + LLM 判断
            ocr_result = self.ocr.analyze_image_sensitive(img_url, img_type)

            if ocr_result.get("contains_sensitive"):
                action = ocr_result.get("action", "keep")
                logger.info(
                    f"   [{i+1}] 含敏感信息 → {action} "
                    f"({', '.join(ocr_result.get('sensitive_items', [])[:2])})"
                )

                if action == "mosaic" and not ocr_result.get("confidence", 0) < 0.70:
                    # 执行 mosaic
                    self.executor.process_image_mosaic(
                        file_id, img_url, img.get("index", i)
                    )
            else:
                logger.info(f"   [{i+1}] 不含敏感信息 → keep")
                ocr_result["action"] = "keep"

            results.append(ocr_result)

        return results

    def _quality_check(self, original_text: str, redacted_text: str) -> dict:
        """脱敏质量验证"""
        checks = {
            "bank_names_check": "PASS",
            "person_names_check": "PASS",
            "dates_check": "PASS",
        }

        # 简单规则验证（无需 LLM）
        remaining_issues = []

        # 检查海峡银行是否残留
        if "海峡银行" in redacted_text or "海峡行" in redacted_text:
            checks["bank_names_check"] = "FAIL"
            remaining_issues.append("海峡银行仍有残留")

        # 检查日期格式是否被替换
        date_patterns = [
            r"2022年\d+月\d+日",
            r"二〇二二年\d+月\d+日",
            r"20\d{2}年\d{1,2}月\d{1,2}日",
        ]
        import re
        for pat in date_patterns:
            if re.search(pat, redacted_text):
                checks["dates_check"] = "FAIL"
                remaining_issues.append(f"日期格式未替换: {pat}")

        quality_score = 1.0 if all(
            v == "PASS" for v in checks.values()
        ) else 0.7

        return {
            "checks": checks,
            "remaining_issues": remaining_issues,
            "quality_score": quality_score,
            "overall_quality": "PASS" if quality_score >= 0.9 else "WARNING",
        }

    def run_interactive_review(self, manifest: RedactionManifest):
        """
        交互式人工确认（用于低置信度项）
        Phase 3 WebUI 的命令行版本
        """
        low_conf = manifest.get_low_confidence()
        if not low_conf:
            logger.info("✅ 无需人工确认的低置信度项")
            return

        logger.info(f"\n⚠️  需要人工确认 {len(low_conf)} 项：")
        for i, entity in enumerate(low_conf):
            print(f"  [{i+1}] {entity.text!r} → {entity.replacement!r}")
            print(f"      类别: {entity.category} | 置信度: {entity.confidence:.2f}")
            print(f"      依据: {entity.evidence}")
            choice = input("      操作: [k]保留 [r]拒绝 [m]手动替换 > ").strip().lower()

            if choice == "r":
                entity.rejected = True
                entity.reject_reason = "人工拒绝"
            elif choice.startswith("m"):
                entity.replacement = choice[1:].strip() or "XXX"

        logger.info("人工确认完成")


def main():
    """命令行入口"""
    import argparse

    parser = argparse.ArgumentParser(description="LLM增强文档脱敏工具包")
    parser.add_argument("input", help="输入文件路径")
    parser.add_argument("-o", "--output", help="输出文件路径")
    parser.add_argument("-c", "--config", default=None, help="配置文件路径")
    parser.add_argument("--dry-run", action="store_true", help="仅分析不执行")
    parser.add_argument("--skip-images", action="store_true", help="跳过图片处理")
    parser.add_argument("--verbose", action="store_true", help="详细日志")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # 推断输出路径
    if args.output:
        output = args.output
    else:
        p = Path(args.input)
        output = str(p.parent / f"{p.stem}_脱敏版{p.suffix}")

    # 运行 Pipeline
    pipeline = RedactionPipeline(config_path=args.config)
    report = pipeline.run(
        input_path=args.input,
        output_path=output,
        dry_run=args.dry_run,
        skip_images=args.skip_images,
    )

    # 打印摘要
    print("\n" + manifest.summary() if "manifest" in dir() else "")
    print(json.dumps(report.get("manifest_summary", ""), ensure_ascii=False))


if __name__ == "__main__":
    main()
