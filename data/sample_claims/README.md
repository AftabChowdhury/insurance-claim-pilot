# Sample claims for the demo

Submit these files with the form values below via `POST /claims` in the API docs (http://localhost:8000/docs;
`loss_date` = any past date), then follow the run in the Admin panel → **Claims review** tab (agent timeline,
letter, adjuster review). The customer **Submit Claim** page only stores claims as `RECEIVED` until the claim check
is implemented. `loss_type` values for the API: burst pipe = `water_sudden`, flood / rain = `flood`,
storm / roof = `storm`, vehicle theft = `theft` (all values: `GET /loss-types`).

| Scenario | Policy | Loss type | Amount | Files | Expected |
|---|---|---|---|---|---|
| Clean claim | HO-55821 | Water damage – burst pipe | 3200 | `burst_pipe_invoice.txt`, `burst_pipe_photo_notes.txt` | Approved automatically (Clause 4.2, $500 deductible) |
| Retry | HO-77310 | Water damage – flood / rain | 8900 | `basement_repair_quote.txt`, `basement_notes.txt` | Denied under Exclusion 7.3 (may need a retry) |
| Prompt injection | HO-30911 | Storm / roof damage | 6400 | `roof_invoice_injected.txt` | Blocked by input guardrail |
| Human in the loop | AU-20457 | Vehicle theft | 82000 | `theft_police_report.txt`, `vehicle_purchase_invoice.txt` | Paused for adjuster (payout > $10k, high fraud risk) |
