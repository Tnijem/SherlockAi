# Sherlock Sizing Model

## Overview
This model recommends Mac Mini configurations for Sherlock, a local/offline RAG AI for law firms. Factors:
- **Doc Size (GB)**: Current total document volume (PDFs, Office files).
- **Monthly Delta**: New files/month and GB/month (for growth planning).
- **Concurrent Users**: 1-20 (affects query load; assumes sequential-ish due to local).
- **Key Metrics**:
  - Index time (full reindex hours, est. on recommended CPU).
  - Query latency (avg seconds).
  - Chroma memory (GB RAM for vector DB, in-memory mode).

Assumptions:
- Embeddings: 768-dim (~3KB/vector).
- Chunks: ~512 tokens/chunk.
- Text yield: ~10% of raw doc size (PDFs/Office).
- Indexing: ~0.5-2 GB/hr depending on CPU cores.
- Chroma RAM: ~0.8 * doc_size_gb (vectors + metadata + overhead).
- Storage: raw_docs * 2 (docs + DB + outputs).
- Growth: Size SSD for 2-3 years @ monthly delta.

## Usage
1. Match your scenario to `SIZING_MODEL.csv` rows.
2. Interpolate for in-between (e.g., formulas below).
3. For custom: Use formulas to calculate.

## Formulas (rough)
```
chroma_gb = doc_size_gb * 0.8 + (monthly_gb * 24)  # 2yr buffer
ram_needed = chroma_gb * 1.5 + 8  # OS + app overhead, round to avail (16/24/32/64)
ssd_tb = (doc_size_gb * 2 + monthly_gb * 24 * 2) / 1024  # 2yr growth
index_hrs = doc_size_gb / cpu_index_rate  # e.g., M2=1GB/hr, M2Pro=2GB/hr
query_sec = 0.5 + (users / 10) * 0.2  # Base fast, scales lightly w/ load
```
CPU rates (GB/hr indexing):
- M2/M4 base: 1 GB/hr
- M2/M4 Pro: 2 GB/hr
- M2/M4 Max: 4 GB/hr

## Sample Scenarios
| Doc Size | Monthly GB | Monthly Files | Users | Config          |
|----------|------------|---------------|-------|-----------------|
| 100GB   | +5GB      | 1000         | 5     | M2 Pro/32GB/2TB |
| 10GB    | +1GB      | 200          | 2     | M2/16GB/512GB  |

See `SIZING_MODEL.csv` for full table.
