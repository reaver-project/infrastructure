const assert = require("assert");
const {
    configure_runner_group,
    parse_arguments,
} = require("./configure-runner-group");


async function test_configuration_paginates_and_updates() {
    const calls = [];
    const first_page = Array.from({length: 100}, (_, id) => ({
        account: {login: `other-${id}`},
        id,
    }));
    const groups = Array.from({length: 100}, (_, id) => ({
        id,
        name: `other-${id}`,
    }));
    const request = async (path, token, method = "GET", body) => {
        calls.push({body, method, path, token});
        switch (path) {
            case "/app/installations?per_page=100&page=1":
                return first_page;
            case "/app/installations?per_page=100&page=2":
                return [{account: {login: "reaver-project"}, id: 1234}];
            case "/app/installations/1234/access_tokens":
                return {token: "installation-token"};
            case "/orgs/reaver-project/actions/runner-groups?per_page=100&page=1":
                return {runner_groups: groups};
            case "/orgs/reaver-project/actions/runner-groups?per_page=100&page=2":
                return {runner_groups: [{id: 77, name: "reaveros"}]};
            case "/orgs/reaver-project/actions/runner-groups/77":
                return {id: 77, ...body};
            case "/repos/reaver-project/reaveros":
                return {id: 99};
            case "/orgs/reaver-project/actions/runner-groups/77/repositories":
                return undefined;
            default:
                throw new Error(`Unexpected request: ${method} ${path}`);
        }
    };
    const options = parse_arguments([
        "--credentials", "-",
        "--repository", "reaver-project/reaveros",
        "--workflow", "reaver-project/reaveros/.github/workflows/aws-runner.yml@refs/heads/main",
    ]);
    const result = await configure_runner_group(
        options,
        {id: 1, pem: "unused"},
        request,
        () => "app-jwt",
    );

    assert.deepStrictEqual(result, {installation_id: 1234, runner_group_id: 77});
    assert(calls.some(call => call.method === "PATCH"
        && call.path === "/orgs/reaver-project/actions/runner-groups/77"));
    assert(calls.some(call => call.method === "PUT"
        && call.body.selected_repository_ids[0] === 99));
}


async function test_configuration_creates_a_missing_group() {
    const calls = [];
    const request = async (path, token, method = "GET", body) => {
        calls.push({body, method, path, token});
        switch (path) {
            case "/app/installations?per_page=100&page=1":
                return [{account: {login: "reaver-project"}, id: 1234}];
            case "/app/installations/1234/access_tokens":
                return {token: "installation-token"};
            case "/orgs/reaver-project/actions/runner-groups?per_page=100&page=1":
                return {runner_groups: []};
            case "/orgs/reaver-project/actions/runner-groups":
                return {id: 77, ...body};
            case "/repos/reaver-project/reaveros":
                return {id: 99};
            case "/orgs/reaver-project/actions/runner-groups/77/repositories":
                return undefined;
            default:
                throw new Error(`Unexpected request: ${method} ${path}`);
        }
    };
    await configure_runner_group(
        {
            group: "reaveros",
            organization: "reaver-project",
            repositories: ["reaver-project/reaveros"],
            workflows: ["reaver-project/reaveros/.github/workflows/aws-runner.yml@refs/heads/main"],
        },
        {id: 1, pem: "unused"},
        request,
        () => "app-jwt",
    );

    assert(calls.some(call => call.method === "POST"
        && call.path === "/orgs/reaver-project/actions/runner-groups"));
}


Promise.all([
    test_configuration_creates_a_missing_group(),
    test_configuration_paginates_and_updates(),
]).catch(error => {
    console.error(error);
    process.exit(1);
});
