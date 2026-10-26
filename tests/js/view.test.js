import { beforeEach, describe, expect, jest, test } from "@jest/globals";

import { EN, ES, buildApp } from "./support/page.js";

let app;
let view;
let state;
let dom;

beforeEach(() => {
  app = buildApp();
  ({ view, state, dom } = app);
});

const assistantMessage = (overrides = {}) => ({
  id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  role: "assistant",
  text: "Hello there",
  citations: [],
  speech_available: true,
  ...overrides,
});

describe("alert and verification form", () => {
  test("showAlert writes plain text and shows the alert; clearAlert hides it again", () => {
    view.showAlert("<b>bad</b>");

    expect(dom.alert.hidden).toBe(false);
    expect(dom.alert.textContent).toBe("<b>bad</b>");
    expect(dom.alert.querySelector("b")).toBeNull();

    state.charLimitAlert = true;
    view.clearAlert();

    expect(dom.alert.hidden).toBe(true);
    expect(dom.alert.textContent).toBe("");
    expect(state.charLimitAlert).toBe(false);
  });

  test("verification error and status are shown and cleared independently", () => {
    view.setVerificationError("wrong");
    view.showVerificationStatus("verified");

    expect(dom.verificationError.hidden).toBe(false);
    expect(dom.verificationError.textContent).toBe("wrong");
    expect(dom.verificationStatus.hidden).toBe(false);
    expect(dom.verificationStatus.textContent).toBe("verified");

    view.clearVerificationError();
    expect(dom.verificationError.hidden).toBe(true);
    expect(dom.verificationStatus.hidden).toBe(false);

    view.clearVerificationStatus();
    expect(dom.verificationStatus.hidden).toBe(true);
  });

  test("resetVerificationForm empties the fields and both messages", () => {
    dom.postcode.value = "SW1A";
    dom.code.value = "123";
    view.setVerificationError("x");
    view.showVerificationStatus("y");

    view.resetVerificationForm();

    expect(dom.postcode.value).toBe("");
    expect(dom.code.value).toBe("");
    expect(dom.verificationError.hidden).toBe(true);
    expect(dom.verificationStatus.hidden).toBe(true);
  });

  test("the form shows only when required and verify needs both fields", () => {
    state.orderVerificationRequired = true;
    view.renderVerification();
    expect(dom.verification.hidden).toBe(false);
    expect(dom.verify.disabled).toBe(true);

    dom.postcode.value = "SW1A 1AA";
    view.renderVerification();
    expect(dom.verify.disabled).toBe(true);

    dom.code.value = "246810";
    view.renderVerification();
    expect(dom.verify.disabled).toBe(false);

    state.verificationPending = true;
    view.renderVerification();
    expect(dom.verify.disabled).toBe(true);

    state.orderVerificationRequired = false;
    view.renderVerification();
    expect(dom.verification.hidden).toBe(true);
  });
});

describe("updateControls", () => {
  test("send needs a session and a non-blank draft", () => {
    view.updateControls();
    expect(dom.send.disabled).toBe(true);

    state.sessionId = "s";
    dom.message.value = "   ";
    view.updateControls();
    expect(dom.send.disabled).toBe(true);

    dom.message.value = "hello";
    view.updateControls();
    expect(dom.send.disabled).toBe(false);
  });

  test("a draft over the limit disables send and raises one alert, cleared when it shrinks", () => {
    state.sessionId = "s";
    dom.message.value = "x".repeat(4001);
    view.updateControls();
    view.updateControls();

    expect(dom.send.disabled).toBe(true);
    expect(dom.alert.textContent).toBe(EN["ui.char_limit"]);
    expect(state.charLimitAlert).toBe(true);

    dom.message.value = "short";
    view.updateControls();

    expect(dom.alert.hidden).toBe(true);
    expect(state.charLimitAlert).toBe(false);
  });

  test("a handed-off conversation locks the composer", () => {
    state.sessionId = "s";
    state.status = "handed_off";
    dom.message.value = "hello";

    view.updateControls();

    expect(dom.message.readOnly).toBe(true);
    expect(dom.send.disabled).toBe(true);
    expect(dom.startRecording.disabled).toBe(true);
    expect(dom.newIntake.disabled).toBe(false);
  });

  test("a completed conversation keeps typing open, because the receipt asks if there is more", () => {
    state.sessionId = "s";
    state.status = "completed";
    dom.message.value = "hello";

    view.updateControls();

    expect(dom.message.readOnly).toBe(false);
    expect(dom.send.disabled).toBe(false);
    expect(dom.startRecording.disabled).toBe(true);
    expect(dom.newIntake.disabled).toBe(false);
  });

  test("a request in flight locks the composer and the new-conversation button", () => {
    state.sessionId = "s";
    state.pending = true;

    view.updateControls();

    expect(dom.message.readOnly).toBe(true);
    expect(dom.newIntake.disabled).toBe(true);
    expect(dom.startRecording.disabled).toBe(true);
    expect(dom.wav.disabled).toBe(true);
    expect(dom.wavAttach.disabled).toBe(true);
  });

  test("confirm shows only when the review can be confirmed and nothing blocks it", () => {
    state.sessionId = "s";
    view.updateControls();
    expect(dom.confirm.hidden).toBe(true);

    state.status = "awaiting_confirmation";
    state.pendingReviewRevision = 3;
    view.updateControls();
    expect(dom.confirm.hidden).toBe(false);
    expect(dom.confirm.disabled).toBe(false);

    state.orderVerificationRequired = true;
    view.updateControls();
    expect(dom.confirm.hidden).toBe(true);

    state.orderVerificationRequired = false;
    state.pending = true;
    view.updateControls();
    expect(dom.confirm.disabled).toBe(true);
  });

  test("recording controls follow the recording state and the draft", () => {
    state.sessionId = "s";
    view.updateControls();
    expect(dom.startRecording.disabled).toBe(false);
    expect(dom.stopRecording.hidden).toBe(true);

    dom.message.value = "typed";
    view.updateControls();
    expect(dom.startRecording.disabled).toBe(true);
    expect(dom.wav.disabled).toBe(true);

    dom.message.value = "";
    state.recording = {};
    view.updateControls();
    expect(dom.startRecording.disabled).toBe(true);
    expect(dom.stopRecording.hidden).toBe(false);
    expect(dom.stopRecording.disabled).toBe(false);
  });

  test("transcription locks every control and restores them afterwards", () => {
    state.sessionId = "s";
    dom.message.value = "";
    view.updateControls();
    const wasDisabled = dom.newIntake.disabled;

    state.sttPending = true;
    view.updateControls();

    expect(dom.help.hidden).toBe(false);
    expect(dom.help.textContent).toBe(EN["ui.transcribing"]);
    expect(dom.chatPanel.getAttribute("aria-busy")).toBe("true");
    expect(dom.retry.disabled).toBe(true);
    for (const button of dom.chatPanel.querySelectorAll("button, input, textarea")) {
      expect(button.disabled).toBe(true);
    }

    state.sttPending = false;
    view.updateControls();

    expect(dom.help.hidden).toBe(true);
    expect(dom.chatPanel.hasAttribute("aria-busy")).toBe(false);
    expect(dom.newIntake.disabled).toBe(wasDisabled);
    expect(dom.retry.disabled).toBe(false);
  });

  test("time buttons are disabled while busy and enabled otherwise", () => {
    state.slotOptions = [{ starts_at: "2026-11-02T09:00", ends_at: "2026-11-02T11:00" }];
    view.renderSlots();

    view.updateControls();
    expect(dom.slotPicker.querySelector("button").disabled).toBe(false);

    state.pending = true;
    view.updateControls();
    expect(dom.slotPicker.querySelector("button").disabled).toBe(true);
  });
});

describe("review", () => {
  test("shows the summary only while it can be confirmed", () => {
    state.summary = "Account reference: NS-000123";
    view.renderReview();
    expect(dom.review.hidden).toBe(true);

    state.status = "awaiting_confirmation";
    state.pendingReviewRevision = 2;
    view.renderReview();
    expect(dom.review.hidden).toBe(false);
    expect(dom.review.textContent).toBe("Account reference: NS-000123");

    state.pendingClarifications = { urgency: {} };
    view.renderReview();
    expect(dom.review.hidden).toBe(true);
    expect(dom.review.textContent).toBe("");
  });
});

describe("time buttons", () => {
  const slot = { starts_at: "2026-11-02T09:00", ends_at: "2026-11-02T11:00" };

  test("the label gives the weekday, date and both clock times in the active language", () => {
    expect(view.slotLabel(slot)).toMatch(/Mon.*Nov.*09:00.11:00/);

    state.language = "es";
    expect(view.slotLabel(slot)).toMatch(/lun.*nov.*09:00.11:00/i);
  });

  test("renders one button per option and calls the booking handler on click", () => {
    const bookSlot = jest.fn();
    view.bind({ bookSlot });
    state.slotOptions = [slot, { starts_at: "2026-11-02T11:00", ends_at: "2026-11-02T13:00" }];

    view.renderSlots();
    const buttons = dom.slotPicker.querySelectorAll("button");

    expect(buttons).toHaveLength(2);
    expect(buttons[0].dataset.startsAt).toBe("2026-11-02T09:00");
    expect(buttons[0].type).toBe("button");
    expect(dom.slotPicker.hidden).toBe(false);
    buttons[1].click();
    expect(bookSlot).toHaveBeenCalledWith(state.slotOptions[1]);
  });

  test("with no options the picker is emptied and hidden", () => {
    state.slotOptions = [slot];
    view.renderSlots();
    state.slotOptions = [];

    view.renderSlots();

    expect(dom.slotPicker.children).toHaveLength(0);
    expect(dom.slotPicker.hidden).toBe(true);
  });
});

describe("transcript", () => {
  test("an assistant message has a play button; a user message does not", () => {
    const assistant = view.renderMessage(assistantMessage(), 1);
    const user = view.renderMessage({ role: "user", text: "Hi", citations: [] }, 0);

    const play = assistant.querySelector("button[data-message-id]");
    expect(play.dataset.messageId).toBe("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
    expect(play.getAttribute("aria-label")).toBe("Play reply 1");
    expect(play.disabled).toBe(false);
    expect(play.querySelector("svg[aria-hidden='true']")).not.toBeNull();
    expect(user.querySelector("button")).toBeNull();
    expect(assistant.textContent).toContain(EN["ui.assistant"]);
    expect(user.textContent).toContain(EN["ui.you"]);
  });

  test("message text is inserted as text, never as markup", () => {
    const bubble = view.renderMessage(assistantMessage({ text: '<img src="x" onerror="boom()">' }), 1);

    expect(bubble.querySelector("img")).toBeNull();
    expect(bubble.textContent).toContain('<img src="x" onerror="boom()">');
  });

  test("a clicked play button calls the playback handler with the message id", () => {
    const playReply = jest.fn();
    view.bind({ playReply });

    view.renderMessage(assistantMessage(), 1).querySelector("button").click();

    expect(playReply).toHaveBeenCalledWith("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa");
  });

  test("a reply that cannot be spoken has a disabled button with the unavailable label", () => {
    const bubble = view.renderMessage(assistantMessage({ speech_available: false }), 1);
    const play = bubble.querySelector("button");

    expect(play.disabled).toBe(true);
    expect(play.getAttribute("aria-label")).toBe(EN["speech.unavailable"]);
    expect(play.dataset.playable).toBe("false");
  });

  test("citations appear in a Sources region that quotes the record and its version", () => {
    const bubble = view.renderMessage(
      assistantMessage({
        citations: [
          { title: "Returns", source_id: "faq-returns", excerpt: "30 days", kb_version: "2026-10-25" },
        ],
      }),
      1
    );

    const region = bubble.querySelector("[role='region']");
    expect(region.getAttribute("aria-label")).toBe(EN["ui.sources"]);
    expect(region.querySelector("summary").textContent).toBe("Returns faq-returns");
    expect(region.textContent).toContain("30 days");
    expect(region.textContent).toContain("Knowledge base version: 2026-10-25");
  });

  test("no citations means no Sources region", () => {
    expect(view.renderMessage(assistantMessage(), 1).querySelector("[role='region']")).toBeNull();
  });

  test("renderTranscript numbers the replies and adds the pending message and thinking dots", () => {
    state.messages = [
      assistantMessage({ id: "a1" }),
      { role: "user", text: "Q", citations: [] },
      assistantMessage({ id: "a2" }),
    ];
    state.pendingTurnMessage = { role: "user", text: "Next", citations: [] };
    state.turnPending = true;

    view.renderTranscript();

    const labels = [...dom.log.querySelectorAll("button[data-message-id]")].map((b) => b.getAttribute("aria-label"));
    expect(labels).toEqual(["Play reply 1", "Play reply 2"]);
    expect(dom.log.children).toHaveLength(5);
    const thinking = dom.log.querySelector("[role='status']");
    expect(thinking.getAttribute("aria-label")).toBe(EN["ui.thinking"]);
    expect(thinking.querySelectorAll(".dots__dot")).toHaveLength(3);
  });

  test("renderTranscript replaces what was there", () => {
    state.messages = [assistantMessage()];
    view.renderTranscript();
    view.renderTranscript();

    expect(dom.log.children).toHaveLength(1);
  });

  test("updateSpeechButtons marks the reply being fetched as loading and leaves others alone", () => {
    state.messages = [assistantMessage({ id: "a1" }), assistantMessage({ id: "a2", speech_available: false })];
    view.renderTranscript();
    state.speechPending = { generation: state.generation, messageId: "a1", token: 1 };

    view.updateSpeechButtons();
    const [first, second] = dom.log.querySelectorAll("button[data-message-id]");

    expect(first.disabled).toBe(true);
    expect(first.getAttribute("aria-busy")).toBe("true");
    expect(first.getAttribute("aria-label")).toBe(EN["speech.loading"]);
    expect(second.getAttribute("aria-label")).toBe(EN["speech.unavailable"]);

    state.speechPending = null;
    view.updateSpeechButtons();

    expect(first.disabled).toBe(false);
    expect(first.getAttribute("aria-label")).toBe("Play reply 1");
  });

  test("a pending reply from an abandoned conversation is not shown as loading", () => {
    state.speechPending = { generation: state.generation - 1, messageId: "a1", token: 1 };

    expect(view.speechIsPending("a1")).toBe(false);
  });
});

describe("language", () => {
  test("ui() uses the state's language", () => {
    expect(view.ui("actions.send")).toBe("Send");

    state.language = "es";

    expect(view.ui("actions.send")).toBe(ES["actions.send"]);
  });

  test("the user's role label follows the language", () => {
    state.language = "es";

    expect(view.renderMessage({ role: "user", text: "Hola", citations: [] }, 0).textContent).toContain(ES["ui.you"]);
  });
});
