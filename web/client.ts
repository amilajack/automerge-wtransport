import * as A from "@automerge/automerge/slim";
import schema from "./schema.json";
import { initializeBase64Wasm } from "@automerge/automerge/slim";
import { automergeWasmBase64 as wasm } from "@automerge/automerge/automerge.wasm.base64";
import { connect, type Kind, type Config, type Transport } from "./transport";
export const initialized = initializeBase64Wasm(wasm);
type TextDoc = { text: string };
type Entry = { doc: A.Doc<TextDoc>; sync: A.SyncState; save: Promise<void> };
export class Client {
  docs = new Map<string, Entry>();
  transport?: Transport;
  status = "disconnected";
  error = "";
  onchange = () => {};
  received: { at: number; id: string; length: number }[] = [];
  onreceive: (id: string, text: string) => void = () => {};
  private db?: IDBDatabase;
  constructor(
    public base = location.origin,
    public token = "",
    public kind: Kind = "wt",
    private persist = true,
  ) {}
  private async database() {
    if (this.db) return this.db;
    this.db = await new Promise<IDBDatabase>((resolve, reject) => {
      const r = indexedDB.open("automerge-wtransport-v1", 1);
      r.onupgradeneeded = () => r.result.createObjectStore("docs");
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
    return this.db;
  }
  private async load(id: string) {
    if (!this.persist) return undefined;
    const db = await this.database();
    return new Promise<Uint8Array | undefined>((resolve, reject) => {
      const r = db.transaction("docs").objectStore("docs").get(id);
      r.onsuccess = () => resolve(r.result);
      r.onerror = () => reject(r.error);
    });
  }
  private save(id: string) {
    const e = this.docs.get(id)!;
    if (!this.persist) return;
    e.save = e.save
      .then(async () => {
        const db = await this.database();
        const b = A.save(e.doc);
        await new Promise<void>((resolve, reject) => {
          const tx = db.transaction("docs", "readwrite");
          tx.objectStore("docs").put(b, id);
          tx.oncomplete = () => resolve();
          tx.onerror = () => reject(tx.error);
          tx.onabort = () => reject(tx.error);
        });
      })
      .catch((e) => {
        this.error = `Local persistence failed: ${e}`;
        this.onchange();
        throw e;
      });
  }
  async open(id: string) {
    await initialized;
    if (this.docs.has(id)) return;
    const bytes = await this.load(id);
    // Loading without supplying an actor intentionally allocates a fresh actor per browser instance.
    const doc = bytes
      ? A.load<TextDoc>(bytes)
      : A.load<TextDoc>(Uint8Array.from(atob(schema), (c) => c.charCodeAt(0)));
    this.docs.set(id, {
      doc,
      sync: A.initSyncState(),
      save: Promise.resolve(),
    });
    if (this.transport) {
      await this.transport.open(id);
      this.pump(id);
    }
    this.onchange();
  }
  async connect() {
    await initialized;
    this.disconnect();
    this.status = "connecting";
    this.onchange();
    try {
      const config: Config = await fetch(new URL("/config", this.base)).then(
        (r) => {
          if (!r.ok) throw Error("Config failed");
          return r.json();
        },
      );
      this.transport = await connect(
        this.kind,
        this.base,
        this.token,
        config,
        (id, p) => {
          const e = this.docs.get(id);
          if (!e) throw Error("Unopened document");
          [e.doc, e.sync] = A.receiveSyncMessage(e.doc, e.sync, p);
          this.save(id);
          this.received.push({
            at: performance.timeOrigin + performance.now(),
            id,
            length: (e.doc.text ?? "").length,
          });
          if (this.received.length > 10000) this.received.shift();
          this.onreceive(id, e.doc.text ?? "");
          this.pump(id);
          this.onchange();
        },
        (e) => {
          this.error = String(e);
          this.disconnect();
          this.onchange();
        },
      );
      for (const [id, e] of this.docs) {
        e.sync = A.initSyncState();
        await this.transport.open(id);
        this.pump(id);
      }
      this.status = "connected";
      this.onchange();
    } catch (e) {
      this.error = String(e);
      this.disconnect();
      throw e;
    }
  }
  disconnect() {
    this.transport?.close();
    this.transport = undefined;
    this.status = "disconnected";
    for (const e of this.docs.values()) e.sync = A.initSyncState();
    this.onchange();
  }
  private pump(id: string) {
    const e = this.docs.get(id)!;
    if (!this.transport) return;
    const [state, msg] = A.generateSyncMessage(e.doc, e.sync);
    e.sync = state;
    if (msg)
      void this.transport.send(id, msg).catch((e) => {
        this.error = String(e);
        this.disconnect();
      });
  }
  setText(id: string, text: string) {
    const e = this.docs.get(id)!;
    e.doc = A.change(e.doc, (d) => {
      if (typeof d.text !== "string") d.text = "";
      A.updateText(d, ["text"], text);
    });
    this.save(id);
    this.pump(id);
    this.onchange();
  }
  append(id: string, text: string) {
    const e = this.docs.get(id)!;
    e.doc = A.change(e.doc, (d) => {
      if (typeof d.text !== "string") d.text = "";
      A.splice(d, ["text"], d.text.length, 0, text);
    });
    this.save(id);
    this.pump(id);
    this.onchange();
  }
  text(id: string) {
    return this.docs.get(id)?.doc.text ?? "";
  }
  actor(id: string) {
    return A.getActorId(this.docs.get(id)!.doc);
  }
  heads(id: string) {
    return A.getHeads(this.docs.get(id)!.doc).sort();
  }
  acknowledged(id: string) {
    const e = this.docs.get(id)!;
    return !!this.transport && A.hasOurChanges(e.doc, e.sync);
  }
  async flush() {
    await Promise.all([...this.docs.values()].map((e) => e.save));
  }
}
