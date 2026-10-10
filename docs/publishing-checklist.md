# 发布清单

> 这份清单是"推到 GitHub / 装给别人用"之前必须逐项确认的东西。
> 打勾前请真的执行过那条验证，不要凭"应该没问题"打勾。

## 一、阻断项（不满足就不能发布）

| # | 项 | 状态 | 怎么验 |
|---|---|---|---|
| 1 | **在真实 DSH 中加载成功** | ⏳ 待验证 | 重启 DSH → 日志出现 `[dividend-ledger] 已注册 8 条面板路由` → 面板能搜到股票 |
| 2 | 面板核心流程可用 | ⏳ 待验证 | 搜索 → 入账 → 改股数 → 删除 → 看到两个股息率 → 看到待收区块 |
| 3 | 无真实持仓入库 | ✅ 已验证 | `git ls-files \| grep data/real` 必须为空 |
| 4 | 无第三方代码入库 | ✅ 已验证 | `git ls-files \| grep -E 'vendor/\|westock-skill-reference'` 必须为空 |
| 5 | LICENSE 存在 | ✅ 已完成 | MIT，见 [LICENSE](../LICENSE) |
| 6 | `private` 已移除 | ✅ 已完成 | `plugin/package.json` 不再含 `private: true` |

## 二、发布前要改的占位

| 项 | 现状 | 要改成 |
|---|---|---|
| `plugin/package.json` 的 `repository` / `bugs` / `homepage` | `OWNER` 占位 | 你的 GitHub 用户名 |
| `README.md` 里的 `github.com/OWNER/...` | `OWNER` 占位 | 同上 |

一条命令改完（把 `你的用户名` 换掉）：

```bash
cd /Users/jim/Projects/dsh-stock-dividend
grep -rl 'github.com/OWNER' README.md plugin/package.json \
  | xargs sed -i '' 's|github.com/OWNER|github.com/你的用户名|g'
```

## 三、发布前的自检命令

```bash
# 1. 全部验收（6 道）
bash scripts/check.sh

# 2. 确认没有敏感文件被跟踪
git ls-files | grep -E 'data/real|vendor/|westock-skill-reference|\.tmp$' && echo "❌ 有敏感文件！" || echo "✅ 干净"

# 3. 确认 JSON 里没有浮点数（金额必须是十进制字符串）
python3.11 -c "
import json
from decimal import Decimal
from dividend_ledger import report  # noqa
print('见 tests/test_report.py 的确定性用例')
"

# 4. 确认离线路径不联网（不该有子进程）
python3.11 -m unittest tests.test_cli.CandidatesOfflineTest -v
```

## 四、推送到 GitHub

```bash
cd /Users/jim/Projects/dsh-stock-dividend
git remote add origin git@github.com:你的用户名/dsh-dividend-ledger.git
git branch -M main
git push -u origin main
```

**推之前再确认一次**（远程一旦收到，历史就删不干净了）：

```bash
git log --all --oneline          # 确认只有预期的那次提交
git ls-files | wc -l             # 应为 81 左右
git ls-files | grep -c data/real # 必须为 0
```

## 五、给别人用时要知道的

| 事项 | 说明 |
|---|---|
| **Python 3.11+ 必需** | 核心零第三方依赖，但需要 3.11（用了 `dataclass(slots=True)` 等特性） |
| **westock CLI 是可选依赖** | 不装也能用离线功能；联网取数才需要。见 README「关于 westock」 |
| **改了 `lib/` 必须重启 DSH** | 宿主模块有缓存，热改不会重载——这是本项目踩过多次的坑 |
| **数据目录默认是空的** | 仓库不含任何真实数据，用户首次要自己写 `data/real/holdings.json` |

## 六、已知限制（发布说明里要写清楚）

1. 持有期内**股数视为恒定**，不建模加仓/减仓（一阶段的明确假设）。
2. 公开源**无 A 股派息到账日**，只能按除权除息日归月。唯一提供 `pay_date` 的
   Tushare 需 2000 积分（200 元/年），未采用。
3. **预案（`proposed`）状态无法从 westock 取得**——实测 `--all` 也不返回预案行。
   当前只能靠人工写入或换数据源。
4. 分红数据来自公开渠道，**可能有错或滞后**，以交易所与公司公告为准。
