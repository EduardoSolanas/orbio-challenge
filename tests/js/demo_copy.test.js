import { afterEach, describe, expect, jest, test } from "@jest/globals";

import { EN, ES, buildApp, flush } from "./support/page.js";

afterEach(() => {
  delete window.navigator.clipboard;
  delete document.execCommand;
});

function setup() {
  const app = buildApp();
  app.i18n.applyStaticLabels("en");
  app.bindEvents();
  return app;
}

describe("copying test customer details", () => {
  test.each([
    ["account", "NS-000123"],
    ["postcode", "SW1A 1AA"],
    ["code", "246810"],
  ])("copies only the %s value", async (field, value) => {
    const writeText = jest.fn().mockResolvedValue(undefined);
    Object.defineProperty(window.navigator, "clipboard", { value: { writeText }, configurable: true });
    setup();
    const button = document.querySelector(`[data-copy="demo-${field}"]`);

    expect(button.getAttribute("aria-label")).toBe(EN[`demo.copy_${field}`]);
    button.click();
    await flush();

    expect(writeText).toHaveBeenCalledWith(value);
    expect(document.getElementById("demo-copy-status").textContent).toBe(EN["demo.copied"].replace("{value}", value));
    expect(document.getElementById("demo-copy-status").classList.contains("u-sr-only")).toBe(true);
  });

  test("reports a denied clipboard write without claiming success", async () => {
    Object.defineProperty(window.navigator, "clipboard", {
      value: { writeText: jest.fn().mockRejectedValue(new Error("Denied")) }, configurable: true,
    });
    setup();
    document.querySelector("[data-copy]").click();
    await flush();

    expect(document.getElementById("demo-copy-status").textContent).toBe(EN["demo.copy_failed"]);
    expect(document.getElementById("demo-copy-status").classList.contains("u-sr-only")).toBe(false);
  });

  test("copies on HTTP pages without the Clipboard API and restores focus", async () => {
    const app = setup();
    document.execCommand = jest.fn(() => true);
    app.dom.message.focus();
    document.querySelector("[data-copy]").click();
    await flush();

    expect(document.execCommand).toHaveBeenCalledWith("copy");
    expect(document.activeElement).toBe(app.dom.message);
    expect(document.querySelector("textarea[readonly]")).toBeNull();
    expect(document.getElementById("demo-copy-status").textContent).toContain("NS-000123");
  });

  test("reports an unavailable fallback", async () => {
    setup();
    document.querySelector("[data-copy]").click();
    await flush();

    expect(document.getElementById("demo-copy-status").textContent).toBe(EN["demo.copy_failed"]);
  });

  test("uses Spanish labels and confirmation", async () => {
    Object.defineProperty(window.navigator, "clipboard", {
      value: { writeText: jest.fn().mockResolvedValue(undefined) }, configurable: true,
    });
    const app = setup();
    app.state.language = "es";
    app.i18n.applyStaticLabels("es");
    const button = document.querySelector('[data-copy="demo-code"]');
    button.click();
    await flush();

    expect(button.getAttribute("aria-label")).toBe(ES["demo.copy_code"]);
    expect(document.getElementById("demo-copy-status").textContent).toBe(ES["demo.copied"].replace("{value}", "246810"));
  });
});
