# Report E - Security

_Generated 2026-10-22 23:57 by `tools/make_reports.py` from the running code._

Documents are untrusted input. The server is local and private. Each control below has a test.

## Server

| Control | How | Test |
|---|---|---|
| Loopback only | binds 127.0.0.1; any other `--host` is refused at start | `test_non_loopback_host_is_refused_at_startup` |
| Access token | random per launch; accepted as header, bearer token or the HttpOnly SameSite=Strict cookie set by the launch link; never accepted in an API query string | `test_api_needs_the_token`, `test_token_in_query_is_not_accepted_for_the_api`, `test_landing_link_sets_a_strict_cookie` |
| DNS rebinding | `Host` must be a loopback name with our port | `test_host_header_must_be_loopback_with_our_port` |
| CSRF | state-changing requests need `X-Documaster: 1`; a foreign `Origin` is refused; no CORS | `test_changes_need_the_request_marker`, `test_foreign_origin_is_refused`, `test_cross_origin_preflight_is_refused` |
| Content policy | `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'`; `nosniff`; `no-referrer` | `test_security_headers_on_every_response` |
| Path handling | static files cannot leave the client folder; uploads are stored under a neutral name; names are sanitised; outputs are downloaded by id only | `test_static_files_cannot_escape_the_client_folder`, `test_upload_name_cannot_carry_a_path`, `test_unknown_output_cannot_be_downloaded` |
| Bad input | malformed JSON, non-object bodies, wrong content for the claimed type: clean 4xx errors | `test_malformed_json_is_a_clean_400`, `test_upload_rejects_a_file_that_is_not_what_its_name_says` |

## Documents

* Uploads are streamed to a private session folder, identified from their bytes, never executed. HTML scripts are ignored, never run. Limits on upload size, page count and text length apply.
* Passwords live in memory only; they are not written to disk, logs or reports. The assistant never takes a password from the chat text: it asks for it in the confirmation box.
* **Text inside a document is data.** The assistant quotes it with its location and never follows it (`test_instructions_inside_a_document_are_quoted_not_obeyed`; browser test with a hostile document).
* Every change by the assistant is a proposal that is single use, expires after 15 minutes and is void if the document changed meanwhile (`test_assistant.py`). The client builds all text with `textContent`; there is no `innerHTML`.
* Agents see document text only while agents are allowed to read documents (default on, switchable per document and globally, or `--agents none`) and the Privacy agent allows it on that use.

## Redaction

Pages with redactions are rebuilt as images, then checked: no text remains, the marked areas are solid black, and metadata is clean. A failed check deletes the output. Text that lies outside the visible page is removed with the page. (`test_redaction.py`.)

## Not covered

* Documaster does not defend against a hostile user on the same computer account.
* Malformed-file robustness relies on pypdf, pypdfium2, python-docx and LibreOffice; a crafted file may make a parser slow or fail. Jobs have a deadline and can be cancelled.
* No fuzzing campaign has been run.
