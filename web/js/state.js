/** Everything the page knows about the conversation and what it is waiting for. */
export class ChatState {
  constructor() {
    this.sessionId = null;
    this.revision = 0;
    this.status = "collecting";
    this.language = "en";
    this.summary = null;
    this.pendingReviewRevision = null;
    this.pendingClarifications = {};
    this.messages = [];
    this.pendingTurnMessage = null;
    this.turnPending = false;
    this.inputMode = "text";
    /** Bumped when a conversation is abandoned, so late responses can be ignored. */
    this.generation = 0;
    this.playbackToken = 0;
    this.objectUrl = null;
    this.playingMessageId = null;
    this.pending = false;
    this.sttPending = false;
    this.sttAbort = null;
    this.createPending = false;
    this.resumePending = false;
    this.resumeFailed = false;
    /** "resume" | "turn" | "confirmation" | "create" | null */
    this.retryKind = null;
    this.retryPayload = null;
    this.charLimitAlert = false;
    this.audioBlob = null;
    this.recording = null;
    this.recordingPending = false;
    this.speechPending = null;
    this.orderVerificationRequired = false;
    this.verificationPending = false;
    this.slotOptions = [];
  }

  issuesPresent() {
    return Object.keys(this.pendingClarifications || {}).length > 0;
  }

  canConfirm() {
    return (
      this.status === "awaiting_confirmation" &&
      this.pendingReviewRevision != null &&
      !this.issuesPresent()
    );
  }

  /** The conversation accepts no more input. */
  isClosed() {
    return this.status === "completed" || this.status === "handed_off";
  }

  /** Something is in flight that should lock the composer and its controls. */
  isBusy() {
    return (
      this.pending ||
      this.createPending ||
      this.sttPending ||
      this.recordingPending ||
      !!this.recording ||
      this.resumePending ||
      this.resumeFailed ||
      this.verificationPending
    );
  }

  bumpPlayback() {
    this.playbackToken += 1;
    return this.playbackToken;
  }
}
