#!/Users/tommynijem/Sherlock/venv/bin/python
import os
sys.path.append('/Users/tommynijem/Library/Python/3.9/lib/python/site-packages')
import chromadb
from pypdf import PdfReader
from docx import Document
import requests
json
OLLAMA = 'http://localhost:11434/api/embeddings'
EMBED = 'mxbai-embed-large'
CHROMA = chromadb.HttpClient(host='localhost', port=8000)
coll = CHROMA.get_or_create_collection('sherlock_all')

def embed(text):
    r = requests.post(OLLAMA, json={'model': EMBED, 'prompt': text})
    return r.json()['embedding']

path = sys.argv[1]
for root, _, files in os.walk(path):
    for f in files:
        p = os.path.join(root, f)
        text = ''
        try:
            if f.endswith('.pdf'):
                reader = PdfReader(p)
                for page in reader.pages:
                    text += page.extract_text()
            elif f.endswith('.docx'):
                doc = Document(p)
                for para in doc.paragraphs:
                    text += para.text
            elif f.endswith('.txt'):
                with open(p) as fp:
                    text = fp.read()
            if text.strip():
                emb = embed(text[:4000])
                coll.add(ids=[f], embeddings=[emb], documents=[text[:1000]])
                print(f'Indexed {f}')
        except Exception as e:
            print(f'Skip {f}: {e}')
print('Done!')
