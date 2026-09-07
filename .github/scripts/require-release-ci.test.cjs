"use strict";

const assert = require("node:assert/strict");
const test = require("node:test");
const { requireReleaseCi } = require("./require-release-ci.cjs");

function response(body, { status = 200, link = null } = {}) {
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (name) => (name.toLowerCase() === "link" ? link : null) },
    json: async () => body,
  };
}

function fakeFetch(routes) {
  return async (url) => {
    const route = routes.get(url);
    assert.ok(route, `unexpected request: ${url}`);
    return route;
  };
}

function gate(id, status = "completed", conclusion = "success") {
  return { id, name: "CI Gate", status, conclusion };
}

const options = {
  repository: "owner/repo",
  token: "token",
  sha: "a".repeat(40),
  apiUrl: "https://api.example.test",
};

function run(id, overrides = {}) {
  return {
    id,
    event: "push",
    head_branch: "main",
    head_sha: options.sha,
    status: "completed",
    conclusion: "success",
    created_at: "2026-09-08T00:00:00Z",
    run_attempt: 1,
    ...overrides,
  };
}

function routesFor(runs, jobs, { link = null } = {}) {
  const base = `${options.apiUrl}/repos/${options.repository}/actions`;
  return new Map([
    [`${base}/workflows/ci.yml/runs?event=push&branch=main&head_sha=${options.sha}&per_page=100`, response({ workflow_runs: runs }, { link })],
    [`${base}/runs/${jobs.id}/attempts/${jobs.attempt ?? 1}/jobs?per_page=100`, response({ jobs: jobs.items })],
  ]);
}

test("uses the selected run's exact attempt for a successful CI Gate", async () => {
  const jobs = { id: 42, attempt: 3, items: [gate(1), gate(2, "completed", "success")] };
  await requireReleaseCi({
    ...options,
    fetchImpl: fakeFetch(routesFor([run(42, { run_attempt: 3 })], jobs)),
  });
});

test("pages exact-SHA runs and rejects a newer same-second failure", async () => {
  const base = `${options.apiUrl}/repos/${options.repository}/actions`;
  const first = `${base}/workflows/ci.yml/runs?event=push&branch=main&head_sha=${options.sha}&per_page=100`;
  const second = `${base}/workflows/ci.yml/runs?page=2`;
  const routes = new Map([
    [first, response({ workflow_runs: [run(10)] }, { link: `<${second}>; rel="next"` })],
    [second, response({ workflow_runs: [run(11, { conclusion: "failure" })] })],
  ]);
  await assert.rejects(
    requireReleaseCi({ ...options, fetchImpl: fakeFetch(routes) }),
    /Latest main CI run 11.*completed\/failure/,
  );
});

test("fails closed for absent or unsuccessful CI Gate", async () => {
  for (const jobs of [[], [gate(1, "completed", "failure")]]) {
    const data = { id: 42, items: jobs };
    await assert.rejects(
      requireReleaseCi({ ...options, fetchImpl: fakeFetch(routesFor([run(42)], data)) }),
      /lacks a successful CI Gate/,
    );
  }
});

test("fails closed for wrong branch, SHA, API errors, and in-progress runs", async () => {
  for (const candidate of [
    run(1, { head_branch: "develop" }),
    run(1, { head_sha: "b".repeat(40) }),
    run(1, { status: "in_progress", conclusion: null }),
    run(1, { run_attempt: 0 }),
  ]) {
    const data = { id: candidate.id, items: [gate(1)] };
    await assert.rejects(
      requireReleaseCi({ ...options, fetchImpl: fakeFetch(routesFor([candidate], data)) }),
      /Complete the main CI run/,
    );
  }

  const base = `${options.apiUrl}/repos/${options.repository}/actions`;
  const failedRoutes = new Map([
    [`${base}/workflows/ci.yml/runs?event=push&branch=main&head_sha=${options.sha}&per_page=100`, response({}, { status: 503 })],
  ]);
  await assert.rejects(
    requireReleaseCi({ ...options, fetchImpl: fakeFetch(failedRoutes) }),
    /API request failed \(503\)/,
  );
  await assert.rejects(
    requireReleaseCi({ ...options, fetchImpl: async () => { throw new Error("network down"); } }),
    /API request failed\./,
  );
});
