A sales rep is reviewing a draft quote and typed a correction in chat. Convert it into structured patches against the extracted RFQ lines.

Rules:
- Refer to lines by `line_no` (1-based, as shown).
- Only set the fields the rep changed, using the same as-written style as the email ("3/16", "316L", "48x96").
- `remove` deletes a line. `customer_text` changes the customer. `certs_required` toggles certs.
- If the message is not a change request, return no patches.
