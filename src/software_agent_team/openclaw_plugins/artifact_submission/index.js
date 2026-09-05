import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";

const PLUGIN_ID = "sat-artifact-submission";
const TOOL_NAME = "sat_submit_artifact";
const PROTOCOL = "sat_artifact_submission_v1";
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const MAX_SCHEMA_BYTES = 512 * 1024;

function unavailableContract() {
  return {
    available: false,
    parameters: {
      type: "object",
      additionalProperties: false,
      properties: {},
      maxProperties: 0,
    },
  };
}

function loadInvocationContract() {
  const schemaPath = process.env.SAT_ARTIFACT_SUBMISSION_SCHEMA_PATH;
  const outputPath = process.env.SAT_ARTIFACT_SUBMISSION_OUTPUT_PATH;
  const schemaSha256 = process.env.SAT_ARTIFACT_SUBMISSION_SCHEMA_SHA256;
  const bindingSha256 = process.env.SAT_ARTIFACT_SUBMISSION_BINDING_SHA256;
  if (!schemaPath || !outputPath || !schemaSha256 || !bindingSha256) {
    return unavailableContract();
  }
  if (
    !path.isAbsolute(schemaPath) ||
    !path.isAbsolute(outputPath) ||
    !SHA256_PATTERN.test(schemaSha256) ||
    !SHA256_PATTERN.test(bindingSha256)
  ) {
    throw new Error("SAT artifact submission environment is invalid");
  }
  const metadata = fs.lstatSync(schemaPath);
  if (!metadata.isFile() || metadata.isSymbolicLink()) {
    throw new Error("SAT artifact submission schema is not a direct file");
  }
  if (metadata.size > MAX_SCHEMA_BYTES) {
    throw new Error("SAT artifact submission schema exceeds its size limit");
  }
  const encoded = fs.readFileSync(schemaPath);
  if (crypto.createHash("sha256").update(encoded).digest("hex") !== schemaSha256) {
    throw new Error("SAT artifact submission schema digest differs");
  }
  const parameters = JSON.parse(encoded.toString("utf8"));
  if (!parameters || typeof parameters !== "object" || Array.isArray(parameters)) {
    throw new Error("SAT artifact submission schema must be an object");
  }
  return {
    available: true,
    parameters,
    outputPath,
    schemaSha256,
    bindingSha256,
  };
}

function writeExclusiveSubmission(contract, toolCallId, payload) {
  const envelope = JSON.stringify({
    protocol: PROTOCOL,
    binding_sha256: contract.bindingSha256,
    schema_sha256: contract.schemaSha256,
    tool_call_id: toolCallId,
    payload,
  });
  const flags =
    fs.constants.O_WRONLY |
    fs.constants.O_CREAT |
    fs.constants.O_EXCL |
    (fs.constants.O_NOFOLLOW || 0);
  const descriptor = fs.openSync(contract.outputPath, flags, 0o600);
  try {
    fs.writeFileSync(descriptor, envelope, { encoding: "utf8" });
    fs.fsyncSync(descriptor);
  } finally {
    fs.closeSync(descriptor);
  }
}

export default {
  id: PLUGIN_ID,
  name: "SAT Artifact Submission",
  description: "Submits one schema-checked semantic artifact to the SAT controller.",
  register(api) {
    const contract = loadInvocationContract();
    api.registerTool({
      name: TOOL_NAME,
      label: "Submit SAT artifact",
      description:
        "Submit the final semantic artifact exactly once. This ends the current Agent invocation.",
      parameters: contract.parameters,
      async execute(toolCallId, params) {
        if (!contract.available) {
          throw new Error("SAT artifact submission is not bound to an invocation");
        }
        writeExclusiveSubmission(contract, toolCallId, params);
        return {
          content: [
            {
              type: "text",
              text: "Semantic artifact accepted by the SAT submission channel.",
            },
          ],
          details: {
            status: "completed",
            submission_status: "accepted",
            protocol: PROTOCOL,
            schema_sha256: contract.schemaSha256,
            binding_sha256: contract.bindingSha256,
          },
          terminate: true,
        };
      },
    });
  },
};
