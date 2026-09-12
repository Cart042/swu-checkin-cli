# main 分支保护（Ruleset）

仓库当前没有配置 Ruleset，`main` 可以被直接 push 或 force push。这个项目已经
是 public、有 fork、并且按 PR + CI 的方式迭代，因此建议在 GitHub 侧把
下面这份规则**应用一次**。

## 建议的规则

| 规则 | 值 | 原因 |
| --- | --- | --- |
| 目标分支 | `refs/heads/main` | 只保护发布分支。 |
| 禁止删除（`deletion`） | 启用 | 防止误删主分支。 |
| 禁止 force push（`non_fast_forward`） | 启用 | 防止历史被覆盖。 |
| 必须走 PR（`pull_request`） | 启用，`required_approving_review_count: 0` | 强制走 PR 流程，但不要求人工 review：主要维护者是自己，要求评审只会增加阻力。 |
| 必须通过的检查（`required_status_checks`） | `CI required checks` | 该检查由 PR CI 的 gate job 汇总 lint / type-check / unit-tests / runtime-checks 四个 job，改名不会让必需检查失效。 |
| 绕过名单（`bypass_actors`） | 空 | 定时任务和发布都只读取代码，不需要绕过保护。 |

## 应用方式

### 方式一：GitHub API（推荐，可复现）

```bash
gh api \
  --method POST \
  -H "Accept: application/vnd.github+json" \
  /repos/Cart042/swu-checkin-cli/rulesets \
  --input - <<'JSON'
{
  "name": "protect-main",
  "target": "branch",
  "enforcement": "active",
  "conditions": {
    "ref_name": {
      "include": ["refs/heads/main"],
      "exclude": []
    }
  },
  "rules": [
    { "type": "deletion" },
    { "type": "non_fast_forward" },
    {
      "type": "pull_request",
      "parameters": {
        "required_approving_review_count": 0,
        "dismiss_stale_reviews_on_push": false,
        "require_code_owner_review": false,
        "require_last_push_approval": false,
        "required_review_thread_resolution": false,
        "allowed_merge_methods": ["merge", "squash", "rebase"]
      }
    },
    {
      "type": "required_status_checks",
      "parameters": {
        "strict_required_status_checks_policy": true,
        "do_not_enforce_on_create": false,
        "required_status_checks": [
          { "context": "CI required checks" }
        ]
      }
    }
  ]
}
JSON
```

需要仓库管理员权限（`Administration: write`）或对应 fine-grained token。

### 方式二：网页

`Settings → Rules → Rulesets → New branch ruleset`，按上表逐项勾选即可；
“Require status checks to pass”里选择 `CI required checks`。

## 与自托管 runner 的关系

PR CI 只使用 GitHub 托管的 `ubuntu-latest`。如果以后需要自托管 runner 跑真实
签到，请放在**私有**运行仓库里，只让受信任的定时工作流使用，不要把它接到
public 仓库的 `pull_request` 事件上——fork 可以修改被检出的代码。

## 验证

应用后可以用下面的命令确认规则生效：

```bash
gh api /repos/Cart042/swu-checkin-cli/rulesets --jq '.[].name'
gh api /repos/Cart042/swu-checkin-cli/rules/branches/main --jq '.[].type'
```

预期看到 `protect-main` 以及 `deletion` / `non_fast_forward` / `pull_request` /
`required_status_checks`。
