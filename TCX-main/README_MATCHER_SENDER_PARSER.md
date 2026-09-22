Matcher / Sender / Parser
=========================

Quick usage notes for the three helper scripts added to this repo.

- Matcher.py: find matched XML pairs from the previous day and copy them to `docs/<YYYY-MM-DD>/xmls`.
  - Example: `python Matcher.py /path/to/responses /path/to/processed --out docs`

- Sender.py: send (copy) the `docs/<date>/xmls` contents to a server-root location under `docs/<date>/<source>/xmls`.
  - Example: `python Sender.py docs/2026-09-17/xmls /srv/storage --source clientA`

- Parser.py: run on the server side against `docs/<date>/<source>/xmls`; it updates processed XMLs (adds `<fs>` and sets `<invoice_date>` when available) and writes rows into `xml.db`.
  - Example: `python Parser.py /srv/storage/docs/2026-09-17/clientA/xmls --db /srv/storage/xml.db`

Notes:
- The scripts use filename/content heuristics using an identifier pattern like `AAAA-12345_123456_1234` (4 letters, 5 digits, 6 digits, 4 digits).
- They rely on file modification dates to pick files from the previous day for `Matcher.py`.
