# Sample claims for the demo

Upload these files in the Streamlit UI with the form values below.

| Scenario | Policy | Loss type | Amount | Files | Expected |
|---|---|---|---|---|---|
| Clean claim | HO-55821 | Water damage – burst pipe | 3200 | `burst_pipe_invoice.txt`, `burst_pipe_photo_notes.txt` | Approved automatically (Clause 4.2, $500 deductible) |
| Retry | HO-77310 | Water damage – flood / rain | 8900 | `basement_repair_quote.txt`, `basement_notes.txt` | Denied under Exclusion 7.3 (may need a retry) |
| Prompt injection | HO-30911 | Storm / roof damage | 6400 | `roof_invoice_injected.txt` | Blocked by input guardrail |
| Human in the loop | AU-20457 | Vehicle theft | 82000 | `theft_police_report.txt`, `vehicle_purchase_invoice.txt` | Paused for adjuster (payout > $10k, high fraud risk) |
