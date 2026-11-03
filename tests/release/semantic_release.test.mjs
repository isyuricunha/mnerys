import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { analyzeCommits } from "@semantic-release/commit-analyzer";

const config = JSON.parse(readFileSync(new URL("../../.releaserc.json", import.meta.url), "utf8"));
const analyzer = config.plugins.find((plugin) => Array.isArray(plugin) && plugin[0] === "@semantic-release/commit-analyzer")[1];

test("only main publishes and npm publication is disabled", () => {
  assert.deepEqual(config.branches, ["main"]);
  assert.equal(config.tagFormat, "v${version}");
  assert.ok(!config.plugins.some((plugin) => {
    const name = Array.isArray(plugin) ? plugin[0] : plugin;
    return name === "@semantic-release/npm" || name === "@semantic-release/git";
  }));
});

const scenarios = [
  ["feat: add an agent adapter", "minor"],
  ["fix: preserve a chat session", "patch"],
  ["perf: reduce token overhead", "patch"],
  ["refactor: extract turn orchestration", "patch"],
  ["security: harden tool execution", "patch"],
  ["feat!: change the v0 API", "minor"],
  ["fix: change behavior\n\nBREAKING CHANGE: incompatible response", "minor"],
  ["docs: update the README", null],
  ["ci: improve the release workflow", null],
  ["chore: update dependencies", null],
  ["test: add contract checks", null]
];

for (const [message, expected] of scenarios) {
  test("Conventional Commit: " + message.split("\n")[0], async () => {
    const actual = await analyzeCommits(
      analyzer,
      { commits: [{ message, hash: "0123456789abcdef" }], logger: { log() {}, error() {} } },
    );
    assert.equal(actual, expected);
  });
}
