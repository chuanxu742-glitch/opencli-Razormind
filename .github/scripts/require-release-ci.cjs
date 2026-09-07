#!/usr/bin/env node
"use strict";

const CI_WORKFLOW = "ci.yml";
const CI_GATE_NAME = "CI Gate";

function fail(message) {
  throw new Error(`${message} Complete the main CI run for this commit, then rerun the release.`);
}

function nextPage(link) {
  if (!link) return null;
  const match = link.match(/<([^>]+)>;\s*rel="next"/);
  return match?.[1] ?? null;
}

async function getAllPages(url, headers, fetchImpl) {
  const items = [];
  let page = url;
  while (page) {
    let response;
    try {
      response = await fetchImpl(page, { headers });
    } catch {
      fail("GitHub Actions API request failed.");
    }
    if (!response.ok) {
      fail(`GitHub Actions API request failed (${response.status}).`);
    }
    const body = await response.json();
    if (!Array.isArray(body.workflow_runs) && !Array.isArray(body.jobs)) {
      fail("GitHub Actions API returned an unexpected response.");
    }
    items.push(...(body.workflow_runs ?? body.jobs));
    page = nextPage(response.headers.get("link"));
  }
  return items;
}

function latestRunForCommit(runs, sha) {
  const matching = runs.filter(
    (run) => run.event === "push" && run.head_branch === "main" && run.head_sha === sha,
  );
  if (matching.length === 0) {
    fail(`No main push CI run exists for commit ${sha}.`);
  }
  return matching.reduce((latest, run) => {
    const latestTime = Date.parse(latest.created_at ?? 0);
    const runTime = Date.parse(run.created_at ?? 0);
    if (runTime > latestTime) return run;
    if (runTime < latestTime) return latest;
    return Number(run.id) > Number(latest.id) ? run : latest;
  });
}

async function requireReleaseCi({ repository, token, sha, apiUrl = "https://api.github.com", fetchImpl = fetch }) {
  if (!repository || !token || !sha) {
    fail("GITHUB_REPOSITORY, GITHUB_TOKEN, and RELEASE_COMMIT_SHA are required.");
  }

  const headers = {
    Accept: "application/vnd.github+json",
    Authorization: `Bearer ${token}`,
    "X-GitHub-Api-Version": "2022-11-28",
  };
  const baseUrl = `${apiUrl.replace(/\/$/, "")}/repos/${repository}/actions`;
  const runs = await getAllPages(
    `${baseUrl}/workflows/${CI_WORKFLOW}/runs?event=push&branch=main&head_sha=${encodeURIComponent(sha)}&per_page=100`,
    headers,
    fetchImpl,
  );
  const run = latestRunForCommit(runs, sha);
  if (run.status !== "completed" || run.conclusion !== "success") {
    fail(`Latest main CI run ${run.id} for commit ${sha} is ${run.status}/${run.conclusion}.`);
  }

  if (!Number.isSafeInteger(run.run_attempt) || run.run_attempt < 1) {
    fail(`Latest main CI run ${run.id} has no valid run attempt.`);
  }
  const jobs = await getAllPages(
    `${baseUrl}/runs/${run.id}/attempts/${run.run_attempt}/jobs?per_page=100`,
    headers,
    fetchImpl,
  );
  const gate = jobs.find((job) => job.name === CI_GATE_NAME);
  if (!gate || gate.status !== "completed" || gate.conclusion !== "success") {
    fail(`CI run ${run.id} lacks a successful ${CI_GATE_NAME} job.`);
  }
}

async function main() {
  await requireReleaseCi({
    repository: process.env.GITHUB_REPOSITORY,
    token: process.env.GITHUB_TOKEN,
    sha: process.env.RELEASE_COMMIT_SHA,
    apiUrl: process.env.GITHUB_API_URL,
  });
  console.log(`Verified successful main CI Gate for ${process.env.RELEASE_COMMIT_SHA}.`);
}

module.exports = { getAllPages, latestRunForCommit, requireReleaseCi };

if (require.main === module) {
  main().catch((error) => {
    console.error(error.message);
    process.exitCode = 1;
  });
}
