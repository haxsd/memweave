/**
 * Mount the local MemWeave continuity memory MCP server through the in-box
 * DSH MCP client bridge.
 *
 * `config.json` is generated next to this file by install.ps1 and contains:
 *   { "python": "<absolute python.exe>", "home": "<absolute memory vault>" }
 *
 * DSH owns transport, cancellation, timeouts and tool registration; the
 * Python process only serves the memweave_* tools and persists Markdown under
 * the shared vault. Keep this file free of machine-specific paths so the
 * template stays reusable.
 */

import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

export const name = 'memweave'
export const inject = ['tools']

function loadConfig() {
  const configPath = fileURLToPath(new URL('./config.json', import.meta.url))
  return JSON.parse(readFileSync(configPath, 'utf8'))
}

const config = loadConfig()

const MEMWEAVE_SERVER = {
  serverName: 'memweave',
  transport: 'stdio',
  command: config.python,
  args: ['-m', 'memweave', 'serve', '--home', config.home],
  env: {
    PYTHONUTF8: '1',
    PYTHONUNBUFFERED: '1',
    ...(config.env ?? {})
  },
  toolCallTimeoutMs: config.toolCallTimeoutMs ?? 120000,
  failOnStartupError: config.failOnStartupError ?? false
}

export async function apply(ctx) {
  const bridge = await import('@deepseek-ai/dsh-mcp-client')
  const connection = ctx.plugin(bridge, MEMWEAVE_SERVER)
  await connection
}
