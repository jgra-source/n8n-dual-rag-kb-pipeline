# n8n Dual-RAG Support Agent + KB Pipeline

Design notes, build runbook, and troubleshooting guide for an n8n workflow that:

- **Ingests** a Google Drive folder of documents into a Pinecone vector store (Google Vertex embeddings), using a **hash-gated, insert-then-purge-by-hash** pipeline that is idempotent and never deletes a file's vectors before its replacements are safely inserted.
- **Answers** customer queries via a LangChain agent over two Pinecone namespaces (authoritative company docs + self-learned Q&A), with Slack escalation and confidence-gated self-learning.

> All environment-specific identifiers (instance URL, Pinecone host, GCP project, Drive/Sheet IDs, Slack channel) have been **redacted to `<PLACEHOLDERS>`**. See the swap table in the doc to adapt it to another environment.

## Contents
- [`dual-rag-kb-pipeline.md`](dual-rag-kb-pipeline.md) — full write-up:
  1. Original problem & root cause
  2. Chosen design (hash-gated insert-then-purge)
  3. Final ingestion flow
  4. Change log
  5. Backups / rollback points
  6. Verification
  7. Caveats & follow-ups
  8. Lessons learned (n8n paired-item & langchain gotchas)
  9. **Recreate-in-another-environment runbook**
  10. **Troubleshooting** (symptom → cause → diagnose → fix)
  11. **Support Agent (query side) build steps**

## Key takeaways (n8n gotchas)
- The langchain vector-store **Insert node collapses `pairedItem` to 0** and emits **one item per chunk** — isolate per-file work in a `Loop Over Items` (batch 1) + a `Limit` node, and reference `$('Loop Over Items').first()`.
- Google Sheets **read/lookup** breaks paired-item linkage — use a `Merge` (enrich) instead of cross-node `.item`.
- Pinecone **serverless** caps delete-by-metadata at **5/sec per namespace**; `$ne` filters skip vectors missing the field.
