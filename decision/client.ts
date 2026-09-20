import { TypeSafeClient } from "@typesafe-ai/sdk";

let client: TypeSafeClient | null = null;

export function getTypeSafeClient(): TypeSafeClient {
 if (!client) {
  if (!process.env.TYPESAFE_API_KEY) throw new Error("typesafe_unavailable: missing API key");
  client = new TypeSafeClient();
 }
 return client;
}

export function resetTypeSafeClientForTest(): void {
 client = null;
}
