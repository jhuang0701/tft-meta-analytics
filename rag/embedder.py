from sentence_transformers import SentenceTransformer
import numpy as np

EMBED_MODEL = "BAAI/bge-small-en-v1.5"
EMBED_DIM   = 384
_model = None

def _get_model():
    global _model
    if _model is None:
        _model = SentenceTransformer(EMBED_MODEL)
        test_vec = _model.encode(["test"], normalize_embeddings=True)
        assert test_vec.shape[1] == EMBED_DIM, f"Got {test_vec.shape[1]}-dim, expected {EMBED_DIM}"
    return _model

def embed_texts(texts):
    vecs = _get_model().encode(texts, normalize_embeddings=True)
    return vecs.tolist()

def embed_query(query):
    return embed_texts([query])[0]

def embed_documents(texts):
    return embed_texts(texts)  