"""嵌入服务 - stella 1024"""
import os, torch, logging, asyncio, json

logger = logging.getLogger(__name__)
_local_model = None
_local_tokenizer = None

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

async def create_embedding(text: str):
    return _encode(text)

async def create_embeddings_batch(texts: list[str]):
    return _encode(texts)

async def warmup_embedding():
    _get_local_model()
