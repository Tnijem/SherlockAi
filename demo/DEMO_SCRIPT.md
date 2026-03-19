# Sherlock AI — Demo Script

> **Runtime**: ~25 minutes for full demo, ~10 minutes for highlights
> **Pre-requisite**: Run `./demo/demo-loader.sh load` before the demo

---

## Demo Cases

| Case | Type | Key Features Showcased |
|------|------|----------------------|
| **Martinez v. Coastal Healthcare** | Medical malpractice | Timeline, risk analysis, deadlines, expert reports, billing data |
| **TechVault v. NexGen** | Trade secret theft | Forensic evidence, contract analysis, compare & contrast, TRO |
| **Greenfield / Riverside** | Real estate + environmental | Technical data (lab results), demand letters, negotiation strategy |

---

## Act 1: First Impressions (3 min)

### Login & Interface Tour

1. Open Sherlock at `https://<host>`
2. Login as demo user (admin / [password])
3. Point out: dark professional theme, gold accents, sidebar with matter history

**Talking point**: *"Sherlock runs entirely on your firm's hardware — no cloud, no data leaves the building. Every document, every query, every AI response stays on-premise."*

---

## Act 2: Case Setup & Document Ingestion (3 min)

### Create a Case

1. Navigate to **Cases** view
2. Click **+ New Case**
3. Fill in:
   - Case Number: `26-CV-01847-RLR`
   - Case Name: `Martinez v. Coastal Healthcare Systems`
   - Case Type: Medical Malpractice
   - Client: Maria Elena Martinez
   - Opposing: Coastal Healthcare Systems, Inc.
   - Jurisdiction: S.D. Florida
4. Set NAS Path to the demo case folder
5. Click **Create** — watch indexing start automatically

**Talking point**: *"Sherlock automatically indexes every document in the case folder — PDFs, Word docs, spreadsheets, even scanned images via OCR. It chunks, embeds, and stores them for instant semantic search."*

---

## Act 3: Conversational Research (8 min)

### Create a Matter and Start Querying

1. Navigate to **Chat** view
2. Click **+ New Matter**, name it "Martinez Case Review"
3. Link it to the Martinez case

### Query 1: Summary (Query Type: Summary)

Set **Query Type** → Summary, then ask:

> **"What are the key facts of the Martinez case?"**

**Expected**: Sherlock summarizes the complaint — parties, surgery, bile duct injury, complications, damages claimed. Sources cited with relevance scores.

**Point out**: Source citations, relevance scores, token stats bar.

### Query 2: Timeline (Query Type: Timeline)

Switch **Query Type** → Timeline:

> **"Build a complete timeline of events from Mrs. Martinez's first symptoms through the filing of this lawsuit."**

**Expected**: Chronological timeline from 12/15/2025 PCP visit through 2/10/2026 complaint filing, hitting every key date from the medical records.

### Query 3: Risk Analysis (Query Type: Risk)

Switch **Query Type** → Risk:

> **"What are the biggest risks and weaknesses in the plaintiff's case?"**

**Expected**: Analysis of affirmative defenses — failure to mitigate (4-day delay returning to ER), pre-existing conditions, known complication defense, peer review privilege, independent contractor defense.

### Query 4: Cross-Document Analysis

> **"Compare Dr. Thornton's operative note with Dr. Alvarez's findings during the reconstruction. What discrepancies exist?"**

**Expected**: Sherlock pulls from both documents, identifying that Thornton claimed "cystic duct identified and clipped" while Alvarez found clips on the CBD with cystic duct unclipped.

### Query 5: Expert Witness

> **"Summarize Dr. Williams' expert opinions and identify which ones are strongest for trial."**

**Expected**: All 5 opinions listed, with analysis of evidentiary support for each.

### Query 6: Verbosity Roles

Switch **Verbosity** → Client:

> **"Explain the case to Mrs. Martinez in simple terms. What happened and what are we asking for?"**

**Expected**: Plain English explanation without medical/legal jargon. Compare with Attorney verbosity on same question.

---

## Act 4: Trade Secrets Case — Advanced Features (5 min)

### Switch to TechVault Case

1. Create new case: `26-CV-02391-EJD — TechVault v. NexGen`
2. Create matter: "TRO Strategy"

### Query 7: Contract Analysis

> **"What obligations did Kevin Zhang have under his employment agreement, and which ones did he breach based on the forensic report?"**

**Expected**: Maps specific agreement clauses (§3.3(c), §3.3(d), §5.1) to specific forensic findings (USB transfer, email forwarding, false exit certification).

### Query 8: Drafting Mode (Query Type: Drafting)

Switch **Query Type** → Drafting:

> **"Draft a declaration in support of the TRO motion, from TechVault's CTO, highlighting the most damning forensic evidence."**

**Expected**: Structured legal declaration with numbered paragraphs, penalty of perjury language, pulling specific facts from the forensic report.

### Query 9: Deadline Extraction

Click the **Deadlines** tab in the matter:

> Extract deadlines

**Expected**: Structured deadline list — discovery cutoffs, deposition deadlines, motion dates. (If scheduling order exists.)

---

## Act 5: Environmental Case — Data-Heavy Analysis (3 min)

### Switch to Greenfield Case

Create matter: "Environmental Negotiation"

### Query 10: Technical Data Interpretation

> **"Which contaminants exceed regulatory limits and by how much? Summarize the remediation options and costs."**

**Expected**: Table-style breakdown of PCE (15.6x MCL), TCE (4.6x), TPH (10x RSL), Lead (2.2x RSL), Hex Chromium (145x RSL). Three options with cost ranges.

### Query 11: Legal Strategy from Technical Data

> **"Based on the Phase II report and the purchase agreement, what are Riverside's strongest legal arguments against Greenfield?"**

**Expected**: Breach of §2.3(a) and §2.3(d) representations, knowledge argument from Seller's own Phase I, indemnification under §7.1(b), potential fraud claim.

### Query 12: Compare Negotiation Positions

> **"Compare the demand letter and seller's response. Where do the parties agree and disagree? What's a reasonable settlement range?"**

**Expected**: Side-by-side analysis — price reduction ($2.8M vs $900K), indemnification cap (uncapped vs $1.5M), survival period (10yr vs 5yr).

---

## Act 6: Internet Research Mode (2 min)

### Toggle Research Mode ON

> **"What is the current standard of care for laparoscopic cholecystectomy? What does the SAGES Safe Cholecystectomy program recommend?"**

**Point out**: Results now include web sources alongside document sources. Sherlock attributes which information came from local documents vs. web search.

**Talking point**: *"Research mode lets attorneys verify against current law and medical literature — with full source attribution so you always know where the information came from."*

---

## Act 7: File Upload & Drag-Drop (2 min)

### Demonstrate Drag-and-Drop

1. Open a Finder window with demo files
2. Drag 2-3 files into the chat window
3. Show the upload progress and indexing
4. Show the prompt: "What would you like me to do with these files?"
5. Click **Compare** pill
6. Watch Sherlock analyze the specific uploaded files

**Talking point**: *"Drop files directly into a conversation — Sherlock indexes them instantly and you can ask anything about them."*

---

## Act 8: Output & Export (2 min)

### Save and Export

1. On any AI response, click **Save** → shows saved confirmation + download
2. Click **Export DOCX** → downloads a formatted Word document
3. Show that the saved file appears in the **Outputs** view
4. Mention NAS mirroring: *"Every saved output automatically mirrors to your firm's shared drive."*

---

## Act 9: Admin & Monitoring (2 min)

### Admin Panel Tour

1. Switch to **Admin** view
2. Show **Usage Dashboard**: per-user token counts, query volume, system load
3. Show **Log Viewer**: real-time logs with level filtering and search
4. Show **User Management**: add/disable users, reset passwords
5. Open **Telemetry Dashboard** in new tab: node health, service status, CPU/RAM gauges

**Talking point**: *"Full visibility into system health, usage patterns, and costs — everything a managing partner needs."*

---

## Closing (1 min)

**Key messages**:
- "Every query, every document, every AI response — 100% on your hardware"
- "No per-query API costs — flat infrastructure cost, unlimited usage"
- "Supports the formats your firm already uses — PDFs, Word, Excel, scanned documents, even audio recordings"
- "Role-based access, audit logging, and compliance-grade data isolation"

---

## Appendix: Example Queries by Feature

### Summary Queries
- "Summarize the complaint in Martinez v. Coastal Healthcare"
- "What is the TechVault case about?"
- "Give me a brief overview of the Greenfield property transaction"

### Timeline Queries
- "Create a timeline of Zhang's data exfiltration activities"
- "Timeline of Mrs. Martinez's medical treatment from admission through discharge"
- "What are the key dates in the Greenfield negotiation?"

### Risk Analysis Queries
- "What are the weaknesses in TechVault's TRO motion?"
- "Identify all risks to Riverside if they proceed with the Greenfield purchase"
- "What defenses will Coastal Healthcare raise and how strong are they?"

### Cross-Document Queries
- "How does the forensic report contradict Zhang's exit interview certification?"
- "Compare the Phase II findings with Greenfield's environmental representations in the purchase agreement"
- "Does the expert report adequately address all the affirmative defenses?"

### Drafting Queries
- "Draft interrogatories focused on Dr. Thornton's prior bile duct injuries"
- "Draft a settlement demand letter for the Greenfield case incorporating the Phase II findings"
- "Draft a motion to compel production of peer review records, arguing the crime-fraud exception"

### Deadline Queries
- "What are all the deadlines in the Martinez scheduling order?"
- "When does the due diligence period expire for the Greenfield transaction?"
- "List all contractual deadlines from the TechVault employment agreement"

### Client-Facing Queries (Verbosity: Client)
- "Explain to Mrs. Martinez what a hepaticojejunostomy is and why she needs lifelong monitoring"
- "Explain to David Chen what the environmental contamination means for his development project"
- "What does a temporary restraining order mean in plain English?"
