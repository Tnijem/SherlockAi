#!/Users/tommynijem/Sherlock/venv/bin/python
"""
Sherlock Simple PDF Indexer POC
PDF text → Ollama embed → Chroma:8000
"""

import os
import sys
import chromadb
from pypdf import PdfReader
import requests
import json

client = chromadb.HttpClient(host='localhost', port=8000)
collection = client.get_or_create_collection('sherlock_simple')

OLLAMA_URL = 'http://localhost:11434/api/embeddings'
EMBED_MODEL = 'mxbai-embed-large'

def get_embedding(text):
    resp = requests.post(OLLAMA_URL, json={'model': EMBED_MODEL, 'prompt': text})
    resp.raise_for_status()
    return resp.json()['embedding']

path = sys.argv[1] if len(sys.argv) > 1 else './mnts/nas1/cases'
count = 0
skipped = 0

for root, dirs, files in os.walk(path):
    for file in files:
        if file.lower().endswith('.pdf'):
            p = os.path.join(root, file)
            try:
                reader = PdfReader(p)
                text = ''
                for page in reader.pages[:10]:  # First 10 pages, POC
                    text += page.extract_text() or ''
                text = text.strip()[:4000]
                if text:
                    emb = get_embedding(text)
                    collection.add(
                        ids=[file],
                        embeddings=[emb],
                        documents=[text[:1000]],  # Preview
                        metadatas=[{'source': p, 'size': len(text)}]
                    )
                    count += 1
                    print(f"✓ Indexed {file} ({len(text)} chars)")
                else:
                    skipped += 1
                    print(f"- Empty {file}")
            except Exception as e:
                skipped += 1
                print(f"- Skip {file}: {e}")

print(f"\n✓ POC complete: {count} PDFs indexed, {skipped} skipped.")
print("Query in OpenWebUI w/ Chroma localhost:8000 collection sherlock_simple!")
