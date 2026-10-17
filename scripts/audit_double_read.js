// Workflow script for the audit reading (analysis plan, "Audit of Group B on the corpus"):
// two independent Claude readers label the 11 Group B fields of every audit document from
// the PDF, blind to every system output; a third reader adjudicates each disagreement.
// It is the test-split reader protocol (data/gold/test_reads) applied to the audit sheet.
//
// Run from Claude Code's Workflow tool after `make draw-audit`, `make audit-sheet` and
// `python scripts/merge_reads.py readers`, with args:
//   { root: "<repo path>", docs: [{doc_id, bank, pages}, ...] }   (docs from config/strata.yaml splits.audit)
// Readers write data/gold/audit_reads/{doc_id}.{a,b}.csv; the adjudicator writes
// {doc_id}.adj.csv. Then: `python scripts/merge_reads.py merge`, `make reliability`, `make audit`.

export const meta = {
  name: 'audit-double-read',
  description: 'Two independent Claude readers label the Group B fields of each audit document from the PDF, a third adjudicates disagreements',
  phases: [
    { title: 'Read', detail: 'readers A and B per document, blind to every system output' },
    { title: 'Adjudicate', detail: 'third reader resolves disagreements and writes {doc_id}.adj.csv' },
  ],
}

const ROOT = args.root
const DOCS = args.docs
const READS = 'data/gold/audit_reads'
const SHEET = 'data/gold/audit_sheet_readers.csv'

const ROW_SCHEMA = {
  type: 'object',
  properties: {
    doc_id: { type: 'string' },
    rows: { type: 'array', items: { type: 'object', properties: {
      field: { type: 'string' },
      value: { type: 'string', description: 'the value as a string; sets joined with "|"; dates ISO; numbers without $ or %; empty when null' },
      null_reason: { type: 'string', enum: ['', 'not_applicable', 'absent_from_document'] },
      evidence_page: { type: 'integer' },
      quote: { type: 'string', description: 'verbatim text from the page that supports the value, under 25 words' },
      notes: { type: 'string' },
    }, required: ['field', 'value', 'null_reason', 'evidence_page', 'quote', 'notes'] } },
    reading_minutes: { type: 'number' },
    problems: { type: 'array', items: { type: 'string' } },
  },
  required: ['doc_id', 'rows', 'reading_minutes', 'problems'],
}

const NO_PEEK = `do not use the parsed text files, do not run the project's code, and do NOT open anything under results/, data/traces/, tests/fixtures/expected_samples.yaml, docs/corpus_pass_2.md, config/s5_routing.yaml, any file named risk_*.csv or predictions_*.csv, or data/gold/audit_sheet.csv (the readers' copy is ${SHEET}): your reading must be independent of every system output and of the other reader.`

const readerPrompt = (doc, tag) => `You are a careful human-style reader labeling one CRA Performance Evaluation PDF for the project at ${ROOT}. Document: ${doc.doc_id} (${doc.bank}, ${doc.pages} pages), file ${ROOT}/data/pdfs/${doc.doc_id}.pdf.

Read the PDF itself with the Read tool, in page ranges of at most 20 pages (pages "1-20", "21-40", ...), and read every page that matters for the fields; ${NO_PEEK}

Codebook: read ${ROOT}/docs/labeling_guide.md fully (sections 1 to 6: vocabulary, procedure, where things are, applicability table, field codebook) and the field list in ${ROOT}/config/fields.yaml. The rows you must fill are the lines of ${ROOT}/${SHEET} whose first column is ${doc.doc_id} (get them with grep): the 11 Group B fields, total_assets_usd, total_assets_asof, lending_data_period_start, lending_data_period_end, n_assessment_areas, n_full_scope, loan_products_evaluated, primary_product, inside_aa_share_number_pct, inside_aa_share_product, avg_ltd_ratio_pct. The sheet's mode column is a layout label and means nothing for your reading; nothing is prefilled.

Rules: one row per field, all 11; exactly one of value or null_reason; n_full_scope is not_applicable exactly when n_assessment_areas is 1; inside_aa_share_product is not_applicable exactly when the inside share is null; absent_from_document only after searching the sections the guide lists; evidence_page is the 1-based PDF page; quote verbatim (under 25 words) from that page; loan_products_evaluated as sorted elements joined with "|"; dates ISO; total_assets_usd as integer dollars (for FDIC prefer the loan-to-deposit table figure in thousands over a rounded sentence, as the guide says); percentages as numbers; lending_data_period_end on or before the evaluation date. Where the guide gives a rule, follow it even if you would decide otherwise, and record doubts in notes. Record your reading time in minutes. Also write the same rows as a CSV file ${ROOT}/${READS}/${doc.doc_id}.${tag}.csv with header doc_id,field,value,null_reason,evidence_page,quote,notes,reading_minutes (quote every text cell; put your reading minutes in every row), and return the structured result.`

phase('Read')
const reads = await pipeline(DOCS,
  d => parallel([
    () => agent(readerPrompt(d, 'a'), { label: `read-a:${d.doc_id}`, phase: 'Read', schema: ROW_SCHEMA, effort: 'high', model: 'opus' }),
    () => agent(readerPrompt(d, 'b'), { label: `read-b:${d.doc_id}`, phase: 'Read', schema: ROW_SCHEMA, effort: 'high', model: 'opus' }),
  ]).then(([a, b]) => ({ doc: d, a, b })),
  async ({ doc, a, b }) => {
    if (!a || !b) return { doc_id: doc.doc_id, status: 'reader_failed', a: !!a, b: !!b }
    const norm = s => String(s ?? '').trim().toLowerCase().replace(/\s+/g, ' ')
    const byField = rows => Object.fromEntries((rows || []).map(r => [r.field, r]))
    const A = byField(a.rows), B = byField(b.rows)
    const fields = Array.from(new Set([...Object.keys(A), ...Object.keys(B)])).sort()
    const same = f => A[f] && B[f] && norm(A[f].value) === norm(B[f].value) && norm(A[f].null_reason) === norm(B[f].null_reason)
    const agreed = fields.filter(same), disputed = fields.filter(f => !same(f))
    let adjudicated = null
    if (disputed.length) {
      adjudicated = await agent(`You adjudicate disagreements between two independent readers of the CRA Performance Evaluation ${doc.doc_id} (${doc.bank}, ${doc.pages} pages), file ${ROOT}/data/pdfs/${doc.doc_id}.pdf, for the project at ${ROOT}. Read ${ROOT}/docs/labeling_guide.md (sections 1 to 6) first. For each disputed field below, read the pages both readers cite (and any other page needed) with the Read tool on the PDF itself (page ranges of at most 20), decide the correct row under the codebook, and return it with a one-sentence reason naming the page in notes. ${NO_PEEK} Return rows only for the disputed fields; the value and null_reason conventions are the readers' (sets joined with "|", ISO dates, integer dollars). Also write those rows as a CSV file ${ROOT}/${READS}/${doc.doc_id}.adj.csv with header doc_id,field,value,null_reason,evidence_page,quote,notes (quote every text cell).

DISPUTED FIELDS:
${JSON.stringify(disputed.map(f => ({ field: f, reader_a: A[f] || null, reader_b: B[f] || null })), null, 2)}`, { label: `adjudicate:${doc.doc_id}`, phase: 'Adjudicate', schema: ROW_SCHEMA, effort: 'high', model: 'opus' })
    }
    return { doc_id: doc.doc_id, status: 'ok', n_fields: fields.length, n_agreed: agreed.length, disputed, minutes_a: a.reading_minutes, minutes_b: b.reading_minutes, problems: [...(a.problems || []), ...(b.problems || [])] }
  }
)
const ok = reads.filter(r => r && r.status === 'ok')
log(`${ok.length}/${DOCS.length} documents read; agreement ${ok.reduce((s, r) => s + r.n_agreed, 0)}/${ok.reduce((s, r) => s + r.n_fields, 0)} fields`)
return reads
