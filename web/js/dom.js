/** Element name -> id in index.html. The ids are also what the browser tests use. */
export const ELEMENT_IDS = Object.freeze({
  log: "transcript",
  review: "review",
  slotPicker: "slot-picker",
  starters: "starters",
  alert: "alert",
  verification: "order-verification",
  verificationNotice: "verification-notice",
  verificationError: "verification-error",
  verificationStatus: "verification-status",
  postcode: "delivery-postcode",
  code: "verification-code",
  verify: "verify-order",
  message: "message",
  send: "send",
  retry: "retry",
  confirm: "confirm",
  newIntake: "new-intake",
  startRecording: "start-recording",
  stopRecording: "stop-recording",
  wav: "wav-recording",
  wavAttach: "wav-attach",
  audio: "reply-audio",
  help: "transcribe-help",
  chatPanel: "support-chat",
});

/** The page's elements, looked up once. */
export class ChatDom {
  constructor(document) {
    this.document = document;
    for (const [name, id] of Object.entries(ELEMENT_IDS)) {
      this[name] = document.getElementById(id);
    }
  }
}
