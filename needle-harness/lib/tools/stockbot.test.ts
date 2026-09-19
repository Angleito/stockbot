import { afterAll, describe, expect, test } from "bun:test";
import { existsSync } from "node:fs";
import { closeBridge, endSession, invoke, newSessionId } from "./stockbot";

const ROOT = process.cwd().endsWith("needle-harness")
  ? process.cwd().replace(/\/needle-harness$/, "")
  : process.cwd();
const HAS_BRIDGE = existsSync(`${ROOT}/venv/bin/python`) && existsSync(`${ROOT}/scripts/tool_bridge.py`);

describe.skipIf(!HAS_BRIDGE)("stockbot bridge", () => {
  const sid = newSessionId();

  // Test-only cleanup; harness runtime never calls closeBridge.
  afterAll(async () => {
    await endSession(sid);
    closeBridge();
  });

  test(
    "invalid accession fails closed with accession error",
    async () => {
      const result = await invoke("get_sec_document", { accession_no: "bad" }, sid);
      expect(result.ok).toBe(false);
      if (!result.ok) expect(result.error).toMatch(/accession/i);
    },
    60_000,
  );
});
