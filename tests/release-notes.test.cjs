const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');

const workflow = readFileSync(
  path.join(__dirname, '../.github/workflows/release-create.yml'),
  'utf8',
);
const AsyncFunction = Object.getPrototypeOf(async function () {}).constructor;
function scriptForStep(name) {
  const match = workflow.match(new RegExp(
    `      - name: ${name}\\n[\\s\\S]*?          script: \\|\\n((?:            [^\\n]*(?:\\n|$)|\\n)*)`,
  ));
  assert.ok(match, `${name} must include an inline script`);
  return new AsyncFunction(
    'github', 'context', 'process', match[1].replace(/^            /gm, ''),
  );
}
const getReleaseNotes = scriptForStep('Get source release notes');
const copyReleaseNotes = scriptForStep('Copy release notes to GitOps pull request');

const context = { repo: { owner: 'source-org', repo: 'source-service' } };
const env = {
  TAG: 'v1.2.3',
  GITOPS_REPO: 'deploy-org/gitops',
  GITOPS_PR_BRANCH: 'release-source-service-v1.2.3',
};
const releaseUrl = 'https://github.com/source-org/source-service/releases/tag/v1.2.3';
const startMarker = '<!-- source-release-notes:start -->';
const endMarker = '<!-- source-release-notes:end -->';

function fixture({
  notes = '### Changes\n\n- Fix streaming\n- Improve deployments',
  body = 'Automated image update.\n\n| Image | Version |\n| app | v1.2.3 |',
  pullRequests,
  releaseError,
  listError,
  updateError,
} = {}) {
  const calls = { releases: [], lists: [], updates: [] };
  const github = {
    rest: {
      repos: {
        async getReleaseByTag(options) {
          calls.releases.push(options);
          if (releaseError) throw releaseError;
          return { data: { body: notes, html_url: releaseUrl } };
        },
      },
      pulls: {
        async list(options) {
          calls.lists.push(options);
          if (listError) throw listError;
          return { data: pullRequests ?? [{ number: 42, body }] };
        },
        async update(options) {
          calls.updates.push(options);
          if (updateError) throw updateError;
        },
      },
    },
  };
  return {
    calls,
    run: async () => {
      const release = await getReleaseNotes(github, context, { env });
      await copyReleaseNotes(github, context, {
        env: { ...env, SOURCE_RELEASE: JSON.stringify(release) },
      });
    },
  };
}

test('uses the source token for release lookup and the GitOps token for PR updates', () => {
  assert.match(workflow, /id: release-notes\n        uses: actions\/github-script@v7\n        with:\n          github-token: \$\{\{ github\.token \}\}/);
  assert.match(workflow, /SOURCE_RELEASE: \$\{\{ steps\.release-notes\.outputs\.result \}\}\n        with:\n          github-token: \$\{\{ secrets\.TOKEN \}\}/);
  assert.ok(workflow.indexOf('name: Get source release notes')
    < workflow.indexOf('name: Create GitOps Pull Request'));
  assert.ok(workflow.indexOf('name: Create GitOps Pull Request')
    < workflow.indexOf('name: Copy release notes to GitOps pull request'));
});

test('copies Markdown notes and a source link while preserving the original description', async () => {
  const { calls, run } = fixture();
  await run();

  assert.deepEqual(calls.releases, [{
    owner: 'source-org',
    repo: 'source-service',
    tag: 'v1.2.3',
  }]);
  assert.deepEqual(calls.lists, [{
    owner: 'deploy-org',
    repo: 'gitops',
    head: 'deploy-org:deployment/release-source-service-v1.2.3',
    base: 'main',
    state: 'open',
    per_page: 1,
  }]);
  assert.equal(calls.updates.length, 1);
  assert.deepEqual(calls.updates[0], {
    owner: 'deploy-org',
    repo: 'gitops',
    pull_number: 42,
    body: [
      'Automated image update.\n\n| Image | Version |\n| app | v1.2.3 |',
      '',
      startMarker,
      '## Release notes',
      '',
      `Source release: ${releaseUrl}`,
      '',
      '### Changes\n\n- Fix streaming\n- Improve deployments',
      endMarker,
    ].join('\n'),
  });
});

test('uses the resolved tag without requiring a release event', async () => {
  const { calls, run } = fixture();
  assert.equal(context.payload, undefined);
  await run();
  assert.equal(calls.releases[0].tag, env.TAG);
  assert.match(workflow, /TAG: \$\{\{ inputs\.tag \|\| github\.event\.release\.tag_name \|\| github\.ref_name \}\}/);
});

test('handles empty pull request descriptions', async () => {
  for (const body of ['', null]) {
    const { calls, run } = fixture({ body });
    await run();
    assert.ok(calls.updates[0].body.startsWith(startMarker));
  }
});

test('explicitly documents releases without notes', async () => {
  for (const notes of ['', null]) {
    const { calls, run } = fixture({ notes });
    await run();
    assert.ok(calls.updates[0].body.includes('_No release notes were provided._'));
    assert.ok(calls.updates[0].body.includes(releaseUrl));
  }
});

test('reruns do not duplicate notes or make unnecessary updates', async () => {
  const first = fixture();
  await first.run();
  const second = fixture({ body: first.calls.updates[0].body });
  await second.run();
  assert.equal(second.calls.updates.length, 0);
});

test('refreshes changed notes without altering surrounding content or replacement characters', async () => {
  const notes = 'Updated notes with literal $&, $`, $\' and $(echo do-not-run).\n```sh\nprintf "hello"\n```';
  const body = `Original description.\n\n${startMarker}\nOld notes\n${endMarker}\n\nReviewer checklist.`;
  const { calls, run } = fixture({ notes, body });
  await run();
  const updated = calls.updates[0].body;
  assert.ok(updated.startsWith('Original description.\n\n'));
  assert.ok(updated.endsWith('\n\nReviewer checklist.'));
  assert.ok(updated.includes(notes));
  assert.ok(!updated.includes('Old notes'));
  assert.equal(updated.split(startMarker).length - 1, 1);
  assert.equal(updated.split(endMarker).length - 1, 1);
});

test('fails explicitly when the GitOps pull request cannot be found', async () => {
  const { calls, run } = fixture({ pullRequests: [] });
  await assert.rejects(run, /No open GitOps pull request found/);
  assert.equal(calls.updates.length, 0);
});

test('propagates source release lookup errors without updating the PR', async () => {
  const error = new Error('Release not found');
  const { calls, run } = fixture({ releaseError: error });
  await assert.rejects(run, error);
  assert.equal(calls.lists.length, 0);
  assert.equal(calls.updates.length, 0);
});

test('propagates GitOps PR lookup errors without updating the PR', async () => {
  const error = new Error('GitOps access denied');
  const { calls, run } = fixture({ listError: error });
  await assert.rejects(run, error);
  assert.equal(calls.updates.length, 0);
});

test('propagates PR update errors', async () => {
  const error = new Error('PR update failed');
  const { run } = fixture({ updateError: error });
  await assert.rejects(run, error);
});
