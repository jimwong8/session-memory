# 凭据清单

| 变量名 | 当前位置 | 建议迁移目标 |
|--------|----------|-------------|
| POSTGRES_PASSWORD | .env | Docker secrets / 环境变量注入 |
| GRAFANA_PASSWORD | .env | Docker secrets / 环境变量注入 |
| OPENAI_API_KEY | .env | Docker secrets / 环境变量注入 |