
"""KG Governance — noise filtering, dedup, confidence decay."""
from __future__ import annotations
import logging
import re
from typing import Set

from sqlalchemy import select, func, text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import KGEntity, KGRelation

logger = logging.getLogger(__name__)

NOISE_WORDS: Set[str] = {
    "grep", "find", "cat", "ls", "cd", "rm", "cp", "mv",
    "chmod", "chown", "sudo", "ssh", "curl", "wget", "ping",
    "top", "ps", "kill", "diff", "sort", "awk", "sed", "tee",
    "tar", "zip", "unzip", "git", "docker", "kubectl", "npm",
    "pip", "yarn", "pnpm", "bash", "zsh", "sh", "echo",
    "mkdir", "touch", "ln", "wc", "head", "tail", "less", "more",
    "vim", "nano", "code", "api", "test", "tests", "error",
    "errors", "fix", "update", "add", "remove", "delete", "create",
    "get", "set", "init", "start", "stop", "run", "build",
    "deploy", "config", "setup", "install", "version", "name",
    "type", "value", "key", "id", "data", "info", "log", "file",
    "folder", "path", "url", "link", "user", "admin", "system",
    "server", "client", "host", "port", "ip", "dns", "http",
    "https", "ssl", "tcp", "udp", "readme", "license",
    "changelog", "todo", "note", "notes", "example", "examples",
    "doc", "docs", "to-do", "to do", "to-do list", "todo list",
    "名字", "名称", "类型", "数据",
    "信息", "文件", "错误", "修复",
    "更新", "添加", "删除", "创建",
    "获取", "设置", "运行", "部署",
    "配置", "安装", "测试", "服务",
    "系统", "用户", "管理", "日志",
    "步骤", "方法", "问题", "方案",
    "计划", "分析", "结果", "功能",
    "模块", "代码", "项目", "工具",
    "命令", "参数", "选项", "默认",
    "输入", "输出", "开始", "结束",
    "完成", "成功", "失败", "注意",
}

NOISE_PATTERNS = [
    re.compile(r"^[^a-zA-Z一-鿿]+$"),  # no letters/chinese
    re.compile(r"^.{1,2}$"),                      # too short
]


def is_noise_entity(name: str, entity_type: str) -> bool:
    if not name or not isinstance(name, str):
        return True
    if name.lower().strip() in NOISE_WORDS:
        return True
    for pat in NOISE_PATTERNS:
        if pat.match(name):
            return True
    return False


async def filter_and_dedup_entities(
    db: AsyncSession, entities: list[dict], session_id: str,
) -> list[dict]:
    if not entities:
        return []
    cleaned = []
    seen = set()
    for ent in entities:
        name = ent.get("name", "").strip()
        ent_type = ent.get("type", "concept").strip()
        if not name or is_noise_entity(name, ent_type):
            continue
        key = (name.lower(), ent_type)
        if key in seen:
            continue
        seen.add(key)
        stmt = select(KGEntity).where(
            KGEntity.session_id == session_id,
            func.lower(KGEntity.name) == name.lower(),
            KGEntity.entity_type == ent_type,
        )
        result = await db.execute(stmt)
        if result.scalar_one_or_none():
            continue
        cleaned.append({"name": name, "type": ent_type})
    return cleaned


async def get_noise_stats(db: AsyncSession) -> dict:
    rows = (await db.execute(select(KGEntity.name, KGEntity.entity_type))).all()
    noise = sum(1 for n, t in rows if is_noise_entity(n or "", t or ""))
    return {"total": len(rows), "noise": noise,
            "pct": round(noise * 100.0 / max(len(rows), 1), 1)}
