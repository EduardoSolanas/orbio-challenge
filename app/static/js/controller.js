/** Shared collaborators and helpers for the controllers. */
export class Controller {
  constructor({ state, view, presenter, api, i18n }) {
    this.state = state;
    this.view = view;
    this.presenter = presenter;
    this.api = api;
    this.i18n = i18n;
  }

  ui(key, values) {
    return this.i18n.t(this.state.language, key, values);
  }

  /** Alert for a request that failed before any response arrived. */
  alertTransportError(error) {
    this.view.showAlert(this.api.transportMessage(error));
  }
}
