"""
FastAPI Web 服务
LLM增强脱敏工具包 - Phase 3

功能：
1. 上传文档 → 自动脱敏 → 预览 + 人工确认队列
2. 低置信度项高亮显示，支持人工确认/拒绝/修改
3. 图片 mosaic 预览
4. 脱敏报告导出

启动方式：
    cd /Users/clzxr/WorkBuddy/Claw/工作目录/redact_kit
    /Users/clzxr/.workbuddy/binaries/python/envs/default/bin/uvicorn app:app --port 8765 --reload
"""

from __future__ import annotations

import os
import json
import logging
import tempfile
from pathlib import Path
from typing import Optional
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, BackgroundTasks
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from manifest import RedactionManifest, SensitiveEntity
from llm_client import LLMClient
from entity_detector import EntityDetector
from ocr_processor import OCRProcessor
from editor_executor import EditorSDKExecutor
from pipeline import RedactionPipeline

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("app")

app = FastAPI(
    title="文档脱敏工具包",
    description="LLM增强型文档脱敏服务",
    version="1.0.0",
)

# ============================================================
# 数据模型
# ============================================================

class RedactionRequest(BaseModel):
    skip_images: bool = False
    dry_run: bool = False


class ConfirmItem(BaseModel):
    text: str
    replacement: str
    action: str  # "confirm" | "reject" | "modify"
    new_replacement: Optional[str] = None


class ConfirmRequest(BaseModel):
    manifest_path: str
    items: list[ConfirmItem]


# ============================================================
# 全局状态（简单会话管理）
# ============================================================

class SessionStore:
    """会话存储（内存 + SQLite 持久化）— P3.4 合规修复"""
    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = Path(__file__).parent / "sessions.db"
        self.db_path = db_path
        self.sessions = {}  # 内存缓存：session_id -> kwargs（不含 manifest/report 大对象）
        self._init_db()

    def _init_db(self):
        """初始化 SQLite 表"""
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                input_path TEXT,
                output_path TEXT,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                updated_at TEXT,
                manifest_summary TEXT,
                operator TEXT
            )
        """)
        conn.commit()
        conn.close()

    def create(self, session_id: str, **kwargs):
        import sqlite3, json
        now = datetime.now().isoformat()
        self.sessions[session_id] = kwargs
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "INSERT OR REPLACE INTO sessions "
            "(session_id, input_path, output_path, status, created_at, updated_at, operator) "
            "VALUES (?, ?, ?, 'pending', ?, ?, ?)",
            (session_id, kwargs.get("input_path", ""), kwargs.get("output_path", ""),
             now, now, kwargs.get("operator", ""))
        )
        conn.commit()
        conn.close()

    def get(self, session_id: str) -> dict:
        # 优先从内存（不含大对象）
        base = self.sessions.get(session_id, {})
        # 从 SQLite 补全元数据
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        row = conn.execute(
            "SELECT status, created_at, updated_at, manifest_summary FROM sessions "
            "WHERE session_id = ?", (session_id,)
        ).fetchone()
        conn.close()
        if row:
            base.setdefault("status", row[0])
            base.setdefault("created_at", row[1])
            base.setdefault("updated_at", row[2])
            base.setdefault("manifest_summary", row[3])
        return base

    def update(self, session_id: str, **kwargs):
        import sqlite3, json
        self.sessions[session_id] = {**self.sessions.get(session_id, {}), **kwargs}
        now = datetime.now().isoformat()
        manifest_summary = kwargs.get("manifest_summary", "")
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            "UPDATE sessions SET status=?, updated_at=?, manifest_summary=? "
            "WHERE session_id = ?",
            (kwargs.get("status", "in_progress"), now,
             str(manifest_summary)[:500], session_id)
        )
        conn.commit()
        conn.close()

    def delete(self, session_id: str):
        import sqlite3
        self.sessions.pop(session_id, None)
        conn = sqlite3.connect(self.db_path)
        conn.execute("DELETE FROM sessions WHERE session_id = ?", (session_id,))
        conn.commit()
        conn.close()


store = SessionStore()

# 获取工作目录
WORK_DIR = Path("/Users/clzxr/WorkBuddy/Claw/工作目录")
UPLOAD_DIR = WORK_DIR / "redact_kit" / "uploads"
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

# 挂载静态文件
STATIC_DIR = WORK_DIR / "redact_kit" / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ============================================================
# 工具函数
# ============================================================

def get_pipeline_config() -> dict:
    """加载 pipeline 配置"""
    config_path = Path(__file__).parent / "config.json"
    with open(config_path, encoding="utf-8") as f:
        return json.load(f)


def save_upload(file: UploadFile) -> str:
    """保存上传文件，返回路径"""
    path = UPLOAD_DIR / f"{datetime.now().strftime('%s')}_{file.filename}"
    with open(path, "wb") as f:
        content = file.file.read()
        f.write(content)
    return str(path)


def render_review_page(session: dict) -> str:
    """渲染人工确认页面 HTML"""
    manifest = session.get("manifest")
    if manifest is None:
        return "<h1>会话无效或已过期</h1>"

    all_entities = (
        manifest.bank_names + manifest.persons + manifest.dates +
        manifest.phone_numbers + manifest.id_numbers + manifest.accounts
    )

    high = [e for e in all_entities if e.confidence >= 0.90 and not e.rejected]
    medium = [e for e in all_entities if 0.70 <= e.confidence < 0.90 and not e.rejected]
    low = [e for e in all_entities if e.confidence < 0.70 and not e.rejected]
    rejected = manifest.rejected

    def entity_rows(entities, tier_label):
        rows = []
        for e in entities:
            color = {
                "HIGH": "#d4edda",
                "MEDIUM": "#fff3cd",
                "LOW": "#f8d7da"
            }.get(tier_label, "#ffffff")

            rows.append(f"""
            <tr style="background:{color}">
              <td>{e.category}</td>
              <td><code>{e.text}</code></td>
              <td><code>{e.replacement}</code></td>
              <td>{e.confidence:.2f}</td>
              <td>{e.source}</td>
              <td>{e.evidence[:60] if e.evidence else '-'}</td>
              <td>
                <button name="action_confirm" data-text="{e.text}" data-rep="{e.replacement}">✅确认</button>
                <button name="action_reject" data-text="{e.text}">❌拒绝</button>
                <input type="text" name="modify_{e.text}" placeholder="手动替换值" style="width:80px"/>
              </td>
            </tr>""")
        return "\n".join(rows) if rows else "<tr><td colspan='7'>无</td></tr>"

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>脱敏确认 - {session.get('input_name','')}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 20px; background: #f5f5f5; }}
  .container {{ max-width: 1400px; margin: 0 auto; background: white; padding: 24px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
  h1 {{ color: #333; border-bottom: 2px solid #007bff; padding-bottom: 8px; }}
  h2 {{ color: #555; margin-top: 24px; }}
  table {{ width: 100%; border-collapse: collapse; margin: 12px 0; font-size: 13px; }}
  th {{ background: #343a40; color: white; padding: 8px 6px; text-align: left; }}
  td {{ padding: 6px; border-bottom: 1px solid #dee2e6; vertical-align: middle; }}
  code {{ background: #f1f3f5; padding: 2px 6px; border-radius: 3px; font-size: 12px; }}
  .stats {{ display: flex; gap: 20px; margin: 16px 0; }}
  .stat-box {{ background: #e9ecef; padding: 12px 20px; border-radius: 6px; text-align: center; }}
  .stat-box .num {{ font-size: 24px; font-weight: bold; color: #007bff; }}
  .stat-box .label {{ font-size: 12px; color: #666; }}
  .tier-HIGH {{ color: #28a745; }} .tier-MEDIUM {{ color: #ffc107; }} .tier-LOW {{ color: #dc3545; }}
  .btn-primary {{ background: #007bff; color: white; border: none; padding: 12px 32px; border-radius: 6px; font-size: 16px; cursor: pointer; }}
  .btn-primary:hover {{ background: #0056b3; }}
  .progress-bar {{ height: 8px; background: #e9ecef; border-radius: 4px; overflow: hidden; margin: 8px 0; }}
  .progress-fill {{ height: 100%; background: linear-gradient(90deg, #28a745, #17a2b8); }}
</style>
</head>
<body>
<div class="container">
  <h1>🔒 文档脱敏确认 - {session.get('input_name','')}</h1>

  <div class="stats">
    <div class="stat-box"><div class="num">{len(high)}</div><div class="label">高置信（直接执行）</div></div>
    <div class="stat-box"><div class="num">{len(medium)}</div><div class="label">中置信（规则验证）</div></div>
    <div class="stat-box"><div class="num">{len(low)}</div><div class="label">低置信（需人工）</div></div>
    <div class="stat-box"><div class="num">{len(rejected)}</div><div class="label">已过滤（误脱风险）</div></div>
  </div>

  <form id="confirmForm" method="post" action="/api/confirm">
    <input type="hidden" name="session_id" value="{session.get('session_id','')}">
    <input type="hidden" name="manifest_path" value="{session.get('manifest_path','')}">
    <input type="hidden" name="output_path" value="{session.get('output_path','')}">
    <input type="hidden" name="file_id" value="{session.get('file_id','')}">

    <h2>🔴 低置信度项（需人工确认）<span class="tier-LOW">⛔ {len(low)}项</span></h2>
    <table>
      <thead><tr><th>类别</th><th>原文</th><th>替换值</th><th>置信度</th><th>来源</th><th>依据</th><th>操作</th></tr></thead>
      <tbody>{entity_rows(low, 'LOW')}</tbody>
    </table>

    <h2>🟡 中置信度项（规则验证）<span class="tier-MEDIUM">{len(medium)}项</span></h2>
    <table>
      <thead><tr><th>类别</th><th>原文</th><th>替换值</th><th>置信度</th><th>来源</th><th>依据</th><th>操作</th></tr></thead>
      <tbody>{entity_rows(medium, 'MEDIUM')}</tbody>
    </table>

    <h2>🟢 高置信度项（直接执行）<span class="tier-HIGH">✅ {len(high)}项</span></h2>
    <table>
      <thead><tr><th>类别</th><th>原文</th><th>替换值</th><th>置信度</th><th>来源</th><th>依据</th></tr></thead>
      <tbody>{entity_rows(high, 'HIGH')}</tbody>
    </table>

    <div style="margin-top: 24px; text-align: center;">
      <button type="submit" class="btn-primary">🚀 执行脱敏替换</button>
    </div>
  </form>

  <div style="margin-top: 24px; padding: 16px; background: #f8f9fa; border-radius: 6px;">
    <h3>📋 执行摘要</h3>
    <pre>{manifest.summary()}</pre>
  </div>
</div>

<script>
document.getElementById('confirmForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const form = e.target;
  const data = new FormData(form);
  
  // 收集低置信度项的人工决策
  document.querySelectorAll('[name="action_reject"]').forEach(btn => {{
    data.append('rejected', btn.dataset.text);
  }});
  document.querySelectorAll('[name="action_confirm"]').forEach(btn => {{
    data.append('confirmed', btn.dataset.text + ':' + btn.dataset.rep);
  }});
  document.querySelectorAll('input[name^="modify_"]').forEach(inp => {{
    if (inp.value) {{
      const text = inp.name.replace('modify_', '');
      data.append('modified', text + ':' + inp.value);
    }}
  }});

  const resp = await fetch('/api/confirm', {{ method: 'POST', body: data }});
  const result = await resp.json();
  if (result.success) {{
    alert('脱敏完成！输出文件: ' + result.output_path);
    window.location.href = '/static/download.html?path=' + encodeURIComponent(result.output_path);
  }} else {{
    alert('错误: ' + result.error);
  }}
}});
</script>
</body>
</html>"""
    return html


# ============================================================
# API 路由
# ============================================================

@app.get("/", response_class=HTMLResponse)
async def index():
    """上传页面"""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>LLM增强文档脱敏工具包</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 0; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); min-height: 100vh; display: flex; align-items: center; justify-content: center; }}
  .card {{ background: white; padding: 40px; border-radius: 16px; box-shadow: 0 20px 60px rgba(0,0,0,0.3); width: 480px; }}
  h1 {{ color: #333; font-size: 24px; margin-bottom: 8px; }}
  .subtitle {{ color: #666; margin-bottom: 32px; }}
  .drop-zone {{ border: 2px dashed #667eea; border-radius: 12px; padding: 40px; text-align: center; cursor: pointer; transition: all 0.3s; }}
  .drop-zone:hover {{ border-color: #764ba2; background: #f8f5ff; }}
  .drop-zone.dragover {{ border-color: #764ba2; background: #f0e8ff; }}
  input[type=file] {{ display: none; }}
  .hint {{ color: #999; font-size: 12px; margin-top: 8px; }}
  .options {{ margin: 16px 0; }}
  .options label {{ display: flex; align-items: center; gap: 8px; margin: 8px 0; cursor: pointer; }}
  .btn {{ background: linear-gradient(135deg, #667eea, #764ba2); color: white; border: none; padding: 14px 40px; border-radius: 8px; font-size: 16px; cursor: pointer; width: 100%; margin-top: 16px; }}
  .btn:hover {{ opacity: 0.9; }}
  .mock-badge {{ background: #ffc107; color: #333; padding: 4px 12px; border-radius: 20px; font-size: 12px; display: inline-block; margin-bottom: 16px; }}
</style>
</head>
<body>
<div class="card">
  <span class="mock-badge">⚠️ DEMO模式（无API Key时）</span>
  <h1>🔒 LLM增强文档脱敏工具包</h1>
  <p class="subtitle">智能识别 + 规则验证 + 人工确认</p>

  <form id="uploadForm" enctype="multipart/form-data" method="post" action="/api/upload">
    <div class="drop-zone" id="dropZone" onclick="document.getElementById('file').click()">
      <div style="font-size: 48px; margin-bottom: 12px;">📄</div>
      <div>拖拽文档到这里，或 <strong>点击选择文件</strong></div>
      <div class="hint">支持 .doc, .docx, .wps 格式</div>
      <input type="file" name="file" id="file" accept=".doc,.docx,.wps" required>
    </div>

    <div class="options">
      <label><input type="checkbox" name="dry_run" value="true"> 干跑模式（仅分析，不实际替换）</label>
      <label><input type="checkbox" name="skip_images" value="true"> 跳过图片处理</label>
    </div>

    <button type="submit" class="btn">🚀 开始脱敏分析</button>
  </form>
</div>
<script>
const dz = document.getElementById('dropZone');
dz.addEventListener('dragover', e => {{ e.preventDefault(); dz.classList.add('dragover'); }});
dz.addEventListener('dragleave', () => dz.classList.remove('dragover'));
dz.addEventListener('drop', e => {{
  e.preventDefault();
  dz.classList.remove('dragover');
  document.getElementById('file').files = e.dataTransfer.files;
}});
</script>
</body>
</html>"""


@app.post("/api/upload")
async def upload_and_analyze(
    background: BackgroundTasks,
    file: UploadFile = File(...),
    dry_run: bool = Form(False),
    skip_images: bool = Form(False),
):
    """上传文件并启动分析"""
    logger.info(f"📤 上传文件: {file.filename} (dry_run={dry_run})")

    # 保存上传文件
    input_path = save_upload(file)

    # 生成会话 ID
    import uuid
    session_id = str(uuid.uuid4())[:8]

    # 推断输出路径
    p = Path(file.filename)
    output_path = str(WORK_DIR / f"{p.stem}_脱敏版{p.suffix}")

    try:
        # 初始化 Pipeline
        config = get_pipeline_config()
        pipeline = RedactionPipeline()

        # Phase 1-3：打开文件、提取内容、LLM识别
        file_id = pipeline.executor.open_file(input_path)
        doc_content = pipeline.executor.get_document_content(file_id)
        text_data = pipeline.executor.extract_all_text(doc_content)
        full_text = text_data["full_text"]

        # LLM 实体检测
        manifest = RedactionManifest(
            document_name=file.filename,
            document_path=input_path,
            total_blocks=len(doc_content.get("content", {}).get("blocks", [])),
        )
        llm_manifest = pipeline.detector.detect_from_text(full_text)
        manifest.merge(llm_manifest)

        # 保存 manifest
        manifest_path = output_path.rsplit(".", 1)[0] + "_manifest.json"
        manifest.save_json(manifest_path)

        # 存入会话
        store.create(
            session_id,
            session_id=session_id,
            input_path=input_path,
            output_path=output_path,
            input_name=file.filename,
            manifest=manifest,
            manifest_path=manifest_path,
            file_id=file_id,
            report={},
        )

        # 返回确认页面
        html = render_review_page(store.get(session_id))
        return HTMLResponse(content=html)

    except Exception as e:
        logger.error(f"分析失败: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/confirm")
async def confirm_and_execute(
    background: BackgroundTasks,
    session_id: str = Form(...),
    manifest_path: str = Form(...),
    output_path: str = Form(...),
    file_id: str = Form(...),
    confirmed: list[str] = Form(default=[]),
    rejected: list[str] = Form(default=[]),
    modified: list[str] = Form(default=[]),
):
    """执行脱敏替换（基于人工确认结果）"""
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话无效或已过期")

    manifest = session.get("manifest")
    if manifest is None:
        raise HTTPException(status_code=404, detail="Manifest 不存在")

    # 应用人工决策
    # 1. 拒绝低置信项
    for text in rejected:
        for entity in manifest._all_entities():
            if entity.text == text:
                entity.rejected = True
                entity.reject_reason = "人工拒绝"

    # 2. 确认/修改
    for item in modified:
        if ":" not in item:
            continue
        text, new_rep = item.split(":", 1)
        for entity in manifest._all_entities():
            if entity.text == text:
                entity.replacement = new_rep

    # 执行替换
    config = get_pipeline_config()
    executor = EditorSDKExecutor(config)

    try:
        exec_result = executor.execute_manifest(file_id, manifest, dry_run=False)

        # 保存
        executor.save_file(file_id, output_path)

        # 更新 manifest
        manifest.save_json(manifest_path)

        return JSONResponse({
            "success": True,
            "output_path": output_path,
            "manifest_path": manifest_path,
            "exec_result": exec_result,
        })

    except Exception as e:
        logger.error(f"执行失败: {e}")
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/status/{session_id}")
async def get_status(session_id: str):
    """查询处理状态"""
    session = store.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="会话不存在")

    manifest = session.get("manifest")
    return {
        "session_id": session_id,
        "manifest_summary": manifest.summary() if manifest else "",
        "file_id": session.get("file_id", ""),
    }


# ============================================================
# 静态文件：下载完成页
# ============================================================

DOWNLOAD_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>脱敏完成</title>
<style>
  body { font-family: -apple-system, sans-serif; display: flex; align-items: center; justify-content: center; min-height: 100vh; margin: 0; background: #f5f5f5; }
  .card { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.1); text-align: center; }
  h1 { color: #28a745; }
  .path { background: #f8f9fa; padding: 12px 20px; border-radius: 6px; font-family: monospace; margin: 20px 0; }
  a { color: #007bff; text-decoration: none; }
</style>
</head>
<body>
<div class="card">
  <h1>✅ 脱敏完成！</h1>
  <p>文件已保存到：</p>
  <div class="path" id="outputPath">加载中...</div>
  <p><a href="/" onclick="window.close()">← 继续处理其他文档</a></p>
</div>
<script>
const params = new URLSearchParams(window.location.search);
const path = decodeURIComponent(params.get('path') || '');
document.getElementById('outputPath').textContent = path;
</script>
</body>
</html>"""

@app.get("/static/download.html", response_class=HTMLResponse)
async def download_page():
    return DOWNLOAD_HTML


# ============================================================
# 启动入口
# ============================================================

def start_server(port: int = 8765):
    """启动 Web 服务"""
    import uvicorn
    logger.info(f"🚀 启动 Web 服务: http://localhost:{port}")
    logger.info(f"   上传目录: {UPLOAD_DIR}")
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


if __name__ == "__main__":
    start_server()
