FROM pytorch/pytorch:2.5.1-cuda12.4-cudnn9-runtime

WORKDIR /app

# 系统依赖 (包含 SSH 监控探针所需 openssh-client & sshpass)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ libpq-dev curl openssh-client sshpass \
    && rm -rf /var/lib/apt/lists/*

# 安装Python依赖（torch已在基础镜像中）
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    fastapi==0.115.0 \
    uvicorn[standard]==0.32.0 \
    pydantic==2.9.0 \
    pydantic-settings==2.5.0 \
    sqlalchemy==2.0.35 \
    asyncpg==0.29.0 \
    alembic==1.13.3 \
    pgvector==0.3.5 \
    redis==5.1.1 \
    hiredis==3.0.0 \
    sentence-transformers==3.3.1 \
    tiktoken==0.8.0 \
    prometheus-client==0.21.0 \
    python-dotenv==1.0.1 \
    httpx==0.27.2 \
    openai==1.57.4

# 构建时校验：确保 SSH 依赖存在
RUN command -v sshpass && command -v ssh && echo "SSH dependencies verified"

# 复制源码
COPY src/ src/

# 入口脚本（启动前依赖检查）
COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 8000

CMD ["/entrypoint.sh"]
