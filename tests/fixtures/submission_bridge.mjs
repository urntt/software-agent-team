// Execute the production plugin; simulate only its host registration/session IO.
import fs from "node:fs";
import { pathToFileURL } from "node:url";

const plugin = (await import(pathToFileURL(process.argv[2]).href)).default;
let tool;
plugin.register({ registerTool(value) { tool = value; } });
const inputs = JSON.parse(fs.readFileSync(0, "utf8"));
const records = [];
for (const [index, args] of inputs.entries()) {
  const id = `submission-${index}`;
  records.push({ type: "message", message: {
    role: "assistant",
    content: [{ type: "toolCall", id, name: tool.name, arguments: args }],
  } });
  let result;
  let isError = false;
  try {
    result = await tool.execute(id, args);
    if (result.terminate !== true) throw new Error("terminal flag missing");
  } catch (error) {
    isError = true;
    result = { content: [{ type: "text", text: error.message }], details: {} };
  }
  records.push({ type: "message", message: {
    role: "toolResult", toolCallId: id, toolName: tool.name,
    isError, content: result.content, details: result.details,
  } });
}
process.stdout.write(JSON.stringify({ parameters: tool.parameters, records }));
