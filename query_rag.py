#!/Users/tommynijem/Sherlock/venv/bin/python
\"\"\"Sherlock CLI RAG Query - NAS Cases.
Chroma retrieve + sherlock-rag LLM.
\"\"\"

import sys
import chromadb
from llama_index.llms.ollama import Ollama
from llama_index.core import PromptTemplate

client = chromadb.HttpClient(host='localhost', port=8000)
coll = client.get_collection('sherlock_cases')

llm = Ollama(model='sherlock-rag', base_url='http://localhost:11434')

PROMPT = PromptTemplate(
    "Context from NAS cases: {context_str}\n\n"
    "Query: {query_str}\n\n"
    "Answer using ONLY context. Structure: Facts | Risks | Precedents | Strategy. No hallucination."
)

if len(sys.argv) < 2:
    print("Usage: python query_rag.py 'query?'")
    sys.exit(1)

query = ' '.join(sys.argv[1:])
results = coll.query(query_texts=[query], n_results=5)

context = '\n---\n'.join([
    f"File: {d['metadata'].get('source', 'unknown')}\nText: {d['documents'][0]}"
    for d in results['metadatas'][0]
])

response = llm.complete(PROMPT.format(context_str=context, query_str=query))
print(response.text)
