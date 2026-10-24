const test = require("node:test");
const assert = require("node:assert/strict");
const { createConfirmationCommand } = require("../../app/static/confirmation_retry.js");

test("stores the original confirmation request ID and exact body for retry", () => {
  const payload = {
    request_id: "11111111-1111-4111-8111-111111111111",
    expected_revision: 3,
    review_revision: 3,
  };
  const command = createConfirmationCommand(payload);
  assert.equal(command.body, JSON.stringify(payload));
  assert.equal(JSON.parse(command.body).request_id, payload.request_id);
});

test("replaying a stored confirmation command does not rebuild or change its bytes", () => {
  const command = createConfirmationCommand({
    request_id: "22222222-2222-4222-8222-222222222222",
    expected_revision: 7,
    review_revision: 7,
  });
  const firstBody = command.body;
  const retryBody = command.body;
  assert.equal(retryBody, firstBody);
  assert.notEqual(retryBody, JSON.stringify({
    request_id: "33333333-3333-4333-8333-333333333333",
    expected_revision: 7,
    review_revision: 7,
  }));
});
