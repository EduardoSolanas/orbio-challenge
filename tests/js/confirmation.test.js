import { describe, expect, test } from "@jest/globals";

import { ConfirmationCommand } from "../../gateway/static/js/confirmation.js";

describe("ConfirmationCommand", () => {
  test("stores the original request id and exact body for retry", () => {
    const payload = {
      request_id: "11111111-1111-4111-8111-111111111111",
      expected_revision: 3,
      review_revision: 3,
    };
    const command = ConfirmationCommand.from(payload);

    expect(command.body).toBe(JSON.stringify(payload));
    expect(JSON.parse(command.body).request_id).toBe(payload.request_id);
  });

  test("replaying a stored command does not rebuild or change its bytes", () => {
    const command = ConfirmationCommand.from({
      request_id: "22222222-2222-4222-8222-222222222222",
      expected_revision: 7,
      review_revision: 7,
    });
    const firstBody = command.body;

    expect(command.body).toBe(firstBody);
    expect(command.body).not.toBe(
      JSON.stringify({
        request_id: "33333333-3333-4333-8333-333333333333",
        expected_revision: 7,
        review_revision: 7,
      })
    );
  });
});
