FROM chromadb/chroma:latest
RUN apt-get update && apt-get install -y python3-pip tesseract-ocr poppler-utils
RUN pip install --no-cache-dir --break-system-packages sentence-transformers pytesseract pypdf pillow unstructured
COPY chroma_indexer.py /usr/local/bin/chroma_indexer.py
RUN chmod +x /usr/local/bin/chroma_indexer.py
