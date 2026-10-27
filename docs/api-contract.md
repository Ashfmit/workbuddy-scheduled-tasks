# WorkBuddy 积分 / 成长中心接口契约（实测记录）

> 全部结论来自 2026-10-27 在 WorkBuddy **5.9.2** 客户端上的实测，非文档转述。
> 实测机环境：**Windows 11 家庭版（build 26200）**；接口随版本可能变化，对不上时以实测为准，别照抄。

## 通道

| 通道 | 用途 | 说明 |
|---|---|---|
| WBIPC 端上通道 | 加油站签到 | 客户端宿主注入鉴权，`http.fetch` 转发；脚本见 `scripts/checkin_wbipc.py` |
| CDP 注入 → `window.wb.http` | 成长中心全部接口 | 开调试端口后往页面注入 JS，借页面已登录会话发请求；脚本见 `scripts/growth_auto.py` |

两条路都**不读取、不存储登录令牌**。

## 关键教训：方法分工

- **查询类一律 GET，写类一律 POST。**
- 用 POST 打查询路由会得到 **404**（不是参数错，是路由不认这个方法）。
- 排查域名（copilot.tencent.com / www.workbuddy.cn）、前缀（/api/v2、/activity）、
  header（X-User-Id / X-Domain）均排除后，真正的变量是**请求方法**。

## 端点清单

### 签到（billing）

| 方法 | 路径 | 请求体 | 备注 |
|---|---|---|---|
| POST | `/v2/billing/meter/checkin-activity-status` | `{}` | 查状态；`today_checked_in` / `streak_days` / `daily_credit` / `total_credits` |
| POST | `/v2/billing/meter/daily-checkin` | `{}` | 领取；幂等，已签返回空或 400+code 10001，都算成功 |

### 成长中心（activity/growth）

| 方法 | 路径 | 请求体 | 备注 |
|---|---|---|---|
| GET | `/v2/activity/growth/streak` | — | 连登状态、补登卡余额、`redemption_status`（三档兑换状态） |
| GET | `/v2/activity/growth/heatmap` | — | 当月日历 `cells[]`，`score==0` 即断点；含 `today.date` |
| GET | `/v2/activity/growth/lottery/chances` | — | 抽奖机会余额 |
| GET | `/v2/activity/growth/buddy/info` | — | Buddy 状态（active / instance_id） |
| POST | `/v2/activity/growth/makeup-cards/use` | `{target_date:"YYYY-MM-DD"}` | 补登；每轮最多 1 张 |
| POST | `/v2/activity/growth/redeem` | `{tier:"7d"\|"14d"\|"28d", client_token:"u-<uuid>"}` | 连登奖励；**tier 必须传档位标识**，传天数或档位名被拒 |
| POST | `/v2/activity/growth/lottery/draw` | `{client_token:"u-<uuid>"}` | 盲盒；**必须带 client_token**，缺了 400 |

### 字段坑

- `redemption_status` 的档位状态在 `tier_<tier>_status`（如 `tier_14d_status`），
  取值 `claimed` / `locked` / 可领；字段缺失时**不猜**，不对着空气 POST。
- 补登候选条件：当月 + 今天之前 + `score==0` + 不在已补记录里；最近断点优先。
- 补登会改连登天数，所以补过之后要重新拉一次 `streak` 再判断兑换档位。
- 盲盒中实物奖时返回 `need_address`，脚本只提醒、不代填地址。

## 已下线的接口（2026-10-27 实测）

- `/v2/activity/growth/config` —— 404，不存在
- `/v2/activity/banner`、`/v2/activity/workbuddy/banner` —— 活动已下线（返回 activity is offline）

## 怎么自己复验

客户端开着调试端口时，按下面的路子可以重现全部结论（只读探测，不碰写操作）：

1. 连 `127.0.0.1:9222`，取第一个页面 target 的 WebSocket 地址（仓库的 `cdp_client.py` 已封装好）；
2. `Runtime.evaluate` 注入一段 JS，用页面自带的 `window.wb.http` 发请求，`awaitPromise` 收返回值；
3. 对同一路径分别用 GET / POST 打，即可复现「方法分工」那条教训。

最小探针（浏览器控制台或 CDP 注入均可）：

```js
// GET 查状态（查询类用 GET）
window.wb.http.get('/v2/activity/growth/streak').then(d => console.log(d.data));

// POST 写操作（写类用 POST，注意 client_token 防重放）
window.wb.http.post('/v2/activity/growth/redeem',
  {tier: '14d', client_token: 'u-' + crypto.randomUUID()}).then(d => console.log(d.data));
```

> [!WARNING]
> 复验时**先只读后写**：写路径（补登/兑换/抽奖）不可逆，拿准了再调。探测前确认端口开着、页面已登录。
