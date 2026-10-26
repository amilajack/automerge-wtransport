import { Client, initialized } from "./client";
import "./style.css";
const $ = <T extends HTMLElement>(id: string) =>
  document.getElementById(id) as T;
const client = new Client();
let selected = "";
const run = (fn: () => Promise<unknown>) =>
  void fn().catch((e) => {
    $("error").textContent = String(e);
  });
client.onchange = () => {
  $("status").textContent = client.status;
  $("error").textContent = client.error;
  const ta = $<HTMLTextAreaElement>("text");
  if (selected) {
    ta.disabled = false;
    if (ta.value !== client.text(selected)) ta.value = client.text(selected);
    $("title").textContent = selected.slice(0, 8);
    $("details").textContent =
      `${client.text(selected).length.toLocaleString()} characters · ${client.acknowledged(selected) ? "server has these changes" : "local changes / syncing"}`;
  }
  $("documents").replaceChildren(
    ...[...client.docs.keys()].map((id) => {
      const b = document.createElement("button");
      b.textContent = id.slice(0, 8);
      b.className = id === selected ? "active" : "secondary";
      b.onclick = () => {
        selected = id;
        client.onchange();
      };
      return b;
    }),
  );
};
async function open(id: string) {
  await client.open(id);
  selected = id;
  location.hash = id;
  client.onchange();
}
$("create").onclick = () => run(() => open(crypto.randomUUID()));
$("open").onclick = () =>
  run(() => open($<HTMLInputElement>("documentId").value.trim()));
$("connect").onclick = () =>
  run(async () => {
    client.token = $<HTMLInputElement>("token").value;
    client.kind = $<HTMLSelectElement>("transport").value as "wt" | "ws";
    await client.connect();
  });
$("disconnect").onclick = () => client.disconnect();
$<HTMLTextAreaElement>("text").oninput = (e) => {
  if (selected)
    client.setText(selected, (e.target as HTMLTextAreaElement).value);
};
$("share").onclick = () =>
  run(async () => {
    if (!selected) return;
    const u = new URL(location.href);
    u.hash = selected;
    await navigator.clipboard.writeText(u.toString());
    $("details").textContent = "Share link copied (token shared separately)";
  });
const params = new URLSearchParams(location.search);
if (params.has("token"))
  $<HTMLInputElement>("token").value = params.get("token")!;
await initialized;
if (location.hash) run(() => open(location.hash.slice(1)));
// Same client implementation is used by the reproducible real-browser harness.
Object.assign(window, { lab: { Client, client, ready: true } });
