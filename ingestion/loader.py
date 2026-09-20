import os
import fitz # PyMuPDF
from pathlib import Path
from langchain_text_splitters import RecursiveCharacterTextSplitter

# -------------------------------
# Configure Chunking
# -------------------------------
CHUNK_SIZE = 800
CHUNK_OVERLAP = 150

splitter = RecursiveCharacterTextSplitter(
    chunk_size=CHUNK_SIZE,
    chunk_overlap=CHUNK_OVERLAP,
    separators=[
        "\n\n",  # paragraph
        "\n",  # line
        ". ",  # sentence
        " ",  # word
        "",  # character (last resort)
    ],
)


# -------------------------------
# TXT Loader
# -------------------------------
def load_txt(path: str):
    with open(path, "r", encoding="utf-8") as f:
        text = f.read()

    return [{"page": 1, "text": text}]


# -------------------------------
# PDF Loader
# -------------------------------
def load_pdf(path: str):
    doc = fitz.open(path)

    pages = []

    for page_number, page in enumerate(doc, start=1):
        text = page.get_text("text").strip()

        if text:
            pages.append({"page": page_number, "text": text})

    doc.close()

    return pages


# -------------------------------
# Load Single File
# -------------------------------
def load_file(path: str):

    suffix = Path(path).suffix.lower()

    if suffix == ".pdf":
        return load_pdf(path)

    elif suffix == ".txt":
        return load_txt(path)

    return []


# -------------------------------
# Recursive Folder Loader
# -------------------------------
def load_documents(folder):

    documents = []

    for root, _, files in os.walk(folder):

        for filename in files:

            if not filename.lower().endswith((".pdf", ".txt")):
                continue

            full_path = os.path.join(root, filename)

            pages = load_file(full_path)

            documents.append({"source": filename, "path": full_path, "pages": pages})

    return documents


# -------------------------------
# Chunk Documents
# -------------------------------
def chunk_documents(folder):

    chunks = []

    global_chunk = 0

    documents = load_documents(folder)

    for document in documents:

        source = document["source"]

        for page in document["pages"]:

            page_number = page["page"]

            page_chunks = splitter.split_text(page["text"])

            for local_chunk, chunk in enumerate(page_chunks):

                chunks.append(
                    {
                        "id": f"{source}_page{page_number}_chunk{local_chunk}",
                        "source": source,
                        "page": page_number,
                        "chunk_number": global_chunk,
                        "text": chunk,
                    }
                )

                global_chunk += 1

    return chunks


# -------------------------------
# Test
# -------------------------------
if __name__ == "__main__":

    chunks = chunk_documents("../documents")

    print(f"\nTotal Chunks : {len(chunks)}\n")

    print("First Chunk\n")
    print(chunks[0])

    print("\n--------------------------------------\n")

    print(chunks[0]["text"])
