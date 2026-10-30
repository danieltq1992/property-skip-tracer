# Browser-assisted PropTrace research

This is a local evidence-ingestion interface for a user-directed browser assistant, including Antigravity. PropTrace itself does not embed or launch that assistant. API keys are not required for this local handoff. Each external site must still permit the access being used; stop at access controls, browser safety blocks, login requirements or restrictions.

1. Start the development app on http://127.0.0.1:8765.
2. Obtain the exact subject street/city/state/ZIP from the user or the app. Never use a tax mailing address as the subject. No location defaults exist.
3. POST the subject object to `/api/research/context`. The response contains current evidence and the current `ReviewedRecord` schema. This context read makes no external source requests.
4. Inspect original county/deed records and document address/APN matching. Research entity relationships next. An officer, incorporation attorney or registered agent is not presumed to be the beneficial owner. Research the identified owner nationwide before considering a secondary contact.
5. Submit each observed record to `/api/research/evidence` with `subject` and `record` objects. Required record fields include `kind`, `source`, `source_url`, `observed_at` (ISO date/time), `excerpt`, `attested` and `review_method`. Set `review_method` to `browser_assisted`. Attestation means the assistant actually inspected the original record; never attest to a snippet or generated claim as if it were that record.
6. Every phone record requires its specific person's name. Do not confirm a primary phone on the user's behalf. Browser-assisted submissions with `phone_association_confirmed=true` are rejected. A human may later record their own reviewed association with a corroborated identity address.
7. A research-exhausted record must name the specific legal owner in `entity`, list attempted searches and their outcomes in `excerpt`, and carry the source URL/date. A different owner's research history cannot unlock secondary contacts. Repair authority requires inspected official/direct evidence; secondary promotion requires human review.
8. Use `/api/research/save` with the subject only when the user has requested saving/updating the lead. This updates the latest derived research fields and preserves existing handwritten notes. Retractions use `/api/research/evidence/{record_id}/retract` with the subject body; do not retract user evidence without direction.

Interactive API schemas: http://127.0.0.1:8765/docs. Endpoint payloads contain private research records and must stay local unless the user explicitly authorizes sharing them. Do not send API secrets or unrelated account information to the evidence API. The code does not claim that an external site was automatically searched unless a real retrieval record exists.

The app maintains an append-only evidence ledger, retraction state and dossier snapshots in its private SQLite store. This ledger is independent of the JSON saved-lead list. Human and browser-assisted records are explicitly distinguished. County-source evidence, licensed data and manually reviewed records remain separate source classes.
