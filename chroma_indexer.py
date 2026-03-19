#!/Users/tommynijem/Sherlock/venv/bin/python
import os
import sys
import chromadb
from pypdf import PdfReader
try:
    from docx import Document
    DOCX_OK = True
except ImportError:
    DOCX_OK = False
import requests
import json
import logging

logging.basicConfig(level=logging.INFO)

OLLAMA_URL = 'http://localhost:11434/api/embeddings'
EMBED_MODEL = 'mxbai-embed-large'
CHROMA_HOST = 'localhost'
CHROMA_PORT = 8000
COLLECTION = 'sherlock_cases'

client = chromadb.HttpClient(host=CHROMA_HOST, port=CHROMA_PORT)
coll = client.get_or_create_collection(COLLECTION)

def get_embedding(text):
    payload = {
        'model': EMBED_MODEL,
        'prompt': text
    }
    response = requests.post(OLLAMA_URL, json=payload, timeout=120)
    response.raise_for_status()
    return response.json()['embedding']

if len(sys.argv) < 2:
    print("Usage: python chroma_indexer.py <directory>")
    sys.exit(1)

directory = sys.argv[1]
count = 0
skipped = 0

for root, dirs, files in os.walk(directory):
    for filename in files:
        filepath = os.path.join(root, filename)
        text = ''
        try:
            lower_name = filename.lower()
            if lower_name.endswith('.pdf'):
                reader = PdfReader(filepath)
                for page in reader.pages[:5]:  # Limit POC
                    text += page.extract_text() or ''
            elif lower_name.endswith('.docx') and DOCX_OK:
                doc = Document(filepath)
                for paragraph in doc.paragraphs:
                    text += paragraph.text + '\n'
            elif lower_name.endswith('.txt'):
                with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                    text = f.read()
            elif lower_name.endswith(('.jpg', '.jpeg', '.png', '.tiff')):
                from PIL import Image
                import pytesseract
                img = Image.open(filepath)
                text = pytesseract.image_to_string(img)
            else:
                continue  # Skip other formats
            text = text.strip()
            if text:
                embedding = get_embedding(text[:4000])
                coll.add(
                    ids=[filename],
                    embeddings=[embedding],
                    documents=[text[:1000]],  # Preview
                    metadatas=[{'path': filepath, 'length': len(text)}]
                )
                print(f"Indexed {filename} ({len(text)} chars)")
                count += 1
            else:
                print(f"Empty: {filename}")
        except Exception as e:
            print(f"Skip {filename}: {e}")
            skipped += 1

print(f"\nSummary: {count} indexed, {skipped} skipped.")
print("RAG ready in OpenWebUI - collection 'sherlock_cases'!")
