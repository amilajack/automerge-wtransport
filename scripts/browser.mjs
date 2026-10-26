import { chromium } from "@playwright/test";
import { readFile, mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
export async function browser({ headless = true, profileDir } = {}) {
  const spki = (
    await readFile(new URL("../certs/spki.txt", import.meta.url), "utf8")
  ).trim();
  const profile =
    profileDir ?? (await mkdtemp(join(tmpdir(), "automerge-wt-chromium-")));
  const context = await chromium.launchPersistentContext(profile, {
    channel: process.env.CHROMIUM_CHANNEL ?? "chrome",
    headless,
    args: [
      `--ignore-certificate-errors-spki-list=${spki}`,
      "--disable-quic-port-selection",
      "--no-sandbox",
    ],
    viewport: { width: 1280, height: 900 },
  });
  return {
    context,
    async close() {
      let timer;
      try {
        await Promise.race([
          context.browser()?.close() ?? context.close(),
          new Promise((_, reject) => {
            timer = setTimeout(
              () => reject(Error("Browser close timed out")),
              10000,
            );
          }),
        ]);
      } finally {
        clearTimeout(timer);
        if (!profileDir) await rm(profile, { recursive: true, force: true });
      }
    },
  };
}
export async function pageClient(
  context,
  {
    base = "https://localhost:4433",
    token = process.env.EXPERIMENT_TOKEN,
    kind = "wt",
    persist = false,
  } = {},
) {
  const page = await context.newPage();
  const evaluate = page.evaluate.bind(page);
  page.evaluate = (...args) => {
    let timer;
    return Promise.race([
      evaluate(...args),
      new Promise((_, reject) => {
        timer = setTimeout(
          () => reject(Error("Browser evaluation timed out")),
          Number(process.env.CLIENT_TIMEOUT_MS ?? 120000),
        );
      }),
    ]).finally(() => clearTimeout(timer));
  };
  page.on("pageerror", (e) => console.error("pageerror:", e.message));
  await page.goto(base, {
    timeout: Number(process.env.CLIENT_TIMEOUT_MS ?? 120000),
  });
  await page.waitForFunction(() => window.lab?.ready);
  await page.evaluate(
    async ({ base, token, kind, persist }) => {
      window.c = new window.lab.Client(base, token, kind, persist);
      const start = performance.now();
      await window.c.connect();
      window.connectReadyMs = performance.now() - start;
    },
    { base, token, kind, persist },
  );
  return page;
}
export async function waitConverged(pages, ids, timeout = 30000) {
  const start = Date.now();
  let state;
  while (Date.now() - start < timeout) {
    state = await Promise.all(
      pages.map((p) =>
        p.evaluate(
          (ids) =>
            ids.map((id) => ({
              text: window.c.text(id),
              heads: window.c.heads(id),
              ack: window.c.acknowledged(id),
            })),
          ids,
        ),
      ),
    );
    if (
      ids.every((_, i) =>
        state.every(
          (x) =>
            x[i].ack &&
            JSON.stringify(x[i].heads) === JSON.stringify(state[0][i].heads) &&
            x[i].text === state[0][i].text,
        ),
      )
    )
      return state;
    await new Promise((r) => setTimeout(r, 50));
  }
  console.error(
    "client states",
    await Promise.all(
      pages.map((p) =>
        p.evaluate(() => ({ status: c.status, error: c.error })),
      ),
    ),
  );
  throw Error(
    `Convergence timeout; ${JSON.stringify(state?.map((s) => s.map((x) => ({ heads: x.heads, length: x.text.length, ack: x.ack }))))}`,
  );
}

// Poll only heads/lengths while large downloads are active. Fetch contents once
// after agreement, so the measurement does not copy MiB over CDP every 50 ms.
export async function waitConvergedCompact(pages, ids, timeout = 90000) {
  const deadline = Date.now()+timeout;
  while (Date.now()<deadline) {
    const state = await Promise.all(pages.map(p=>p.evaluate(ids=>ids.map(id=>({
      heads:c.heads(id), ack:c.acknowledged(id), length:c.text(id).length
    })),ids)));
    if(ids.every((_,i)=>state.every(s=>s[i].ack && s[i].length===state[0][i].length && JSON.stringify(s[i].heads)===JSON.stringify(state[0][i].heads)))) {
      const contents=await Promise.all(pages.map(p=>p.evaluate(ids=>ids.map(id=>c.text(id)),ids)));
      if(ids.every((_,i)=>contents.every(s=>s[i]===contents[0][i]))) return state;
      throw Error("Convergence heads match but contents differ");
    }
    await new Promise(r=>setTimeout(r,100));
  }
  throw Error("Convergence timeout (compact heads/contents check)");
}
