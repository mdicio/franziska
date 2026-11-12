/**
 * One /think turn on a cloud model through the pi SDK: the prompt arrives on stdin, the reply goes to stdout.
 *
 *   echo "plan my week" | node pi/think.ts
 *   FRANZISKA_PI_MODEL=ollama/gemma4:12b node pi/think.ts < prompt.txt   # same plumbing, local model
 *
 * The model's only tools are the `life` MCP server (schedule + notes, franziska/mcp_server.py): no shell, no files,
 * no finance. Credentials come from pi's own store (~/.pi/agent/auth.json), written by `pi` → `/login openai`
 * (Sign in with ChatGPT). Nothing is persisted: in-memory session, no user mcp.json, skills or context files.
 */
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import {
	createAgentSession,
	createMcpExtension,
	DefaultResourceLoader,
	getAgentDir,
	ModelRuntime,
	SessionManager,
	SettingsManager,
} from "@earendil-works/pi-coding-agent";

const here = dirname(fileURLToPath(import.meta.url));
const projectDir = dirname(here);
const agentDir = getAgentDir();

const SYSTEM = `You are the user's personal assistant, consulted for things that need more thought than a quick
reply: planning a week, untangling conflicts, drafting a message, explaining something.
Their schedule and saved notes are reachable through the mcp__life__* tools. Call mcp__life__now before resolving
relative dates, mcp__life__agenda before saying what is planned, and mcp__life__recall before stating a saved fact.
Only add, move or delete items when the user asks for it. You cannot see their finances or email.
If a tool call fails, say so plainly: never tell the user something was saved, moved or deleted unless the tool
result confirms it.
Reply in the user's language, briefly, formatted for a phone chat (plain text, short lines, no tables).`;

const ref = process.env.FRANZISKA_PI_MODEL ?? "openai/gpt-5.5";
const slash = ref.indexOf("/");
const [provider, modelId] = [ref.slice(0, slash), ref.slice(slash + 1)];

const prompt = (await new Response(process.stdin as unknown as ReadableStream).text()).trim();
if (!prompt) {
	console.error("think.ts: empty prompt on stdin");
	process.exit(2);
}

// Built-in providers plus pi/models.json (local Ollama, for testing the plumbing without the cloud).
const modelRuntime = await ModelRuntime.create({
	authPath: join(agentDir, "auth.json"),
	modelsPath: join(here, "models.json"),
});
const model = modelRuntime.getModel(provider, modelId);
if (!model) {
	console.error(`think.ts: unknown model ${ref}`);
	process.exit(2);
}

const settingsManager = SettingsManager.inMemory({ compaction: { enabled: false }, retry: { enabled: true, maxRetries: 2 } });
const resourceLoader = new DefaultResourceLoader({
	cwd: projectDir,
	agentDir,
	settingsManager,
	noSkills: true,
	noPromptTemplates: true,
	noThemes: true,
	noContextFiles: true,
	systemPromptOverride: () => SYSTEM,
	appendSystemPromptOverride: () => [],
	extensionFactories: [
		// Only the server registered below: the user's own ~/.pi/agent/mcp.json is not read.
		createMcpExtension({ loadConfig: () => ({ servers: [], errors: [], autoEnableCodemode: false }) }),
		{
			name: "life",
			hidden: true,
			factory: (pi) => {
				pi.registerMcpServer("life", {
					command: "uv",
					args: ["run", "--quiet", "--directory", projectDir, "python", "-m", "franziska.mcp_server"],
					exposure: "direct",
					description: "The user's schedule (appointments, tasks, reminders) and saved notes",
				});
			},
		},
	],
});
await resourceLoader.reload();

const { session } = await createAgentSession({
	cwd: projectDir,
	agentDir,
	model,
	thinkingLevel: (process.env.FRANZISKA_PI_THINKING as "off" | "low" | "medium" | "high") ?? "low",
	modelRuntime,
	resourceLoader,
	settingsManager,
	sessionManager: SessionManager.inMemory(projectDir),
	tools: ["mcp__life__*"],
});

try {
	await session.bindExtensions({}); // session_start: connects the MCP server
	await session.prompt(prompt);
	if (process.env.FRANZISKA_PI_DEBUG) console.error(JSON.stringify(session.messages));
	let text = session.getLastAssistantText()?.trim();
	if (!text) {
		// Seen with a local stand-in model: the tools ran, then the turn ended with reasoning only. Ask once.
		await session.prompt("Now reply to the user, briefly, with what you did or found.");
		text = session.getLastAssistantText()?.trim();
	}
	if (!text) {
		console.error("think.ts: the model returned no text");
		process.exit(1);
	}
	process.stdout.write(text + "\n");
} finally {
	session.dispose();
}
// The MCP child and pooled HTTP connections would keep the event loop alive; the turn is over.
process.exit(0);
