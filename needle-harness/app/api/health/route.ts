import { existsSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

export async function GET(): Promise<Response> {
  const needleWeights = process.env.NEEDLE_WEIGHTS ?? null;
  const weightsPresent =
    needleWeights !== null ? existsSync(needleWeights) : existsSync(join(process.cwd(), "..", "needle3.cact"));
  return Response.json({
    ok: true,
    port: process.env.PORT ?? "3000",
    hasOpencodeKey: Boolean(process.env.OPENCODE_API_KEY),
    needleWeights,
    weightsPresent,
    venvPresent: existsSync(`${homedir()}/.cache/needle-harness/.needle/bin/python`),
  });
}
