# MCP 接入说明

本文档说明本项目实际调用了哪些麦当劳 MCP 能力、如何调用、以及每个调用在业务链路中的位置。

---

## 1. MCP Server

| 项 | 值 |
|---|---|
| Server 名称 | `mcd-mcp` |
| 接入地址 | `https://mcp.mcd.cn` |
| 传输方式 | Streamable HTTP |
| 协议版本 | `2024-11-05` |
| 鉴权方式 | `Authorization: Bearer <MCP_TOKEN>` |
| 状态管理 | 无状态，服务端不返回 `Mcp-Session-Id`，可直接连续调用 |

Token 通过环境变量 `MCD_MCP_TOKEN` 传入，**不写入任何代码或配置文件**。

---

## 2. 使用的 Tool

### 2.1 `query-meals` — 菜单主数据

**作用**：拉取指定门店、指定业务类型下的完整菜单。

**调用参数**：

```json
{
  "storeCode": "3500027",
  "orderType": 1,
  "beType": 1
}
```

**关键返回字段**：

| 字段 | 用途 |
|---|---|
| `data.categories[]` | 菜单分类，用于餐品归类 |
| `data.meals[code].name` | 餐品名称，用于对齐营养表 |
| `data.meals[code].currentPrice` | 菜单现价（**可能含随单购条件**） |
| `data.meals[code].originalPrice` | 划线原价 |
| `data.meals[code].discountType` | 优惠类型，决定价格口径 |
| `data.meals[code].withOrder` | 随单购卡信息 |

**在本项目中的位置**：链路起点，提供全部候选餐品。

---

### 2.2 `query-meal-detail` — 套餐拆解（核心）

**作用**：获取单个餐品的选配结构，把套餐还原成单品清单。

**调用参数**：

```json
{
  "storeCode": "3500027",
  "orderType": 1,
  "beType": 1,
  "code": "9900003424"
}
```

**关键返回结构**：

```
data.rounds[]              选配轮次（主餐 / 小食 / 饮料）
  ├─ name                  轮次名，如「选择早餐套餐饮料」
  └─ choices[]             该轮所有可选商品
       ├─ code             子项 productCode
       ├─ name             子项名称 ← 用于查营养
       ├─ quantity         已选数量   ← 优先依据
       ├─ isDefault        是否推荐项 ← 兜底依据
       └─ diffPrice        相对默认项的差价，如 "+ ¥1"
```

**为什么它是核心**：营养表只有单品数据。没有这一步，所有套餐的营养都是空白——实测本店 78 件餐品中 29 件是套餐，占 37%。

**在本项目中的位置**：把「一个套餐 code」变成「若干单品 code + 名称」，是营养计算的前提。

---

### 2.3 `list-nutrition-foods` — 营养数据

**作用**：一次性拉取全量餐品营养数据，共 158 条。

**调用参数**：无（空对象）。

**返回**：以管道符分隔的文本表格，字段为：

```
productName | nutritionDescription | energyKj | energyKcal |
protein | fat | carbohydrate | sodium | calcium
```

**注意**：返回的是**文本**而非结构化 JSON，需要自行解析。本项目将其固化为 `data/nutrition_raw.tsv`，共 158 行、8 个字段，已校验无残缺。

**在本项目中的位置**：所有热量/蛋白/脂肪/碳水/钠/钙数据的唯一来源。

---

### 2.4 `calculate-price` — 价格核对

**作用**：按实际商品列表计算应付金额，验证本地价格计算是否与收银台一致。

**调用参数**：

```json
{
  "storeCode": "3500027",
  "orderType": 1,
  "beType": 1,
  "items": [{"productCode": "9900003424", "quantity": 1}]
}
```

**关键返回**：`data.price`（现价，单位**分**）、`data.productPrice`、`data.discount`。

**在本项目中的位置**：不是求解必经步骤，而是**价格口径的验证器**。正是靠它发现了「随单购价 ≠ 支付价」的问题（见第 4 节）。

---

### 2.5 `query-nearby-stores` — 门店定位

**作用**：按城市和地标搜索门店，拿到 `storeCode`。

**调用参数**：

```json
{"beType": 1, "searchType": 2, "city": "合肥", "keyword": "天鹅湖"}
```

**返回**：门店列表，含 `storeCode`、`address`、`distance`、营业时间、可预约时段。

**在本项目中的位置**：用户未指定门店时，用于把「我附近」翻译成 `storeCode`。

---

### 2.6 `query-store-coupons` — 门店可用券（预留）

**作用**：查询指定门店可使用的优惠券。

**当前状态**：已接入但未参与求解。测试账号在该门店返回空数组。

**规划**：把券纳入组合优化，可在现有基础上进一步降低实付价。接口已跑通，只差求解器支持。

---

## 3. 完整调用链路

```
① query-nearby-stores        用户说"我附近" → storeCode
        ↓
② query-meals                门店菜单 → 78 件餐品
        ↓
③ query-meal-detail × 29     疑似套餐逐个拆解（并发 6，约 8 秒）
        ↓
④ list-nutrition-foods       158 条营养数据 → 本地 TSV
        ↓
   名称对齐 + 营养计算        （纯本地，无网络）
        ↓
   TDEE + 组合求解             （纯本地，无网络）
        ↓
⑤ calculate-price            抽样核对价格
        ↓
⑥ create-order               （用户确认后下单，本项目不自动执行）
```

**设计取舍**：②③④ 是一次性抓取，结果缓存在 `examples/store_3500027.json`。求解阶段完全离线，原因是：

- 每次求解都现场查 29 个套餐太慢（约 30 秒）
- 营养数据是静态的，没必要重复拉
- 离线可复现，便于测试和评审

菜单更新后重跑 `fetch_mcp.py` 即可刷新缓存。

---

## 4. 踩坑记录

这几条是实际调试中撞出来的，接口文档里没写。

### 4.1 `tools/call` 的响应混着说明文字

`content[0].text` 不是纯 JSON，而是：

```
# 输出格式要求（严格遵守，不可省略）
...
## Original Response

{"success":true,"code":200,...,"supportModify":false}}## 数据传参规则
```

业务 JSON 后面**直接接着**下一段说明，中间**没有换行符**。所以 `json.loads(line)` 会报 `Extra data`。

必须用 `json.JSONDecoder().raw_decode()` 从行首解析、容忍尾部多余字符：

```python
obj, _ = json.JSONDecoder().raw_decode(line)
```

### 4.2 `isDefault` 与 `quantity` 会互相矛盾

「猪柳蛋麦满分套餐」的饮料轮次：

| choice | isDefault | quantity |
|---|---|---|
| 大杯鲜萃咖啡 | 1 | 0 |
| 鲜萃冰咖 | 0 | 1 |

`isDefault=1` 但 `quantity=0`，说明推荐项 ≠ 已选项。必须 **quantity 优先**，整轮全为 0 时才退回 `isDefault`。

### 4.3 随单购价不是支付价

「原味板烧鸡腿炒双蛋堡组合」：

| 来源 | 价格 |
|---|---|
| `query-meals.currentPrice` | 13.20 |
| `query-meals.originalPrice` | 22.00 |
| `calculate-price.price` | **2200 分 = 22.00** |

`discountType` 为 `"随单购早餐卡优惠"`——13.20 的前提是先买早餐卡。若直接拿 `currentPrice` 做预算约束，用户到柜台会发现钱不够。

处理：`discountType` 含「随单购」时，支付价取 `originalPrice`，条件价另存。

### 4.4 营养表按品名索引，菜单按 code 索引

两套体系命名不同步，详见 README 的「名称对齐」章节。核心原则是**宁可返回未知也不猜**。

### 4.5 `query-meal-detail` 对到店自提不能传 `beCode`

到店自取（`beType=1`）场景传了 `beCode` 会报错。只有得来速（`beType=5`）、外送（`beType=2`）、团餐（`beType=6`）才需要。

---

## 5. 业务价值

| MCP 能力 | 单独使用的局限 | 本项目如何放大价值 |
|---|---|---|
| `query-meals` | 只有商品名和价格，用户仍需自己判断 | 与营养数据打通，每件餐品带上热量和宏量营养素 |
| `query-meal-detail` | 用于查看套餐内容、修改搭配 | 用于**营养反解**：把套餐拆成单品再求和，解决 37% 餐品算不出的问题 |
| `list-nutrition-foods` | 一份静态表格，与菜单无法关联 | 建立名称对齐层，让营养数据真正可用 |
| `calculate-price` | 用于下单前算价 | 用于**校验价格口径**，发现条件价陷阱 |
| `query-nearby-stores` | 找店 | 把自然语言位置转成可查询的 `storeCode` |

一句话：**MCP 提供的是「能买什么」，本项目补上「该买什么」。**

---

## 6. 错误处理

| 场景 | 处理方式 |
|---|---|
| Token 无效 / 过期 | `fetch_mcp.py` 捕获 HTTP 401/403，提示检查 `MCD_MCP_TOKEN` |
| 网络超时 | 单请求 30 秒超时，套餐详情并发抓取，失败项跳过并打印警告 |
| 响应非 JSON | 尝试 SSE 与纯 JSON 两种格式，均失败则抛 `McpError` |
| 营养数据缺失 | 标记为 `nutrition=None`，不进入推荐候选，在 `audit` 中列出 |
| 套餐子项查不到 | 尝试同轮替换为可查选项；无替代则整份标记为不可用 |
| 预算内无解 | 返回空方案 + 说明文案，不强行给出超标方案 |
