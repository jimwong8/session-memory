#!/usr/bin/env python3
"""单条 KG 提取测试 - 诊断 LLM 真实返回"""
import os, json, re, sys
from openai import OpenAI

# 读 .env 里的 key
keys = []
env_path = "/app/scripts/../.env"
for p in ["/app/.env", "/app/scripts/../.env"]:
    try:
        for line in open(p):
            line = line.strip()
            if line.startswith("BACKUP_OPENAI_API_KEY="):
                keys = [k.strip() for k in line.split("=", 1)[1].split(",") if k.strip()]
                break
        if keys:
            break
    except Exception:
        pass

if not keys:
    keys = [os.getenv("BACKUP_OPENAI_API_KEY", "")]
    print("用环境变量 BACKUP_OPENAI_API_KEY")
else:
    print(f"从 .env 读到 {len(keys)} 个 key")

base = os.getenv("BACKUP_OPENAI_BASE_URL", "https://api.edgefn.net/v1")
print(f"base_url = {base}")
print(f"key[0] = {keys[0][:8]}...")

content = "服务器10.100.1.13运行Session Memory系统,依赖postgres和redis,用uvicorn提供服务。"
prompt = (
    "你是一个知识图谱提取引擎。只输出JSON，禁止markdown。\n"
    "允许实体类型: file, function, concept, error, tool, person, product, technology\n"
    "允许关系: depends_on, modifies, fixes, exemplifies, prefers_over, contradicts, reinforces, evolved_into, invalidated_by, leads_to, derived_from, part_of\n\n"
    '格式:\n{"entities":[{"name":"x","type":"concept","confidence":0.9}],"relations":[{"source":"a","target":"b","type":"depends_on","confidence":0.8}]}\n\n'
    "要求:\n- 实体名称必须在原文中出现\n- 置信度 0-1，低于 0.7 的不要输出\n- 只提取原文中明确提到的信息\n\n"
    "内容:\n" + content
)

for model in ["KAT-Coder-Exp-72B-1010", "DeepSeek-V3.2-EXP", "DeepSeek-V4-Flash"]:
    print(f"\n=== {model} ===")
    try:
        client = OpenAI(api_key=keys[0], base_url=base, timeout=30)
        r = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}],
            temperature=0, max_tokens=512
        )
        t = r.choices[0].message.content or ""
        print(f"  返回长度: {len(t)}")
        print(f"  内容前200: {t[:200]}")
        # 尝试解析 JSON
        m = re.search(r"\{[\s\S]*\}", t)
        if m:
            try:
                d = json.loads(m.group(0))
                print(f"  JSON解析成功: entities={len(d.get('entities',[]))}, relations={len(d.get('relations',[]))}")
            except Exception as e:
                print(f"  JSON解析失败: {e}")
        else:
            print(f"  未找到JSON")
    except Exception as e:
        print(f"  异常: {type(e).__name__}: {str(e)[:200]}")
