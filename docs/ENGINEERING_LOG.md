# Engineering Log

Actual bugs, failed approaches and fixes, in date order. Design decisions belong in DECISIONS.md.

| Date | Phase | What happened | Fix / outcome |
|------|-------|---------------|---------------|
| 2026-11-08 | 4 | Review found that `PATCH /rules/{id}` awaited the request body after reading the current rule from the Store. Another request could change the rule during that `await`, so the edit could be merged onto an out-of-date rule. No automated test reproduces this race; it was found by reading the code. | The endpoint now reads the body first, then reads the current rule and applies the change with no `await` in between (Decision 76). |
| 2026-11-08 | 4 | Test H1 (demo workflow) compared the Run All *response*, which contains only the newly added notifications, with the full expected history, so the comparison checked the wrong thing. | With approval, the test now compares `GET /notifications` (the full history) with the expected list. The expected list itself did not change. |
| 2026-11-08 | 4 | The sticky replay bar wrapped onto two rows (about 100 px) below 1226 px wide and covered table headings. | The bar is sticky only at widths of 1240 px and above; narrower, it scrolls with the page (Decision 95). |
