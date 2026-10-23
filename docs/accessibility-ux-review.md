# Northstar Shop accessibility and mobile UX review

**Review date:** 2026-10-24
**Scope:** the served page at `/` (`app/static/index.html`, `styles.css`, `app.js`, `i18n.js`) and the existing J01–J10 Playwright journeys.
**Method:** source/DOM-contract inspection, existing deterministic browser journeys, JavaScript unit tests, and a narrow mobile keyboard journey. No application behavior was changed by this review.

## Evidence and limits

The following commands were run from the repository root:

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/e2e/test_j09_keyboard_mobile.py -q` | **1 passed** in 1.37 s (390×844 keyboard/no-overflow/escaping smoke) |
| `.venv/bin/python -m pytest tests/e2e -q -k 'not intent_judge'` | **12 passed, 9 deselected** in 11.70 s; the 12 tests are J01–J10 plus the two additional confirmation/new-intake cases |
| `.venv/bin/python -m pytest tests/e2e -q` | **14 passed, 7 errors** in 11.44 s. The seven errors are the optional LLM-judge cases; `E2E_JUDGE_BASE_URL` and `E2E_JUDGE_MODEL` are not configured. This is unverified, not an application failure. |
| `node --test tests/unit/*.test.js` | **18 passed, 0 failed** |

The existing browser smoke is valuable evidence for its assertions, but it is not a WCAG audit: it does not prove screen-reader output, 200% text resize, 400% reflow, contrast, target size, reduced motion, 320 px layout, or real voice-over behavior. No axe/Accessibility Tree run, manual screen-reader run, contrast analyzer, or assistive-technology run was available in this checkout; those checks are explicitly **unverified** below.

## Confirmed defects

These are defects confirmed by the current HTML/CSS/JavaScript or by a stated current browser contract. Priority reflects likely user impact, not implementation effort.

### F-01 — No user-facing language control

- **Priority:** high
- **User impact / journeys:** A user who needs Spanish cannot choose it from the UI; language is only changed as a side effect of server conversation state. This affects intake, review, errors, confirmation, and speech controls in J01–J10, especially Spanish users arriving before the first supported-language turn or needing to switch language.
- **Location:** `/`; `app/static/index.html:11–43` (no language selector); `app/static/i18n.js:154–213` (`applyStaticLabels` only applies a language selected by session responses); `app/static/app.js:307–321,327–354`.
- **Current behavior:** `html[lang]` starts as `en` and is updated when a session view/turn response supplies `reply_language`. There is no `<select>`/button for language and no documented visible way to change language. The existing unit test proves programmatic label switching, not a user interaction.
- **Expected behavior:** Provide a keyboard-operable, persistently named language control (for example `<label for="language">Language</label><select id="language">…</select>`) with English and Español options. Changing it must update `document.documentElement.lang`, all current static labels, error copy, review copy, and the speech availability state without translating historical message text silently.
- **Proposed change:** Add a labeled native select near the page heading; use the existing `i18n` dictionary for labels and options; announce “Language changed to …” in a status region; keep message-language metadata and per-message playback language intact. Do not use a tooltip as the only language affordance.
- **Test / acceptance assertion:** Playwright at 390 px: Tab reaches the language select, its accessible name is `Language`, arrow keys select `Español`, `document.documentElement.lang === 'es'`, button/label/error names are Spanish, and focus remains on the select. Repeat in reverse. Add a screen-reader assertion that the control has one name and no duplicate language announcements.
- **WCAG:** 3.1.1 Language of Page; 3.1.2 Language of Parts; 3.3.2 Labels or Instructions; 2.1.1 Keyboard.

### F-02 — Async work has no visible busy/status state or progress semantics

- **Priority:** high
- **User impact / journeys:** During session creation/resume, send, retry, confirmation, transcription, and recording startup, users can see disabled controls but cannot tell what is happening, whether the request started, or whether a stalled request is still active. Screen-reader users receive no status. This affects J01, J03–J07, and J10.
- **Location:** `/`; `app/static/index.html:13–42` (no status element); `app/static/app.js:156–210,381–535,623–716,738–798,882–934,968–1022`.
- **Current behavior:** `updateControls()` sets `disabled`, `readOnly`, and `hidden`, but no element gets `aria-busy`, a status message, or a “working” label. `alert` is used only after failures. A 35–45 second deadline can therefore look like a frozen page.
- **Expected behavior:** The active surface exposes a concise, non-duplicated status such as “Sending message…”, “Loading saved intake…”, “Transcribing audio…”, “Confirming details…”, or “Recording”. The relevant region has `aria-busy="true"` during work and returns to false; status text is polite and does not interrupt typed input. Long operations provide a clear recovery path after timeout.
- **Proposed change:** Add one `<p id="status" role="status" aria-live="polite" aria-atomic="true"></p>` and a scoped busy state on the composer/transcript region. Set status at each async transition and clear it on success/failure/stale completion. Use a CSS spinner only as supplemental decoration, with text always present.
- **Test / acceptance assertion:** Hold the J04 request and assert the status text, `aria-busy=true`, disabled controls, and `readonly` message; release and assert a success/failure status and `aria-busy=false`. Repeat STT and confirmation. A screen reader must hear one status per operation, not a repeated transcript.
- **WCAG:** 4.1.2 Name, Role, Value; 4.1.3 Status Messages; 1.3.1 Info and Relationships.

### F-03 — Transcript live region is re-rendered wholesale and can duplicate announcements

- **Priority:** high
- **User impact / journeys:** A screen-reader user may hear the greeting and all prior turns repeatedly whenever a new turn arrives, language changes, or a session is refreshed. Long transcripts become difficult to navigate. This affects all conversational journeys and policy Sources in J08–J09.
- **Location:** `app/static/index.html:13`; `app/static/app.js:292–299,321–324,351–354`; `app/static/styles.css:53–63`.
- **Current behavior:** `#transcript` is `role="log" aria-live="polite" aria-relevant="additions"`, but `renderTranscript()` removes every child and appends every message again. The DOM mutation is not limited to the new message despite the `additions` value.
- **Expected behavior:** Existing messages stay in the DOM; only genuinely new messages are appended to the log. A refresh/reconciliation that does not add a message must not announce the conversation again. The log has an accessible name and a navigable heading/landmark.
- **Proposed change:** Key message elements by saved message ID (or use a stable append/update strategy); avoid `clearChildren` for ordinary additions. Give the log an explicit accessible name, e.g. `aria-label="Conversation"`, and keep review/status/errors outside the live log.
- **Test / acceptance assertion:** With a screen-reader test harness or Accessibility Tree capture, send two turns and assert that the second update exposes only the new user/assistant pair. Reload/resume and language relabeling must produce no new live announcement. DOM assertion: existing message node identity is preserved when a later turn is appended.
- **WCAG:** 4.1.3 Status Messages; 1.3.1 Info and Relationships; 2.4.6 Headings and Labels.

### F-04 — Form error/help text is not programmatically associated with the textarea

- **Priority:** high
- **User impact / journeys:** Character-limit and transcription guidance are visible, but a user of a screen reader does not reliably receive the relevant instruction when focusing Message. There is no invalid state or error association for the 4000-character limit.
- **Location:** `app/static/index.html:20–22`; `app/static/app.js:168–209,1078–1087`; `app/static/styles.css:180–184`.
- **Current behavior:** `textarea#message` has a visible label and an extra identical `aria-label`, but no `aria-describedby`, `aria-invalid`, `maxlength`, or error ID. `#transcribe-help` is toggled independently and `#alert` is a global alert.
- **Expected behavior:** The field announces its purpose, limits, current invalid state, and recovery instruction. Over-limit input is prevented or clearly rejected at the field; the user can recover by editing without guessing why Send is disabled.
- **Proposed change:** Add `maxlength="4000"` (if the intended behavior is hard limit) or keep soft validation but add `aria-describedby="message-help message-error"`; add a persistent concise help sentence; toggle `aria-invalid="true"` and an inline `role="alert"`/`aria-live="polite"` error with `for`/ID association. Remove the redundant `aria-label` when the visible label is the accessible name source.
- **Test / acceptance assertion:** Focus Message with an accessibility-tree check and verify name, description, required/limit information. Enter 4001 characters; assert invalid state, one actionable error (“Remove … characters”), Send disabled, and recovery after editing. Test Spanish copy.
- **WCAG:** 1.3.1 Info and Relationships; 3.3.1 Error Identification; 3.3.2 Labels or Instructions; 4.1.2 Name, Role, Value.

### F-05 — Review and confirmation state changes do not move or restore focus

- **Priority:** high
- **User impact / journeys:** After a successful turn, a review region and Confirm details button appear below the current focus. After Retry, New intake, confirmation, or a failure, focus remains on a removed/disabled control or at an unpredictable point. Keyboard and screen-reader users must search the page to continue.
- **Location:** `app/static/index.html:15,26–29`; `app/static/app.js:212–221,292–355,569–621,738–821`.
- **Current behavior:** Rendering toggles `hidden` and appends nodes but never calls a deliberate focus target. New intake keeps the old transcript visible while the request is pending, then replaces it without focus management.
- **Expected behavior:** On review creation, focus moves to the review heading or a concise status, then the user can reach Confirm. On an error, focus moves to the error summary/alert; on Retry completion, focus returns to the initiating control; on New intake completion, focus returns to Message. Do not steal focus while a user is typing.
- **Proposed change:** Add a focusable (`tabindex="-1"`) heading inside review and a stable error/status target. Store the initiating element before async work, focus it only after the corresponding state transition, and guard stale generations. Announce review availability without duplicating the whole transcript.
- **Test / acceptance assertion:** Keyboard-only J01: after HIGH, focus is on the review heading or status and Tab reaches Confirm; after a J03 failure, focus is on Retry; after New intake, focus is Message; after J07 TTS failure, focus is the error target and text remains usable. Assert no focus jump during typing.
- **WCAG:** 2.4.3 Focus Order; 2.4.7 Focus Visible; 2.4.11 Focus Not Obscured; 3.2.1 On Focus.

### F-06 — Recording state and microphone failures are not adequately announced

- **Priority:** high
- **User impact / journeys:** A recording user has no visible elapsed-time limit, recording indicator, live state, or explicit instruction that recording has begun/stopped. Permission denial is only a global alert and focus does not move to it. This affects J06, J07, and J10.
- **Location:** `app/static/index.html:31–37`; `app/static/app.js:968–1070`; `app/static/i18n.js:30–35,62–67`.
- **Current behavior:** Start/Stop visibility and disabled state change, but no status text or `aria-pressed`/`aria-label` state is exposed. The automatic 60-second cutoff is silent. A denial message is shown in `role="alert"`, but no recovery focus is specified.
- **Expected behavior:** Start has a stable name; active recording exposes “Recording. Maximum 60 seconds. Stop recording.” and elapsed time where useful; Stop ends the state and announces that a take is ready for transcription. Permission failure says what happened and gives the nearby typed/upload alternatives, with focus on the recovery message or first alternative.
- **Proposed change:** Use a button state (`aria-pressed` only if it remains a toggle) or separate named buttons plus a `role="status"` recording status; provide a text timer that is not updated every second to a live region; localize all states; add a visible “recorded audio ready” state tied to Transcribe audio. Catch `getUserMedia` failures with actionable copy for permission, unsupported browser, and device-in-use cases.
- **Test / acceptance assertion:** J06/J07/J10 plus manual screen reader: Start announces recording, Stop announces ready state, auto-stop announces limit, denial leaves Message and WAV controls usable and announces one recovery message. Keyboard can activate both controls.
- **WCAG:** 1.3.1 Info and Relationships; 4.1.2 Name, Role, Value; 4.1.3 Status Messages; 3.3.3 Error Suggestion.

### F-07 — WAV upload control is visually clipped and its keyboard focus treatment is ineffective

- **Priority:** high
- **User impact / journeys:** The real file input is clipped to 1×1 px while the styled `<label>` is not focusable. Keyboard users can encounter an invisible focus target; touch users get a small dashed label, and the selected filename/state is not surfaced. This affects J06/J07 and mobile users who cannot use a microphone.
- **Location:** `app/static/index.html:35–36`; `app/static/styles.css:108–116,210–229`; `app/static/app.js:1072–1076`.
- **Current behavior:** `#wav-recording` is visually hidden with clipping. CSS includes a focus selector for the input, but the input cannot present a useful visible outline in that state. The label says only “WAV recording”; no selected file name or accepted-format instruction is shown.
- **Expected behavior:** Keyboard focus is visibly on a usable upload control; the control says accepted format/size and exposes the selected file name or validation error. The control remains operable at 200% text size and on a narrow viewport.
- **Proposed change:** Keep the native input in the tab order with a visually hidden technique that preserves a focus proxy, or use a real styled `<label>` with an adjacent status and a visible focus style on the label via `:focus-within`. Add `accept` guidance, filename status, and localized invalid/oversize recovery copy.
- **Test / acceptance assertion:** Tab through the voice controls at 390×844 and 200% zoom; assert a visible focus indicator, accessible name/description, selected filename, and keyboard activation. Upload invalid, silent, oversized, and valid files; assert one associated actionable error.
- **WCAG:** 2.1.1 Keyboard; 2.4.7 Focus Visible; 1.4.10 Reflow; 3.3.1 Error Identification.

### F-08 — Interactive target sizing is below the mobile target recommendation

- **Priority:** medium
- **User impact / journeys:** `.btn` padding is approximately 36–38 px tall at the normal font size, and controls can be tightly packed by a `0.45rem` gap. This increases accidental activation and makes voice controls difficult to use one-handed. A 390 px no-overflow pass does not prove target size.
- **Location:** `app/static/styles.css:186–203,210–217,248–256`.
- **Current behavior:** Buttons use `padding: 0.45rem 0.8rem`; the 420 px media rule reduces padding further to `0.4rem 0.65rem`. There is no minimum inline/block size or coarse-pointer adjustment.
- **Expected behavior:** Primary controls meet at least 44×44 CSS px where practical, have at least 8 px spacing, and remain readable when zoomed. Text wrapping must not reduce the hit area.
- **Proposed change:** Set `min-block-size: 44px` and suitable inline padding on buttons, file trigger, source summaries, and language control; use a coarse-pointer media query only if needed; allow action groups to stack on narrow screens.
- **Test / acceptance assertion:** Browser measure all interactive bounding boxes at 320, 390, and 768 px; assert minimum target dimensions/spacing and activation with touch emulation. Verify no clipped text at 200% zoom.
- **WCAG:** 2.5.8 Target Size (Minimum) (AA, 24 px minimum; 44 px is the recommended product target); 1.4.4 Resize Text; 1.4.10 Reflow.

### F-09 — The page hides horizontal overflow instead of proving content reflows

- **Priority:** medium
- **User impact / journeys:** `overflow-x: hidden` can conceal clipped content or controls. The hostile long-token J09 check passes at 390 px, but hidden overflow can mask future content, larger text, source titles, or native audio controls. This affects mobile and text-resize users.
- **Location:** `app/static/styles.css:22–35,53–63,80–84,118–123,130–147`; `tests/e2e/test_j09_keyboard_mobile.py:103,114`.
- **Current behavior:** The document globally hides horizontal overflow. Text uses aggressive `word-break: break-word`; no 320 px, 200% text, or 400% reflow check exists.
- **Expected behavior:** Content naturally wraps or scrolls within the component that owns overflow; the document does not need to hide a layout defect. At 320 px and 400% equivalent reflow, every action and message remains reachable without two-dimensional scrolling except content that genuinely requires it.
- **Proposed change:** Remove global `overflow-x:hidden` after fixing offending objects; use `overflow-wrap:anywhere` selectively; give transcript/source bodies local overflow only where needed; test native audio and long labels separately. Add responsive breakpoints based on content, not only `max-width:420px`.
- **Test / acceptance assertion:** Playwright widths 320/390/768/1280 plus browser text zoom/forced font-size; assert `scrollWidth <= clientWidth`, no clipped focus ring, reachable actions, and no two-axis page scrolling. Mark any intentionally scrollable transcript as such and keyboard reachable.
- **WCAG:** 1.4.10 Reflow; 1.4.4 Resize Text; 2.4.11 Focus Not Obscured.

### F-10 — Playback audio has no explicit accessible name or playback status

- **Priority:** medium
- **User impact / journeys:** Native controls may be exposed inconsistently across browsers and screen readers, especially when several saved replies exist. Users are told “Voice is AI-generated” but not which reply the audio belongs to, whether it is loading, or why playback failed. This affects J06, J07, J09, and J10.
- **Location:** `app/static/index.html:39–42`; `app/static/app.js:262–287,824–879`; `app/static/playback_labels.js`; `app/static/styles.css:231–245`.
- **Current behavior:** `<audio controls>` has no `aria-label`, `aria-describedby`, or fallback text. Play buttons are correctly numbered by the existing contract, but the single shared audio element is replaced for each request and its load/play/failure state is silent except a global alert on failure.
- **Expected behavior:** The audio player has a localized name such as “Reply audio, reply 2” and a description/disclosure. Loading, playing, ended, and unavailable states are exposed without duplicate announcements; a failed play points to the still-available text reply.
- **Proposed change:** Update the audio accessible name/description from the selected message; add a scoped status; preserve native controls and provide text fallback. Do not make autoplay the only way to start playback; keep Play reply as the keyboard action.
- **Test / acceptance assertion:** Accessibility Tree sees one named player; with two replies, selecting each updates the name; keyboard Space/Enter on Play reply loads it; simulated TTS failure announces one actionable message and leaves Send/Confirm usable. Verify native controls at mobile width.
- **WCAG:** 4.1.2 Name, Role, Value; 1.2.2 Captions (if spoken content is intended as prerecorded multimedia, provide an equivalent transcript—the visible reply text is the candidate); 4.1.3 Status Messages.

### F-11 — Error handling uses a global alert without association, focus policy, or deduplication

- **Priority:** medium
- **User impact / journeys:** Errors from resume, turn, STT, TTS, confirmation, timeout, and microphone paths all overwrite one global `role="alert"`. Users may miss a message far above the current control, hear repeated/competing errors, or not know which action fixes it. This affects J03–J07 and J10.
- **Location:** `app/static/index.html:17`; `app/static/app.js:94–114,359–379,421–471,498–535,651–692,759–766,845–879,907–927,1062–1066`.
- **Current behavior:** `showAlert()` replaces text and unhides the alert, but does not focus it, set `aria-atomic`, link it to the failed control, or prevent an old stale error from remaining visible. Some errors are hard-coded English (`app.js:1062–1064`) instead of using `i18n`.
- **Expected behavior:** Errors identify the failed operation, state whether data was saved, and give the next action. Field errors are associated with fields; operation errors are scoped to the operation and have one live announcement. Stale callbacks cannot surface an old error. All copy is localized.
- **Proposed change:** Add stable error IDs and `aria-describedby`/`aria-errormessage` where supported; use `role="alert"` only for urgent failures and `role="status"` for recoverable completion; focus the error summary after an operation fails; centralize all messages through `i18n` including WAV-limit failure.
- **Test / acceptance assertion:** J03/J04/J07: assert exact operation-specific copy, one announcement, focus on recovery action, preserved draft, retry availability, and Spanish equivalents. J10: release stale STT/TTS and assert no stale error is shown.
- **WCAG:** 3.3.1 Error Identification; 3.3.3 Error Suggestion; 4.1.3 Status Messages; 3.1.1 Language of Page.

### F-12 — Confirmation receipt exposes the internal session UUID

- **Priority:** medium
- **User impact / journeys:** Users see an implementation identifier in the final confirmation, which is confusing and can disclose an internal correlation token. This affects J01, J03, J06–J09 and the completed/reload path.
- **Location:** the rendered transcript path `app/static/app.js:251–261,292–299`; server receipt contract used by `app/conversation.py:406` and `app/prompts.py:461`; expected by `tests/support/fakes.py:399` and J01/J03/J04/J05/J06/J07/J08/J09.
- **Current behavior:** The frontend safely renders server text as a text node, but the receipt text includes `session_id`; the browser contract explicitly says the target UUID must not be visible text, accessible name, title, or tooltip.
- **Expected behavior:** Show a human-facing confirmation/reference number that is intentionally designed and safe, or omit the internal UUID. Internal IDs remain only in API/local storage/private bindings.
- **Proposed change:** Change receipt copy/contract to a non-sensitive reference (or “Your intake was confirmed”) and update UI/E2E assertions. Do not put the UUID into an ARIA label as a workaround.
- **Test / acceptance assertion:** Complete J01/J09 and assert the visible transcript, accessible names, titles, and DOM text contain no canonical UUID; separately assert API request routing still uses the private session ID. Update the no-regression fixture contract deliberately.
- **WCAG / privacy:** WCAG 3.3.1 Error Identification is not the primary issue; this is a usability/privacy requirement from `docs/implementation-contract.md:310`.

## Recommendations and unverified checks

These items are important for a production-ready AA review but are not asserted as confirmed defects without a browser/AT measurement.

### R-01 — Add semantic landmarks and headings for every state

- **Priority:** medium
- **Impact / journey:** Long transcripts and review/source/error areas are difficult to skim with landmark navigation.
- **Location:** `index.html:10–43`; `app.js:223–249,251–289`.
- **Recommendation:** Use `<header>`, `<section aria-labelledby>`, `<form>` for the composer, headings for Conversation, Review, Voice input, and Sources, and a clear confirmation heading. Keep one page `<h1>` and a logical heading order.
- **Test:** Accessibility Tree has one h1, ordered headings, named landmarks, and no empty unnamed region at each J01/J08 state.
- **WCAG:** 1.3.1; 2.4.6. **Unverified:** no Accessibility Tree/AT run was available.

### R-02 — Verify color contrast, focus contrast, forced colors, and dark/high-contrast modes

- **Priority:** high if a failure is found; currently unverified
- **Location:** `styles.css:1–10,71–78,140–147,180–184,242–245`.
- **Recommendation:** Measure text, border, disabled, alert, focus, link/source, and disclosure colors against WCAG AA. Ensure the 3 px focus ring remains visible in Windows High Contrast/forced colors; do not rely on opacity alone for disabled explanations.
- **Test:** axe plus manual contrast sampling for normal/hover/focus/disabled/alert states; forced-colors browser run.
- **WCAG:** 1.4.3 Contrast (Minimum); 1.4.11 Non-text Contrast; 2.4.7 Focus Visible. **Unverified:** no contrast tool or forced-colors run.

### R-03 — Test 200% text resize and 400% reflow with browser zoom and user font settings

- **Priority:** high if a failure is found; currently unverified
- **Location:** all `/` states; `styles.css:37–44,186–192,231–240`.
- **Recommendation:** Use rem-based spacing/type, allow action groups to stack, keep native audio usable, and ensure focus is not clipped by transcript max-height or overflow.
- **Test:** Playwright at 320/390 px with 200% text size and 400% equivalent viewport; run J01 review/confirmation, J06 voice, J08 Sources, and J03 error/resume. Assert every primary action is reachable.
- **WCAG:** 1.4.4; 1.4.10. **Unverified:** existing J09 only checks 390×844 at default sizing.

### R-04 — Add reduced-motion and media preference checks before introducing loading animation

- **Priority:** low
- **Location:** `styles.css` has no animation today; future loading/recording indicator location is the composer/voice region.
- **Recommendation:** Prefer a static text status. If a spinner/pulse is added, wrap animation in `@media (prefers-reduced-motion: no-preference)` and provide an immediate non-motion state. Avoid flashing recording indicators.
- **Test:** Emulate `prefers-reduced-motion: reduce`; assert no non-essential animation runs and status remains understandable.
- **WCAG:** 2.3.1 Three Flashes or Below Threshold; 2.2.2 Pause, Stop, Hide. **Unverified:** no motion currently exists and no preference test exists.

### R-05 — Define the accessible names and descriptions as a testable ARIA inventory

- **Priority:** high
- **Affected controls:** `/`.
- **Required names:**
  - `textarea#message`: **Message**; description includes “4000 characters maximum” and any current field error.
  - `#send`: **Send**; disabled state must be explained by field validation/status, not name mutation.
  - `#retry`: **Retry**; description names whether session loading, turn, confirmation, or new intake is being retried.
  - `#confirm`: **Confirm details**; description points to Review details.
  - `#new-intake`: **Start a new intake** (or keep “New intake” consistently); description warns that it clears the current draft/view but does not delete the saved session.
  - `#start-recording`: **Start recording**; recording status described separately.
  - `#stop-recording`: **Stop recording**; active recording status described separately.
  - `#transcribe`: **Transcribe audio**; description says a selected/recorded WAV is required and the result fills Message without sending.
  - `#wav-recording`: **WAV recording**; description states PCM WAV, maximum size, and selected filename.
  - language select: **Language** with **English** and **Español** options.
  - each playback button: **Play reply {ordinal}**; if unavailable, disabled name should say why without exposing a UUID.
  - audio player: **Reply audio, reply {ordinal}** plus AI-generated disclosure.
  - transcript: **Conversation**; review: **Review details**; Sources: **Sources**; status/error regions have stable names only where needed.
- **Test:** Use Playwright `get_by_role` plus an Accessibility Tree snapshot; assert every interactive control has exactly one meaningful name and every dynamic description is associated.
- **WCAG:** 4.1.2; 3.3.2; 1.3.1.

### R-06 — Define explicit keyboard and screen-reader acceptance paths

- **Priority:** high
- **Acceptance path:**
  1. Load `/` at 390×844. Tab from the browser document start through heading/Language, Message, Send, Retry when present, Confirm when present, New intake, Start/Stop recording, Transcribe audio, WAV upload, playback, and Sources summaries. There must be no unreachable control, focus trap, or invisible focused target.
  2. Type a message, submit with Enter only where the control contract permits, and verify one status; while pending, verify the draft is not lost.
  3. Trigger a failed resume/turn/STT/TTS/confirmation. Focus lands on the error/recovery target; the message explains what to do; Retry is reachable and does not duplicate the request.
  4. Reach Review, inspect all four facts, activate Confirm with Space/Enter, and verify a named confirmation status and no UUID leakage.
  5. Start/stop recording and upload a WAV with keyboard; verify state, file name, transcription result, and fallback to text.
  6. Open Sources with Enter/Space and navigate its summary/details content without losing context.
  7. Switch English/Spanish; verify page language, control names, status/error copy, and per-message historical language behavior.
- **Screen-reader assertions:** one greeting only; new turns only once; status/busy/errors once; field error read on focus; review/receipt announced after state change; playback and recording state named; focus return deterministic.
- **WCAG:** 2.1.1; 2.4.3; 2.4.7; 4.1.2; 4.1.3. **Current status:** J09 proves only a subset of the keyboard path; screen-reader assertions are unverified.

## Prioritized change plan

1. **P0 / before accessibility sign-off:** add operation status/busy semantics (F-02), fix live-region update strategy (F-03), associate field errors and limits (F-04), implement deterministic focus management (F-05), and make recording/upload states accessible (F-06/F-07).
2. **P1 / core mobile release:** add a user-facing language control (F-01), remove UUID from visible receipt text (F-12), verify/repair contrast and focus contrast (R-02), and meet target sizing/reflow requirements (F-08/F-09/R-03).
3. **P1 / voice and media:** name the shared audio player and expose playback status (F-10); keep text fallback and failure recovery primary.
4. **P2 / structure and regression hardening:** add landmarks/headings (R-01), reduced-motion coverage (R-04), the ARIA inventory tests (R-05), and the complete keyboard/screen-reader acceptance path (R-06).

## No-regression checklist for J01–J10

The implementation of these findings must preserve the existing journey contracts. Re-run the full real-browser suite, not only unit tests:

- [ ] **J01:** fresh greeting; social and mixed facts; natural prompts; exact four-slot review; Confirm details; one confirmation; reload completed view read-only; New intake creates a separate empty session. Preserve keyboard focus/status improvements and do not expose internal UUID in visible receipt.
- [ ] **J02:** invalid order retains valid problem; correction/conflict/rejection/recovery; Confirm stays hidden until a fresh valid review; errors identify the affected recovery path.
- [ ] **J03:** refresh/resume failure retains local session and shows Retry; Retry is keyboard reachable/focusable; no replacement POST; server restart restores transcript; focus/status do not duplicate history.
- [ ] **J04:** pending request locks exactly the intended controls, preserves draft, shows one busy status, and Retry uses the same serialized request without duplicate turns.
- [ ] **J05:** stale two-tab conflict has a named, visible, associated error; refreshed draft remains; explicit new Send/Confirm works; no cross-tab focus or announcement assumption is introduced.
- [ ] **J06:** microphone denial leaves typed/upload paths usable; Start/Stop recording state and 60-second cutoff are announced; recording/upload fills an editable draft without committing; MP3/WAV playback is real and named; AI-generated disclosure remains visible; confirmation succeeds.
- [ ] **J07:** STT and TTS outages produce localized actionable errors, retain text/review, do not poison state, and allow one normal receipt.
- [ ] **J08:** supported policy answers expose a named Sources region and details summaries; unsupported payment question has no Sources; review token/summary survives; Sources restore after reload without replaying the whole transcript to a screen reader.
- [ ] **J09:** at 390×844, hostile text remains inert/escaped; controls have names and visible focus; Sources/Play/Confirm are keyboard reachable; no document overflow; add assertions for the new status, language, upload, and focus behavior.
- [ ] **J10:** stale STT/TTS callbacks cannot change the new session, draft, audio, status, or error; only latest audio owns the player; pagehide pauses/revokes audio; Send remains usable during playback.

**Overall assessment:** the current implementation has a solid deterministic baseline: the shipped J01–J10 browser journeys (excluding the separately configured LLM judge) pass, J09 proves a useful 390 px keyboard/no-overflow/content-escaping slice, and unit label tests pass. It is not yet an accessibility sign-off because async status, focus transitions, dynamic announcement boundaries, form error association, recording/upload semantics, language choice, target sizing/reflow, contrast, reduced motion, and screen-reader behavior remain unproven or defective as listed above.
