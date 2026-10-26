import { resolve } from "node:path";
import { browser } from "./browser.mjs";
const b = await browser({
  headless: false,
  profileDir: resolve(process.env.PROFILE_DIR ?? ".chromium-profile"),
});
const page = await b.context.newPage();
await page.goto(process.env.BASE_URL ?? "https://localhost:4433");
console.log("Dedicated persistent Chromium profile; close its window to exit.");
await new Promise((r) => b.context.once("close", r));
await b.close();
