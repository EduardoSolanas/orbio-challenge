/** A confirm request frozen as exact bytes, so a retry resends the identical command. */
export class ConfirmationCommand {
  constructor(body) {
    this.body = body;
  }

  static from(payload) {
    return new ConfirmationCommand(JSON.stringify(payload));
  }
}
