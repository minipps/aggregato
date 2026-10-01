# Accessibility review

The [1 October browser validation](publication-review/browser-validation.md) records current
automated checks, keyboard interactions, responsive layouts, screenshots, and manual review limits.
The implementation audit below is historical.

Reviewed 2026-07-30 for Phase 10. The application has a semantic `header` / labelled primary
`nav` / `main` landmark shell, a visible-on-focus skip link, and a persistent high-contrast focus
ring. Route links receive their router-provided current-page state. The shell is intentionally
text- and table-led: media images remain optional and never carry essential information.

| Surface | Keyboard and semantics | Text alternatives / status |
|---|---|---|
| Dashboard | Heading hierarchy, provider table headers, keyboard links | Health is named and degraded state uses an alert |
| Log | Labelled filters and native submit/load-more controls | Shared loading, empty, and error states explain the result |
| Work / Creator | Heading-led detail, tables, native merge/split controls | Ratings retain source-scale context; images are decorative |
| Providers / Sync history | Labelled controls and provider/run tables | Status chips use text as well as colour |
| Ingest failures | Native `details` disclosure and replay button | Stored payload label and error text remain selectable |
| Resolution | Native buttons follow queue order; shortcut support is tested | Candidate reasons and link confidence are written out |
| Statistics | Labelled period/toggle controls, definition lists, ranked lists | Explicit notice that sub-units are excluded by default |
| Settings | Native labelled inputs and warning alert | Storage numbers are labelled rather than chart-only |
| Login | Labelled token input and submit control | Authentication errors use the shared problem presentation |

Manual verification procedure: tab from a fresh load to confirm the skip link, every primary-nav
link, each view's controls, and retry/load-more actions appear in visible focus order. Check at 200%
browser zoom that navigation wraps to one column and tables remain readable without clipped data.

Known limitation: this is an implementation audit, not a substitute for testing with screen-reader
users. Future UI additions must repeat this table-row review and preserve the shared shell.
