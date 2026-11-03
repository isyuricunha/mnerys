import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { analyzeCommits } from "@semantic-release/commit-analyzer";
import { generateNotes } from "@semantic-release/release-notes-generator";

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

test("locked conventionalcommits preset renders release notes for the stable writer", async () => {
  const notesPlugin = config.plugins.find(
    (plugin) => Array.isArray(plugin)
      && plugin[0] === "@semantic-release/release-notes-generator",
  )[1];
  const notes = await generateNotes(notesPlugin, {
    commits: [
      {
        hash: "1111111111111111111111111111111111111111",
        message: "feat(gateway): demonstrate versioned features",
      },
      {
        hash: "2222222222222222222222222222222222222222",
        message: "fix(session): demonstrate corrected message persistence",
      },
    ],
    lastRelease: {
      version: "0.1.0",
      gitTag: "v0.1.0",
      gitHead: "0000000000000000000000000000000000000000",
    },
    nextRelease: {
      version: "0.2.0",
      gitTag: "v0.2.0",
      gitHead: "3333333333333333333333333333333333333333",
    },
    options: {
      repositoryUrl: "https://github.com/example/mnerys.git",
    },
    logger: { log() {} },
  });

  assert.match(notes, /### Features/);
  assert.match(notes, /### Bug Fixes/);
  assert.match(notes, /demonstrate versioned features/);
  assert.match(notes, /demonstrate corrected message persistence/);
});

for (const [message, expected] of scenarios) {
  test("Conventional Commit: " + message.split("\n")[0], async () => {
    const actual = await analyzeCommits(
      analyzer,
      { commits: [{ message, hash: "0123456789abcdef" }], logger: { log() {}, error() {} } },
    );
    assert.equal(actual, expected);
  });
}
