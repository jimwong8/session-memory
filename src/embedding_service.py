"""嵌入服务 - stella 1024"""
import os, torch, logging, asyncio, json, os as _os

logger = logging.getLogger(__name__)
_local_model = None
_local_tokenizer = None

# ── 高负载降级开关 ──────────────────────────────
# 背景: 13号机无 GPU，embedding 用 CPU 跑 BERT。当消息写入量激增时
# （如 2026-08-18 的 watch 循环堆积事故，2400 条/分），CPU 被 embedding
# 占满导致请求排队。此开关在系统负载过高时跳过 embedding，保护主链路。
# 控制方式:
#   - 环境变量 EMBEDDING_SKIP_ON_LOAD: "1" 启用（默认启用）
#   - 环境变量 EMBEDDING_SKIP_LOAD_THRESHOLD: 负载阈值（默认 15.0，即 loadavg > 15 时跳过写入，检索 query 不跳过）
_EMBED_SKIP_ON_LOAD = _os.environ.get("EMBEDDING_SKIP_ON_LOAD", "1") == "1"
_EMBED_SKIP_LOAD_THRESHOLD = float(_os.environ.get("EMBEDDING_SKIP_LOAD_THRESHOLD", "15.0"))

def _skip_due_to_load() -> bool:
    """系统负载过高时返回 True（跳过 embedding）"""
    if not _EMBED_SKIP_ON_LOAD:
        return False
    try:
        load = _os.getloadavg()[0]
        if load > _EMBED_SKIP_LOAD_THRESHOLD:
            logger.warning(f"系统负载 {load:.1f} 超过阈值 {_EMBED_SKIP_LOAD_THRESHOLD}，跳过 embedding 生成")
            return True
    except Exception:
        pass
    return False

def _get_local_model():
    global _local_model, _local_tokenizer
    if _local_model is None:
        from src.config import settings
        from transformers import BertTokenizer, BertModel, BertConfig
        from safetensors.torch import load_file as sf_load

        path = settings.embedding_model
        logger.info(f"Loading model from: {path}")
        _local_tokenizer = BertTokenizer(vocab_file=os.path.join(path, "vocab.txt"))
        state = sf_load(os.path.join(path, "model.safetensors"))
        state.pop("embeddings.position_ids", None)

        with open(os.path.join(path, "config.json")) as f:
            cfg_dict = json.load(f)
        config = BertConfig(**cfg_dict)
        _local_model = BertModel(config)
        _local_model.load_state_dict(state, strict=False)
        _local_model.eval()
        logger.info(f"Loaded: dim={_local_model.config.hidden_size}")
    return _local_model, _local_tokenizer

def _encode(texts):
    model, tok = _get_local_model()
    if isinstance(texts, str):
        texts = [texts]
    inputs = tok(texts, padding=True, truncation=True, return_tensors="pt", max_length=512)
    with torch.no_grad():
        outputs = model(**inputs)
    mask = inputs["attention_mask"].unsqueeze(-1).expand(outputs.last_hidden_state.size()).float()
    embs = torch.sum(outputs.last_hidden_state * mask, 1) / torch.clamp(mask.sum(1), min=1e-9)
    results = [embs[i].tolist() for i in range(len(texts))]
    return results[0] if len(texts) == 1 else results

async def create_embedding(text: str, force: bool = False):
    if not force and _skip_due_to_load():
        return None
    emb = _encode(text)
    expected = settings.embedding_dimensions if "settings" in dir() else 768
    try:
        expected = _get_expected_dim()
    except Exception:
        expected = 768
    if emb is not None and len(emb) != expected:
        # rebuild local model fresh
        global _local_model, _local_tokenizer
        _local_model = None
        _local_tokenizer = None
        emb = _encode(text)
    return emb


def _get_expected_dim():
    from src.config import settings
    return settings.embedding_dimensions

async def create_embeddings_batch(texts: list[str], force: bool = False):
    if not force and _skip_due_to_load():
        return None
    return _encode(texts)

async def warmup_embedding():
    _get_local_model()
