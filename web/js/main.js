import { App } from "./app.js";

new App({ window })
  .start()
  .catch(() => {
    document.body.textContent = "Northstar support could not load. Please reload the page.";
  });
