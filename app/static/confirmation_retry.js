function createConfirmationCommand(payload) {
  return { body: JSON.stringify(payload) };
}

if (typeof window !== "undefined") {
  window.NorthstarConfirmationRetry = { createConfirmationCommand };
}

if (typeof module !== "undefined") {
  module.exports = { createConfirmationCommand };
}
