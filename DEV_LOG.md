# 开发日志（product-feed-kr）

## 2026-05-11

### 当前进度摘要

- **抓取**：`wecatalog_scrape_store` 导出 `wecatalog_store_products.json`（分类树、详情、`imgsSrc`/`imgs` 等）。
- **上架**：`seven17_upload` 通过 Playwright 填写 그누보드5 `itemform`（分类、标题、价格、库存、类型、图片等）；商品说明 `it_explan` 默认可由环境变量 `SEVEN17_FILL_IT_EXPLAN` 控制是否写入。
- **价格**：无 API 标价时 JSON 可写入 `optimaPrice: "-1"`；上架侧对无有效标价记录跳过，避免填 0。货源侧标价大量依赖 **标题里的 💰/¥/P 数字**；后续拟统一改为 **AI 从 title 抽取人民币金额**（见下文 §4）。

### 尚待解决 / 差距

#### 1. 图片上传后空白、不可见

- **现象**：后台文件选择已执行，但前台或保存后图片显示为空白、不可见（具体表现需在 headed 模式下对照 DOM / 网络请求确认）。
- **可能方向**（待验证）：
  - 下载图链时的 **Referer / Cookie / User-Agent** 与相册 CDN 策略不匹配，导致实际写入的是空文件或损坏文件；
  - 后台对 **格式 / 体积 / 尺寸** 有校验，上传成功但未通过前端预览；
  - `it_imgN` 与主题脚本（裁剪、AJAX 二次提交）不同步，需按站点实际流程补步骤。
- **下一步**：抓一条失败样本对比「直链下载文件大小 / 魔数」与浏览器打开是否一致；必要时改为 **复用已登录 Playwright 上下文** 对图片 URL `request.get` 或走页面内 fetch。

#### 2. 表单内尺码（或规格）

- **现状**：当前流水线 **未对接** seven17 后台里与尺码、选项相关的字段（若主题为选项科目 `it_option_subject` + SKU 等，脚本未自动拆分）。
- **需求**：从货源标题/详情文案中识别 **尺码表、码数**（如「012 码」「S/M/L」），再映射到后台表单。
- **设想**：引入 **AI / 结构化抽取**（规则 + LLM）：输入标题与可选详情 HTML，输出标准化尺码列表或选项行，再扩展 `_fill_itemform` 或单独一步「选项填写」流程。

#### 3. 商品名称清理

- **现状**：上架标题主要来自微猫 `commodity.title`，仅可做前缀 `WEGO_TITLE_PREFIX`；无统一「去 emoji / 去带货话术 / 长度与违禁词」清洗。
- **需求**：**调用 AI**（或规则管线 + AI 兜底）对 `it_name` 做清理：去除干扰符号、统一格式、符合后台长度与合规要求。
- **设想**：独立模块 `title_clean()`，输入原始 title + 可选策略（韩语站点偏好），输出 `it_name`；配置 API Key 与模型 via 环境变量，默认关闭、开启后上架前自动替换。

#### 4. 价格识别（从标题，拟用 AI）

- **现状**：`wego_commodity.price_from_title_cny` 用少量 **正则** 匹配标题里的 💰、`¥`/`￥`、`RMB`、`P+数字` 等；写法多变、区间价、多国货币混写时容易漏检或误检。
- **需求**：与名称清理一致，**标价也以 title 为主数据源**，在无可靠 `optimaPrice` / `priceArr` 时，用 **AI 结构化抽取**「单一主力人民币售价数字」（或明确输出「无法识别」再走占位 `-1` / 跳过上架策略）。
- **设想**：
  - 输入：原始 `commodity.title`（可选附带截取后的详情纯文本）；输出：`{ "cny_amount": "340" | null, "confidence": ... }`。
  - 接入点：抓取写 JSON 前、或上架 `parse_wego_product` 前统一走一层 `price_from_title_ai()`；与现有正则做成 **可配置**：仅正则 / 正则失败再 AI / 仅 AI。
  - 配置：与标题清理共用或分离模型与 API Key（环境变量），默认关闭，避免无密钥环境报错。

### 参考 `delivery-slip-vlm` 调用 AI（配置与代码范式）

仓库 **`D:\Python\delivery-slip-vlm`** 已有一套 **OpenAI 兼容网关** 用法，可直接照搬思路到本仓库（标题清洗 / 标题抽价 / 尺码结构化等 **纯文本** 或多模态均可）。

#### 对方项目里关键文件

| 路径 | 作用 |
|------|------|
| `D:\Python\delivery-slip-vlm\configs\default.yaml` | 业务参数：`vlm.model`、`temperature`、`timeout_seconds`、`max_workers` 等（**不放密钥**） |
| `D:\Python\delivery-slip-vlm\.env.example` | 网关地址与密钥：**`VLM_BASE_URL`**、**`VLM_API_KEY`**、可选 **`VLM_MODEL`** |
| `D:\Python\delivery-slip-vlm\src\delivery_vlm\config.py` | `load_dotenv` + `load_config(yaml)`；**`vlm_settings()`** 从环境变量读 `VLM_*` |
| `D:\Python\delivery-slip-vlm\src\delivery_vlm\llm\client.py` | **`OpenAICompatClient`**：`openai.OpenAI(api_key=..., base_url=...)`，封装 **`chat_vision`**（图+文） |
| `D:\Python\delivery-slip-vlm\src\delivery_vlm\pipeline\delivery_run.py` | 组装：`vs = vlm_settings()` → `OpenAICompatClient(api_key=..., base_url=...)` → `chat_vision(...)` |

#### 与本仓库对齐：地址和密钥放哪

本仓库已有 **`product_feed_kr/seven17_config.py`**：**环境变量优先**，其次 **`config/seven17.json`**（勿提交真实密钥）。建议在示例里增加与 VLM 同语义的键（见仓库根目录 **`config/seven17.example.json`**）：

- **`OPENAI_BASE_URL`**（或沿用对方命名 **`VLM_BASE_URL`**，只要在代码里 `getenv` 一致即可）：兼容接口根地址，例如 `https://api.openai.com/v1` 或国内兼容网关给出的 `/v1` 前缀 URL。
- **`OPENAI_API_KEY`**：API Key。**更推荐只写在环境变量或本机 `seven17.json`，不要提交 Git。**
- **`OPENAI_MODEL`**：模型名，如 `gpt-4o-mini`、`qwen-turbo`（视网关而定）。

可选：在项目根使用 **`.env`** + `python-dotenv`，与 `delivery-slip-vlm` 一致；本仓库若未全局 `load_dotenv`，需在入口脚本里调用一次，或在 shell / systemd 里 `export`。

依赖：对方使用官方 **`openai`** Python SDK（`pip install openai`）。纯文本对话可在 `OpenAICompatClient` 旁新增类似方法，或直接：

```python
from openai import OpenAI

api_key = ...   # getenv("OPENAI_API_KEY")
base_url = ...  # getenv("OPENAI_BASE_URL") 或 None 表示官方默认
client = OpenAI(api_key=api_key, base_url=base_url)
resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[
        {"role": "system", "content": "你只输出 JSON，字段 cny_amount 为字符串或 null。"},
        {"role": "user", "content": title},
    ],
    temperature=0.1,
)
text = (resp.choices[0].message.content or "").strip()
```

多模态继续用对方的 **`chat_vision`**（传 `image_bytes` + `content_type`）即可。

#### 小结

- **YAML**（delivery-slip-vlm）：调参与模型名等非秘密配置。
- **`.env` / 环境变量 / `seven17.json`**：密钥与 base URL；**密钥优先环境变量**，避免进仓库。
- **客户端**：`OpenAI(api_key=..., base_url=...)` + `chat.completions`（文本）或沿用 **`OpenAICompatClient.chat_vision`**（图像）。

### 备注

- 本文档随迭代更新；解决某项后可将对应小节改为「已解决」并简短记录方案与提交范围。
