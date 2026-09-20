from loader import chunk_documents
from sentence_transformers import SentenceTransformer
import chromadb
import os
import sys

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "..", "database_chroma")

TENANT_BUILD_CONFIG = {
    "ggsipu": {
        "collection_name": "database_collection",
        "documents_subdir": None,
    },
    "nsut": {
        "collection_name": "nsut_collection",
        "documents_subdir": "nsut",
    },
    "dominos": {
        "collection_name": "dominos_collection",
        "documents_subdir": "dominos",
    },
}


def build_index(collection_name, documents_subdir=None):
    model = SentenceTransformer('BAAI/bge-base-en-v1.5')
    client = chromadb.PersistentClient(path=DB_PATH)
    collection = client.get_or_create_collection(collection_name)

    if documents_subdir:
        documents_dir = os.path.join(BASE_DIR, "..", "documents", documents_subdir)
    else:
        documents_dir = os.path.join(BASE_DIR, "..", "documents")

    chunks = chunk_documents(documents_dir)
    print(f"Loaded {len(chunks)} chunks from {documents_dir}. Starting embedding...")

    chunk_texts = [chunk["text"] for chunk in chunks]
    embeddings = model.encode(chunk_texts, show_progress_bar=True)

    collection.upsert(
        ids=[chunk["id"] for chunk in chunks],
        embeddings=[e.tolist() for e in embeddings],
        documents=chunk_texts,
        metadatas=[{
            "source": chunk["source"],
            "page": chunk["page"],
            "chunk_number": chunk["chunk_number"]
        } for chunk in chunks]
    )

    print(f"Done. Indexed {len(chunks)} chunks into '{collection_name}'.")


if __name__ == "__main__":
    # Usage: python build_index.py            -> builds GGSIPU (default)
    #        python build_index.py nsut       -> builds NSUT
    #        python build_index.py dominos    -> builds Domino's
    tenant = sys.argv[1] if len(sys.argv) > 1 else "ggsipu"

    config = TENANT_BUILD_CONFIG.get(tenant)
    if config is None:
        print(f"Unknown tenant '{tenant}'. Valid options: {list(TENANT_BUILD_CONFIG.keys())}")
    else:
        build_index(config["collection_name"], config["documents_subdir"])