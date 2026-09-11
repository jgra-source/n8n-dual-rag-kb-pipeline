# Dual-RAG Support Agent + KB Pipeline — Rework Summary

- **Workflow ID:** `<WORKFLOW_ID>`
- **Name:** Dual-RAG Support Agent + KB Pipeline (Full Google)
- **Instance:** https://<N8N_INSTANCE>
- **Pinecone index:** `<PINECONE_INDEX>` (serverless, host `<PINECONE_HOST>`), namespace `company_docs`
- **Embeddings:** Google Vertex `text-embedding-005` (768-dim), project `<VERTEX_PROJECT>`
- **Date:** 2026-06-23 (rework); **last verified live 2026-09-11**
- **Status:** PUBLISHED / live — `activeVersionId` = `<ACTIVE_VERSION_ID>`
- **Node count:** 59 → 56 after rework + simplification + per-file collapse → **57** after the 2026-09-03 model-provider addition (§4.8)
- **Agent model (live, 2026-09-11):** Azure OpenAI `gpt-5-mini` — swapped from Gemini 2.5 Flash free tier on 2026-09-03; see §4.8
- **Companion cleanup fix:** 2026-06-24 — namespace-wipe bug found & fixed in the separate `KB Orphan Vector Cleanup` workflow (see §12); this was the actual cause of `company_docs` losing all its vectors.

---

## 1. Original problem

> "Why is it deleting its <PINECONE_INDEX> records?"

The KB ingestion pipeline was losing vectors from the `company_docs` namespace.

### Root cause
The "upsert" path was implemented as **destructive delete-then-reinsert**:

```
Drive event → Classify → Route KB Action
  upsert → Purge Stale Vectors (DELETE by drive_file_id)  ← deletes FIRST
         → Download → split → embed → re-insert
```

Every file event deleted the file's vectors **before** re-inserting. Combined with a Google Drive **`fileUpdated`** trigger (which fires on metadata-only changes) and a slow, non-atomic re-insert leg, this meant:
1. Any failure/interruption in the re-insert left the vectors permanently gone.
2. Two minute-pollers on the same folder could race (a later purge deleting a still-running insert's vectors).

---

## 2. Chosen design — hash-gated insert-then-purge-by-hash

Considered three options:
- **A:** insert-then-purge (band-aid).
- **B (pure):** deterministic-ID HTTP upsert — rejected: n8n's Extract From File has **no DOCX support** (DOCX text only comes from the langchain loader), and raw HTTP embeddings would risk embedding-space drift vs the query-time retriever.
- **B (hybrid, CHOSEN):** keep the langchain embed/insert stack (DOCX works, embeddings always match queries) + make it safe and idempotent:
  1. **Content-hash gate** — skip files whose content is unchanged.
  2. **Tag each chunk** with `content_token` (Drive `headRevisionId`) and **INSERT new vectors BEFORE deleting old ones**, then delete only the *prior* token.

### Safety properties achieved
- **No data loss** — insert happens before any delete; purge only runs after a successful insert.
- **No over-delete** — purge scoped to `drive_file_id == X AND content_token != current`.
- **No churn / no duplicates** — the gate skips unchanged files, so spurious `fileUpdated` events are free no-ops.

---

## 3. Final KB ingestion flow

```
Watch KB Folder (fileUpdated) ┐
Watch KB Folder (Created)     ┴→ Classify File Event  (adds content_token = headRevisionId,
                                                        cfg_pineconeHost, cfg_ns_docs)
   → Route KB Action
       ├─ delete  → Delete Old Vectors → Log Delete            (genuine DELETE_<name> files)
       └─ upsert  → Lookup KB State ┐
                    (Route also ───→ Merge Gate (enrichInput1: drive_file_id = file_id))
                                      → Content Changed?  ($json.stored_token != $json.content_token)
                                          ├─ false → Skip Unchanged
                                          └─ true  → Loop Over Items (batchSize 1)
                                                       └─[loop]→ Restore File Meta → Download File
                                                                  → Route by File Type
                                                                      ├ pdf  → Extract Text (PDF) → Insert KB Vectors (PDF)
                                                                      ├ docx → Insert KB Vectors (DOCX)
                                                                      └ text/gdoc → Insert KB Vectors (Text)
                                                                  → Purge Prior Version (throttled)
                                                                  → Log Upsert → Update KB State
                                                                  └──────────→ back to Loop Over Items
                                                       └─[done]→ (ends)
```

Key state stores:
- **`KBState` tab** (sheet `<SHEETS_DOC_ID>`): `file_id | stored_token | updated_at`, one row per file (upsert by `file_id`). Drives the skip gate. **Must stay in sync with Pinecone — clear it whenever you wipe the namespace.**
- **`Logs` tab**: append-only audit (`action = "upsert"` / `"delete"`).

---

## 4. Changes made (in order)

1. **Core safety rewrite** — removed `Purge Stale Vectors`; added `content_token` to `Classify File Event` and to all 3 loaders' metadata; added insert-then-purge `Purge Prior Version` nodes (filter `content_token $ne current`).
2. **Hash gate** — added `Lookup KB State`, `Content Changed?`, `Skip Unchanged`, `Update KB State`.
3. **Paired-item fix #1 (gate)** — `Lookup KB State` (Sheets read) broke paired-item linkage → "Multiple matching items" error. Fixed with `Merge Gate` (enrichInput1) so both tokens sit on one item and the IF compares `$json` only.
4. **Rate-limit throttle** — Pinecone serverless caps delete-by-metadata at **5/sec per namespace**. Set `Purge Prior Version` batching to 1 req / 350 ms.
5. **Simplification (59 → 54)** — collapsed 3× `Purge Prior Version` → 1, 3× `Log Upsert` → 1 (action `"upsert"`), removed dead `Delete Complete` Set node.
6. **Paired-item fix #2 (the big one)** — the langchain `Insert KB Vectors` node **collapses `pairedItem` to 0**, so in multi-file runs every post-insert reference (`Purge`, `Log`, `Update KB State`) AND the loader metadata resolved to the *first* file → all chunks mis-tagged with file 0's `drive_file_id`/`content_token`. **Fixed** by wrapping the per-file body in **`Loop Over Items` (batchSize 1)** and repointing every per-file reference to `$('Loop Over Items').first()` (pairing-independent, since one item per iteration). This makes ingestion serial (slower) but correct.
7. **Per-chunk fan-out fix** — the langchain `Insert` node emits **one output item per chunk**, so the tail (`Purge → Log → Update KB State`) was running once *per chunk* instead of once per file. Symptoms: dozens of duplicate `KBState`/`Logs` rows per file, and the purge firing N delete-by-metadata calls per file (the real cause of the earlier 5/sec rate-limit error at "item 39"). **Fixed** by inserting a **`One Per File`** node (`Limit`, maxItems 1) between the inserts and the tail, so each file produces exactly one purge call, one log row, one KBState upsert.
8. **Model-provider swap (2026-09-03)** — the query-side agent originally ran on `gemini-2.5-pro`/`flash` via the Google Vertex chat node (`Agent Model`). Gemini's free tier caps at **20 requests/day**, which the live support agent exceeded. Added `Agent Model (Azure)` (`lmChatAzureOpenAi`, `gpt-5-mini`) and repointed `Support Agent`'s `ai_languageModel` connection to it. **The Gemini node was not deleted** — it's disabled in place, connection removed, kept as the documented rollback path if the Azure deployment is ever unavailable. Retrieval and embeddings (Vertex `text-embedding-005`) were untouched — only the agent's own reasoning model moved; the two retriever embeddings still must match the ingestion embeddings, which they do (§9.5 invariant unaffected).

---

## 5. Backups (rollback points) — `/home/ubuntu/backups/`
- `<WORKFLOW_ID>_2026-06-20.json` — earliest
- `<WORKFLOW_ID>_2026-06-22.json` — original (pre-rework)
- `<WORKFLOW_ID>_2026-06-22_pre-simplify.json`
- `<WORKFLOW_ID>_2026-06-22_pre-loop.json`
- `<WORKFLOW_ID>_2026-06-23_pre-limit.json` — before the per-chunk fan-out fix

---

## 6. Verification result (2026-06-23) — ✅ MATCHED
- **Drive KB folder:** 11 PDFs (N = 11). All `application/pdf`.
- **Pinecone `company_docs`:** 2480 vectors (chunks) ≈ 225 chunks/file avg — plausible for large FIA regulation PDFs.
- **Latest execution 2413** (15:47→15:58Z): status **success**, ran on the loop version.
- **`KBState`:** **11 distinct `file_id`s and 11 distinct `stored_token`s** confirmed by user → all 11 files indexed and correctly tagged. ✅
- **Reconciliation:** Drive (11) = KBState distinct files (11) = distinct `drive_file_id` in Pinecone (11). Every file indexed exactly once.

### Cleanup note
`KBState` and `Logs` accumulated **many duplicate rows per file** (one per chunk) from runs *before* the per-chunk fan-out fix (change #7). These are cosmetic — the gate's `Lookup KB State` uses `returnFirstMatch`, so duplicate rows still resolve to the correct token. After change #7, future runs write exactly one row per file.

**KBState de-dupe (manual — done in the Sheet UI):** the available MCP connectors have no row-level Sheets delete, and automating it would require clearing/rewriting the live state table (too risky for a cosmetic fix). Recommended: `KBState` tab → select `file_id` column → **Data → Data cleanup → Remove duplicates** (match on `file_id`) → leaves 11 rows. **Leave `Logs` as-is** (append-only audit history).

---

## 7. Known caveats / follow-ups
- **Performance:** loop is serial (one file per iteration) — `N × (download + embed + insert)`. Cannot raise `batchSize` above 1 without reintroducing the paired-item collapse bug.
- **Data hygiene after the bug:** any **pre-loop** multi-file execution (e.g. 2398) mis-tagged all-but-the-first file. The clean fix is `deleteAll` on `company_docs` + clear `KBState`, then re-upload through the loop version. (User had already wiped Pinecone at least once.)
- **Migration & `$ne`:** Pinecone `$ne` filters only match vectors that *have* the field, so legacy untagged vectors aren't auto-purged — always start a reindex from a clean namespace + empty `KBState`.
- **Batch uploads:** safe (one execution picks up all; gate makes re-runs idempotent). For large sets, upload in waves of ~15–20 — the unthrottled limiters are **Vertex embeddings** and **Google Sheets** per-minute quotas (Pinecone purge is throttled). Optional: enable *Retry On Fail* (~3 tries, 1000 ms) on `Log Upsert`, `Update KB State`, and the insert node.
- **`gdoc` branch:** `Route by File Type` routes native Google Docs to the Text branch, but `Download File` does a raw download (no export format) — likely errors for true Google Docs. KB is currently all PDF, so the path is effectively dead; remove it or add an export step if Google Docs are needed.
- **Concurrency:** n8n has **no per-workflow** concurrency cap (only instance-wide `N8N_CONCURRENCY_PRODUCTION_LIMIT`). The loop + hash gate make races unlikely; left as-is.
- **Log label:** consolidation changed the log action from `upsert_pdf/docx/text` to plain `"upsert"`. Can be made dynamic if per-type visibility is wanted.
- **Credentials:** the n8n MCP **cannot** attach predefined-type credentials (e.g. `pineconeApi`) to HTTP Request nodes — `Purge Prior Version` and `Delete Old Vectors` Pinecone creds were attached manually in the editor.

---

## 8. Lessons learned (also saved to memory)
- The n8n MCP can't bind **any** predefined-type credential to `httpRequest` nodes (confirmed for `httpHeaderAuth` and `pineconeApi`) — must use the editor UI; publish after saving.
- The langchain **vector-store Insert node collapses `pairedItem` to 0** — never rely on `$('UpstreamNode').item` after it in multi-item runs; isolate with a `Loop Over Items` (batch 1) and reference `$('Loop Over Items').first()`.
- Google Sheets **read/lookup** nodes also break paired-item linkage — use a `Merge` (enrich) to bring looked-up values onto the working item instead of cross-node `.item` references.
- Pinecone **serverless**: delete-by-metadata is capped at **5/sec per namespace**; `$ne` filters skip vectors lacking the field.
- Always ask the rollout method before editing a workflow, and checkpoint the full JSON first.
- **Orphan/GC cleanup is dangerous:** a Pinecone delete-by-metadata built from a dynamic "keep" list will wipe the **entire namespace** if that list is ever empty — `$nin: []` (and `$in: []`) match **every** vector. Always guard `ids.length === 0` and **abort before deleting**. A wrong folder ID (querying the Sheet doc instead of the Drive folder) is exactly how the companion `KB Orphan Vector Cleanup` returned 0 files and nuked `company_docs` nightly — see §12.

---

## 9. Recreating this in another environment

This is a portable runbook. Replace every value in the table below for the target environment; everything else is structural and identical.

### 9.1 Environment-specific values to swap
| Placeholder | This env | Where it's used |
|---|---|---|
| `<PINECONE_HOST>` | `https://<PINECONE_HOST>` | `Classify File Event` (`cfg_pineconeHost`); inherited by Purge/Delete via `$('Loop Over Items').first()` |
| `<PINECONE_NAMESPACE>` | `company_docs` | `Classify File Event` (`cfg_ns_docs`); the 3 Insert nodes' `pineconeNamespace` |
| `<PINECONE_INDEX>` | `<PINECONE_INDEX>` | all vector-store nodes (`pineconeIndex`) |
| `<VERTEX_PROJECT>` | `<VERTEX_PROJECT>` | all `embeddingsGoogleVertex` nodes |
| `<EMBED_MODEL>` / dims | `text-embedding-005` / 768 | embeddings nodes; **index must be created with matching dimension** |
| `<DRIVE_FOLDER_ID>` | `<DRIVE_FOLDER_ID>` | both Drive triggers (`folderToWatch`) |
| `<SHEETS_DOC_ID>` | `<SHEETS_DOC_ID>` | `Lookup KB State`, `Update KB State`, `Log Upsert`, `Log Delete` |
| `<SLACK_CHANNEL>` | `<SLACK_CHANNEL>` | `Config` (`cfg_slackChannelId`) / agent escalation |

### 9.2 Prerequisites (create first)
1. **Pinecone index** with dimension = embedding model's (768 for `text-embedding-005`). Namespaces are created on first upsert.
2. **Credentials** in n8n: `pineconeApi`, Google `googleApi` service account (Vertex), `googleSheetsOAuth2Api`, `googleDriveOAuth2Api`, Slack (if using escalation).
3. **Google Sheet** with two tabs:
   - `Logs`: `timestamp | workflow | action | file_name | file_id | status | message`
   - `KBState`: `file_id | stored_token | updated_at`
4. **Drive folder** to watch.

### 9.3 Build order (KB ingestion half)
1. Two Drive triggers on `<DRIVE_FOLDER_ID>` — one `fileCreated`, one `fileUpdated`, poll every minute → both into `Classify File Event`.
2. `Classify File Event` (Set): set `drive_file_id`, `file_name`, `mimeType`, `content_token = {{ $json.headRevisionId }}`, `kb_action = startsWith("DELETE_") ? "delete" : "upsert"`, plus `cfg_pineconeHost`/`cfg_ns_docs`.
3. `Route KB Action` (Switch on `kb_action`): `delete` → `Delete Old Vectors` (HTTP POST `<host>/vectors/delete`, filter `{drive_file_id:{$eq}}`) → `Log Delete`.
4. `upsert` branch — the **gate**: `Route → Lookup KB State` (Sheets read, filter `file_id`) **and** `Route → Merge Gate` input 0; `Lookup → Merge Gate` input 1 (combine, `drive_file_id = file_id`, `enrichInput1`). `Merge Gate → Content Changed?` (IF `$json.stored_token != $json.content_token`).
5. `Content Changed?` false → `Skip Unchanged` (NoOp). True → **`Loop Over Items`** (Split in Batches, `batchSize 1`).
6. Loop `[loop]` body: `Restore File Meta` (Set drive_file_id/file_name/mimeType from `$json`) → `Download File` (Drive download, fileId `$json.drive_file_id`) → `Route by File Type` (Switch on mimeType): pdf → `Extract Text (PDF)` → `Insert (PDF)`; docx → `Insert (DOCX)`; text/gdoc → `Insert (Text)`.
7. Each Insert = langchain `vectorStorePinecone` (insert) + its own `embeddingsGoogleVertex` + `documentDefaultDataLoader` (+ recursive splitter, chunk 800 / overlap 150). **Loader metadata** (tags every chunk): `drive_file_id`, `source_file`, `mimeType`, `content_token` — ALL referencing `={{ $('Loop Over Items').first().json.<field> }}`.
8. All 3 Insert nodes → **`One Per File`** (`Limit`, maxItems 1) → **`Purge Prior Version`** (HTTP POST `<host>/vectors/delete`, body `{namespace, filter:{drive_file_id:{$eq}, content_token:{$ne current}}}`, all via `$('Loop Over Items').first()`; **batching 1 req / 350 ms**) → `Log Upsert` → `Update KB State` (Sheets appendOrUpdate, match `file_id`) → back to `Loop Over Items`.
9. Loop `[done]` → unconnected.

### 9.4 Non-portable manual steps (after import)
- **Attach `pineconeApi` credential** to `Delete Old Vectors` and `Purge Prior Version` in the editor — the n8n MCP/import cannot bind predefined-type creds to HTTP nodes.
- Re-select Google/Sheets/Drive/Slack credentials on their nodes (credential IDs differ per instance).
- Confirm the Vertex embedding **region** (default `us-central1`) matches your index/model.
- Publish.

### 9.5 Invariants that must hold (or it breaks)
- `content_token` source (`headRevisionId`) must be identical between the gate compare, the loader metadata, and the purge filter — otherwise files never match and re-index forever or duplicate.
- `KBState` must stay in sync with the vector store: **whenever you wipe the namespace, clear `KBState`** (else the gate skips files that no longer have vectors).
- `batchSize` on `Loop Over Items` must stay **1** (higher reintroduces the pairedItem-collapse mis-tagging).
- Index embedding **dimension must equal** the model's, and the same model must serve both ingestion and the query-time retriever (embedding-space consistency).

---

## 10. Troubleshooting — errors seen & how to diagnose/fix

General diagnosis: open the failed **execution**, click the red/failed node, read the error + its **input** items. For data-correctness issues (mis-tagging, duplicates), inspect the `metadata` of items output by an `Insert` node and the rows written by `Log Upsert` / `Update KB State`.

| Symptom / error message | Root cause | Diagnose | Fix |
|---|---|---|---|
| **"Multiple matching items for item [0]"** (on `Content Changed?` or any node) | A cross-node `$('X').item` reference can't resolve because an upstream node (Sheets read, or langchain Insert) broke `pairedItem`. | Check which node throws; look at whether an upstream Sheets-read or vector Insert sits between it and the referenced node. | Don't use `$('Classify…').item` across those nodes. Use `Merge` (enrich) for the gate, and `$('Loop Over Items').first()` inside the per-file loop. |
| **"reached the delete by metadata requests per second limit … (5/sec)"** | Too many Pinecone delete-by-metadata calls/sec. Was caused by the purge firing **once per chunk** (per-chunk fan-out). | Count how many times `Purge Prior Version` ran in the execution (should be 1 per file). | Ensure `One Per File` (Limit=1) sits before the purge; keep purge node batching at 1 req / 350 ms. For bulk reindex, `deleteAll` once instead of per-file purges. |
| **Dozens of duplicate rows in `KBState` / `Logs`** (same `file_id`, ms-apart timestamps) | Tail nodes ran once per chunk (langchain Insert emits one item per chunk). | KBState has many rows per file but only N distinct `file_id`s. | `One Per File` (Limit=1) after the inserts (change #7). De-dupe existing rows manually (Sheet → Remove duplicates on `file_id`). |
| **All of a file's chunks tagged with the WRONG `drive_file_id`** (multi-file run) | langchain Insert collapses `pairedItem` to 0 → loader metadata + downstream resolve to file 0. | Inspect `Insert KB Vectors` output items → `metadata.drive_file_id`; if 2 different files show the same id, this is it. | Loop Over Items (batchSize 1) + reference `$('Loop Over Items').first()` everywhere per-file. |
| **Files silently skipped / KB ends up empty** | `KBState` out of sync with Pinecone (state says "indexed" but vectors were wiped). | `Content Changed?` goes false though Pinecone has no vectors for that file. | Whenever you `deleteAll` the namespace, also clear `KBState`. Keep them in sync. |
| **Vectors deleted but never come back** (the original bug) | Destructive delete-then-reinsert; re-insert failed or raced. | Old design only — confirm `Purge Stale Vectors` is gone and purge runs only AFTER insert. | Insert-then-purge-by-hash (already in place). |
| **Entire `company_docs` namespace wiped (recurring / nightly)** | The **separate** `KB Orphan Vector Cleanup` workflow built its delete filter from an **empty** Drive listing → `$nin:[]` matches every vector. The listing was querying the **Sheet doc ID instead of the Drive folder ID**, so it returned 0 files. | In that workflow's executions: `List Drive Folder Files` → `files:[]`, `Build Orphan Filter` → `"$nin":[]`, `Delete Orphan Vectors` → `success`. The run shows `error` only from a *later* log node, masking the delete. | Point the listing at the real `<DRIVE_FOLDER_ID>`; add an `ids.length === 0` abort guard. See §12. |
| **HTTP node: credential can't be set / "does not accept credential 'pineconeApi'"** | n8n MCP/import can't bind predefined-type creds to `httpRequest`. | Node shows no credential after import/programmatic edit. | Attach the Pinecone credential manually in the editor on `Delete Old Vectors` and `Purge Prior Version`; then publish. |
| **`Lookup KB State` errors** (sheet/tab not found) | `KBState` tab doesn't exist yet. | Read node error references the sheet/range. | Create the `KBState` tab (`file_id\|stored_token\|updated_at`). |
| **Google Docs (native) file fails in pipeline** | `Download File` does a raw download; native Google Docs require an export format. | Error on `Download File` for a `application/vnd.google-apps.document`. | Add an export step for the `gdoc` route, or remove that route if KB is only PDF/DOCX/TXT. |
| **Vertex / Google Sheets 429s during large batch uploads** | Embedding + Sheets per-minute quotas (not throttled like the purge). | 429 mid-batch on the embeddings or Sheets nodes. | Upload in waves of ~15–20; enable *Retry On Fail* (~3 tries, 1000 ms) on `Log Upsert`, `Update KB State`, and the insert nodes. Gate makes re-runs idempotent. |
| **Validation warning: `Alert Staff on Slack` missing `resource`** | Pre-existing Slack node config; unrelated to this rework. | Appears in every `update_workflow` response. | Cosmetic — fix the Slack node's `resource`/`operation` if/when you touch the escalation path. |
| **Stale/expired Google OAuth token** | OAuth consent screen in *Testing* mode (tokens expire after 7 days). | Google nodes throw invalid/expired token. | Publish the Google Cloud OAuth consent screen to **Production**. |

### Quick health check after a run
1. Execution status = `success` (check for `error` runs — an error inside the loop stops that whole run, so later files won't process).
2. Reconcile: **Drive file count = distinct `KBState.file_id` = distinct `drive_file_id` in Pinecone.**
3. Spot-check one file: Pinecone query `filter:{drive_file_id:{$eq:"<id>"}}` returns chunks whose `metadata.source_file` matches that file's name.

---

## 11. Support Agent (webhook / query) half — build steps

The query side is independent of the KB ingestion side (shares only the Pinecone index + embeddings). It answers customer questions over the two namespaces, escalates low-confidence cases to Slack, and self-learns confident Q&A pairs into `self_learned`.

### 11.1 Extra environment values (beyond §9.1)
| Placeholder | This env | Used in |
|---|---|---|
| `<WEBHOOK_PATH>` | `support-agent` | `Customer Query Webhook` (POST) |
| `<AGENT_MODEL>` | live: `gpt-5-mini` via Azure OpenAI (`lmChatAzureOpenAi`); rollback: `gemini-2.5-flash` (temp 0.2) via Google Vertex, kept disabled — see §4.8 | `Agent Model (Azure)` (wired) / `Agent Model` (disabled) |
| `<SELF_LEARNED_NS>` | `self_learned` | `Self-Learned Answers KB`, `Write to Self-Learned KB` |
| `<CONFIDENCE_THRESHOLD>` | `0.78` | `Config` (`cfg_confidenceThreshold`), `Confident Enough to Learn?` |
| `<MODE>` | `live` | `Config` (`cfg_mode`) — gates whether Slack alerts actually send |

### 11.2 Flow
```
Customer Query Webhook (POST /<WEBHOOK_PATH>, responseMode = responseNode)
  → Config (Set: cfg_mode, cfg_pineconeHost, cfg_ns_docs, cfg_confidenceThreshold, cfg_slackChannelId)
  → Normalize Query (Set: message/customer_id/session_id from $json.body?.x ?? $json.x ?? default)
  → Message Empty?  (IF $json.message is empty)
       ├─ true  → Reject Empty Message (respondToWebhook 400)
       └─ false → Support Agent
                    ├─ ai_languageModel: Agent Model (Azure) (lmChatAzureOpenAi, gpt-5-mini) — swapped in from Agent Model (lmChatGoogleVertex, gemini-2.5-flash), disabled, §4.8
                    ├─ ai_tool:          Company Docs KB (vectorStorePinecone retrieve-as-tool, ns company_docs, topK 6)
                    │                      └ ai_embedding: Embeddings Docs Retriever (embeddingsGoogleVertex)
                    ├─ ai_tool:          Self-Learned Answers KB (retrieve-as-tool, ns self_learned)
                    │                      └ ai_embedding: Embeddings Learned Retriever
                    └─ ai_outputParser:  Agent Output Schema (structured: {answer, confidence, needs_escalation, escalation_reason})
  → Needs Escalation?  (IF $json.output.needs_escalation == true)
       ├─ true  → Build Escalation Alert (Set slack_text + answer)
       │           → Live Mode (Escalation)?  (IF $('Config').first().json.cfg_mode == "live")
       │                ├─ true  → Alert Staff on Slack (channel cfg_slackChannelId) → Respond (Escalated)
       │                └─ false → Respond (Escalated)        (200, {success, escalated:true, answer})
       └─ false → Confident Enough to Learn?  (IF $json.output.confidence >= <CONFIDENCE_THRESHOLD>)
                    ├─ true  → Build Q&A Document (Set text="Q: …\nA: …", confidence, learned_at)
                    │           → Write to Self-Learned KB (vectorStorePinecone insert, ns self_learned)
                    │                ├ ai_embedding: Embeddings Learn Insert
                    │                └ ai_document:  Load Q&A Document (jsonData $json.text; metadata source, confidence)
                    │           → Respond (Answer)
                    └─ false → Respond (Answer)               (200, {success, escalated:false, answer, confidence})
```

### 11.3 Key node configs
- **Support Agent** (`@n8n/n8n-nodes-langchain.agent`): `promptType=define`, `text={{ $json.message }}`, `hasOutputParser=true`. System message enforces: always search `company_docs` first, fall back to `learned_answers`, never answer from general knowledge, return a confidence score, set `needs_escalation` when unsure.
- **Agent Output Schema** (structured output parser): example `{ "answer": "...", "confidence": 0.92, "needs_escalation": false, "escalation_reason": "" }`.
- **Retriever tools**: two `vectorStorePinecone` in `retrieve-as-tool` mode on the same index, namespaces `company_docs` (topK 6) and `self_learned`; each with its own `embeddingsGoogleVertex` (same model/dims as ingestion — required for embedding-space match).
- **Self-learn write**: `Write to Self-Learned KB` (insert, ns `self_learned`) + `Load Q&A Document` (jsonData `$json.text`, metadata `source=self_learned`, `confidence`). Note: this side does **not** use deterministic IDs/purge — it just appends confident Q&A pairs.
- **Live-mode gate**: `Live Mode (Escalation)?` checks `cfg_mode == "live"` so you can run in a dry/test mode that skips actually posting to Slack.

### 11.4 Manual steps / gotchas (query side)
- Attach credentials: Slack (`Alert Staff on Slack`), Vertex `googleApi` (agent model + both retriever embeddings).
- **`Alert Staff on Slack` currently has the pre-existing validation warning** (missing `resource`/`operation` discriminator) — set `resource=message`, `operation=post` when configuring.
- The agent answers strictly from retrieval; if `company_docs` is empty (e.g. mid-reindex), expect low-confidence/escalation responses — not hallucinated answers (by design).
- Self-learned writes are ungated by content hash; over time `self_learned` can accumulate near-duplicate Q&A. Prune periodically if it grows noisy.

---

## 12. Companion workflow — KB Orphan Vector Cleanup (namespace-wipe bug, fixed 2026-06-24)

A **separate** scheduled workflow, **`KB Orphan Vector Cleanup`** (`<CLEANUP_WORKFLOW_ID>`), runs nightly at 2 AM to garbage-collect vectors for files removed from Drive *without* a `DELETE_` rename. It is independent of the main pipeline and shares only the Pinecone index/namespace. **This — not the main pipeline — was the real cause of `company_docs` losing its vectors.** The main pipeline's insert-then-purge logic was verified correct and never over-deleted.

### The bug
The cleanup deletes every vector whose `drive_file_id` is **not** in the current Drive folder listing. Two faults combined to wipe the whole namespace every night:

1. **Wrong folder ID.** `List Drive Folder Files` queried `'<SHEETS_DOC_ID>' in parents` — the **Google Sheet** doc ID, not the **Drive KB folder** (`<DRIVE_FOLDER_ID>`). A spreadsheet has no child files, so the listing returned **0 files every run**.
2. **Unguarded `$nin: []`.** With an empty ID list, `Build Orphan Filter` produced `{ drive_file_id: { "$nin": [] } }`. In Pinecone an empty `$nin` matches **every** vector, so `Delete Orphan Vectors` purged all of `company_docs`.

**Why it stayed hidden:** the delete node has `onError: continueRegularOutput`, and each run only showed status `error` because the *downstream* `Log Cleanup to Sheets` node failed on a `YOUR_SPREADSHEET_ID` placeholder — **after** the delete had already succeeded. Confirmed from execution data: `files:[]` → `"$nin":[]` → delete `success`. It had been wiping the namespace nightly since 2026-06-20.

### The fix (3 changes; backup `<CLEANUP_WORKFLOW_ID>_2026-06-24.json`)
1. **Correct folder ID** — query now targets `'<DRIVE_FOLDER_ID>' in parents and trashed=false`.
2. **Empty-list guard (the real safeguard)** — `Build Orphan Filter` now `throw`s and aborts if `ids.length === 0`, so a wrong folder / API hiccup / empty folder can **never** wipe the namespace again. No delete is ever sent on an empty keep-list.
3. **Log fix** — `Log Cleanup to Sheets` document ID set to the real `<SHEETS_DOC_ID>`.

### Status / rollout
- Fix is **saved to the draft but NOT yet published**; the workflow is **deactivated** as a stopgap so it cannot fire again until the KB is restored.
- **Recovery before re-publishing:** (1) confirm `company_docs` is empty in Pinecone (or `deleteAll`); (2) clear `KBState` data rows; (3) re-upload the current KB files to Drive so ingestion repopulates; (4) reconcile **Drive count = distinct `KBState.file_id` = distinct Pinecone `drive_file_id`**; (5) **then publish** the cleanup to re-arm the nightly sweep.

### Notes
- The `DELETE_` path (`Delete Old Vectors`) and the nightly sweep are **complementary**, not redundant: `DELETE_` is immediate; the sweep is the catch-all for files deleted straight from Drive (≤24h lag). Both retained.
- The KB folder contents changed during debugging (the FIA F1 regulation PDFs were swapped for other PDFs); "what belongs in `company_docs`" is whatever is currently in `<DRIVE_FOLDER_ID>`.
- The `Logs` tab is heavily bloated with per-chunk duplicate rows from pre-fix runs — cosmetic; de-dupe (`Remove duplicates`) is optional. Leave `Logs` append-only otherwise.
