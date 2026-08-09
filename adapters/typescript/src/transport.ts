import { Client } from "@modelcontextprotocol/sdk/client/index.js";
import { StreamableHTTPClientTransport } from "@modelcontextprotocol/sdk/client/streamableHttp.js";

export interface RuntimeToolCaller {
  callTool(name: string, args: Record<string, unknown>): Promise<unknown>;
  close(): Promise<void>;
}

export interface StreamableHttpOptions {
  url: string | URL;
  apiKey?: string;
  headers?: Record<string, string>;
  clientName?: string;
  clientVersion?: string;
}

export class StreamableHttpToolCaller implements RuntimeToolCaller {
  private constructor(
    private readonly client: Client,
    private readonly transport: StreamableHTTPClientTransport
  ) {}

  static async connect(
    options: StreamableHttpOptions
  ): Promise<StreamableHttpToolCaller> {
    const headers = new Headers(options.headers);
    if (options.apiKey) {
      headers.set("Authorization", `Bearer ${options.apiKey}`);
    }
    const client = new Client({
      name: options.clientName ?? "altm-typescript-sdk",
      version: options.clientVersion ?? "1.0.0"
    });
    const transport = new StreamableHTTPClientTransport(
      new URL(options.url),
      { requestInit: { headers } }
    );
    await client.connect(transport);
    return new StreamableHttpToolCaller(client, transport);
  }

  async callTool(
    name: string,
    args: Record<string, unknown>
  ): Promise<unknown> {
    const result = await this.client.callTool({
      name,
      arguments: args
    });
    if ("isError" in result && result.isError) {
      throw new Error(`ALTM MCP tool ${name} returned an error`);
    }
    return result;
  }

  async close(): Promise<void> {
    await this.transport.close();
  }
}
