# HTTP API 参考

默认服务地址为 `http://127.0.0.1:8765`。比较在服务进程所在机器执行，路径采用该机器上的绝对路径。

## 启动与输入来源

在已安装本项目的 Python 环境中启动服务，使用实际存放 PDF 的目录。

```powershell
python -m pdfdiff --allow-root "E:\documents"
```

`--allow-root` 可重复指定。省略时允许启动工作目录及其子目录，显式指定时仅允许所列目录。路径按解析后的实际位置检查，包括符号链接或目录联接的目标。只接受普通本地 PDF，每份最多 32 MiB。URL、UNC 和设备路径均被拒绝。

使用 `GET /api/health` 检查服务可用性。请求使用 `Content-Type: application/json`，本地脚本无需设置 `Origin`。浏览器跨站请求会被拒绝。

每侧来源使用 `{"path":"E:\\documents\\reference.pdf"}` 或 `{"document_id":"uploaded-document-id"}`，二选一。上传 ID 来自 `POST /api/documents?filename=reference.pdf` 的响应 `id`，上传体为 PDF 原始字节。文件位于调用方机器时，可先上传，再提交比较。

## 提交与查询

`POST /api/v1/comparisons` 创建任务，成功返回 HTTP 202。`Location` 响应头与响应中的 `links.self` 指向状态查询地址。

整份文档按同页码比较，要求两侧页数相同。

```json
{
  "reference": {"path": "E:\\documents\\reference.pdf"},
  "candidate": {"path": "E:\\documents\\candidate.pdf"},
  "pages": {"mode": "all"},
  "settings": {"dpi": 150, "ink_threshold": 220}
}
```

省略 `pages` 时默认 `all`。DPI 可取 96、150、200、300、600，默认 150。墨迹阈值为 1 至 254 的整数，默认 220。

指定页面对应关系时使用 `pairs`，页码从 1 开始，按列表顺序生成比较项。最多指定 2000 对，配对不能重复。

```json
{
  "reference": {"path": "E:\\documents\\reference.pdf"},
  "candidate": {"path": "E:\\documents\\candidate.pdf"},
  "pages": {
    "mode": "pairs",
    "pairs": [{"reference_page": 3, "candidate_page": 4}]
  },
  "settings": {"dpi": 300, "ink_threshold": 220},
  "region_mm": [20, 30, 180, 120]
}
```

`region_mm` 位于请求顶层，仅允许用于单个比较项。顺序为 `[x0,y0,x1,y1]`，采用旋转后可见页左上角起算的毫米坐标。省略时比较整页。

可在创建请求中携带 `Idempotency-Key`。键长为 1 至 128，字符限于英文字母、数字和 `._:-`。同键配合相同规范化请求会返回原任务，同键配合不同请求返回 `idempotency_conflict`。网络中断后重试同一次提交时沿用原键，修改或重新生成输入 PDF 后使用新键。

幂等键仅在原任务仍被保留时有效。过期、容量淘汰或服务重启后，同键会创建新任务，并读取提交时的文件内容。持久复核需要自行保存报告、图片和文件哈希。

| 操作 | 路径 | 成功结果 |
| --- | --- | --- |
| 查询状态 | `GET /api/v1/comparisons/{id}` | HTTP 200，轻量状态、进度、汇总及链接 |
| 获取报告 | `GET /api/v1/comparisons/{id}/report` | 终态 HTTP 200，完整或部分报告 |
| 获取已处理项 | `GET /api/v1/comparisons/{id}/pairs/{index}` | HTTP 200，单项详情 |
| 获取图片 | `GET /api/v1/comparisons/{id}/pairs/{index}/images/{kind}` | HTTP 200，PNG |
| 取消任务 | `POST /api/v1/comparisons/{id}/cancel` | HTTP 200，当前状态 |

`index` 从 1 开始，对应请求页面对的列表序号，与两侧实际页码分别记录。图片 `kind` 可为 `reference`、`candidate`、`overlay`、`diff`，按任务保存的文件快照生成。

约一秒间隔轮询状态直至 `status` 为 `completed`、`cancelled` 或 `failed`，长任务可降低查询频率。活动状态为 `queued` 或 `running`。

`completed` 表示所有比较项均已尝试处理，需继续检查 `summary.error_pairs`。取消在当前页面处理结束后生效。`cancelled` 与 `failed` 的报告保留已处理结果，未处理项计入覆盖信息。

活动任务的 `expires_at` 为空。终态结果闲置 60 分钟后过期，最多保留 4 份终态任务，较早记录也可能因容量限制被淘汰。需要留存时及时保存报告和图片。

## 读取报告

状态响应包含 `progress` 与 `summary`，完整报告在终态后单独获取。

| 字段 | 解释 |
| --- | --- |
| `progress.total_pairs` / `processed_pairs` | 请求比较项总数与已处理项数 |
| `progress.succeeded_pairs` / `failed_pairs` | 已成功和失败的比较项数 |
| `progress.current_pair_index` | 当前处理项的列表序号 |
| `summary.visual_changed_pairs` / `visual_unchanged_pairs` | 明确检测到视觉变化或无变化的成功项数 |
| `summary.pairs_with_findings` / `finding_count` / `review_count` | 有诊断的项数、诊断总数与待复核诊断数 |

完整报告另含 `files`、`engine`、`settings`、`page_pairs`、`coverage` 和 `pairs`。`files` 记录文件 SHA-256、大小与读取时间，`engine` 记录应用与测量库的版本。`page_pairs` 保留请求的全部配对，`pairs` 仅包含已处理项。每个 `pairs[]` 提供以下内容。

| 字段 | 解释 |
| --- | --- |
| `index` / `reference_page` / `candidate_page` | 项目序号与两侧实际页码 |
| `status` | `done` 或 `error` |
| `has_visual_difference` | 成功项的视觉差异判断，失败时为空 |
| `report` | 引擎测量与完整 `diagnostics`，失败时为空 |
| `error` | 失败原因，包含 `code`、`detail`、`retryable` |
| `images` | 成功项提供同一输入快照的四种图片地址 |

已处理的错误项通过单项详情接口同样返回 HTTP 200，具体原因见 `status` 和 `error`。其图片请求返回 HTTP 409。

先检查布尔值 `coverage.all_pairs_compared`，以及错误、未处理项的计数 `failed_pairs`、`unprocessed_pairs`，再检查计数 `diagnostics_truncated_pairs`、`text_position_unavailable_pairs`、`content_comparison_incomplete_pairs`。布尔值 `coverage.truncated` 表示诊断或报告达到容量上限，逐项诊断的 `coverage` 和 `limitations` 提供具体原因。

从每个成功项的 `report.diagnostics.findings` 读取诊断。

`coverage.all_pairs_compared` 只表示所有请求配对均成功比较。确认整份文档覆盖时，还需核对 `files.*.page_count`、`page_pairs` 是否覆盖目标页，以及 `settings.region_mm` 是否限定了局部范围。

| 字段 | 调整时的用途 |
| --- | --- |
| `type` / `scope` | 变化类型与测量范围 |
| `reference` / `candidate` | 页码、文字、上下文和 `bbox_mm` |
| `anchor_bbox_mm` | 插入或删除的空侧可能提供相邻文字锚点，位于该侧定位对象中 |
| `measurements` | 参考值、待比较值、差值、单位、统计方法 |
| `target.description` | 需要达到的输出效果 |
| `target.candidate_adjustment` | 可靠位置测量可提供待比较侧的几何修正量 |
| `reliability.level` / `reasons` / `evidence` | 可靠程度、适用条件、匹配覆盖率和残差等依据 |
| `related_findings` | 与该项共同描述同一变化的诊断 ID |
| `source_location` | 当前为空，结合源工程定位 |

常见类型为 `page.geometry`、`text.content`、`text.position`、`text.font_size`、`text.font_name`、`text.reflow`、`text.line_spacing`、`text.line_pattern`、`visual.region`。

位置与行距单位为 mm，字号单位为 PDF pt。`delta` 为待比较减参考，正值分别向右、向下。`candidate_adjustment` 的方向已针对待比较侧计算。例如 `delta=+1 mm` 与 `candidate_adjustment.dx_mm=-1` 表示当前向右偏移，需要向左修正 1 mm。

连续行模式的 `initial_dx_mm`、`initial_dy_mm` 为拟合初始偏移，`per_line_dx_mm`、`per_line_dy_mm` 为逐行增量。查看 `sample_lines`、`fit_residual_p95_mm` 与关联行号后，再选择需要修改的源设置。

`visual.region` 的 `reliability.evidence.crop_px` 为共同画布上的像素框，可用于裁取其 `image_keys` 指定的整页图。毫米框用于定位，像素框用于图片裁取。

多个诊断可能覆盖同一变化。没有诊断条目时仍需检查原始 RGB 变化、测量覆盖和容差。每次修改后固定比较条件，重新读取输入文件并提交新任务复测。

## 处理失败与重试

HTTP 错误采用 `application/problem+json`，根据响应中的稳定 `code` 与说明处理。

| HTTP 状态与 code | 处理方式 |
| --- | --- |
| 422 `invalid_request` | 检查来源、页面列表、DPI、阈值与选区 |
| 404 `source_not_found` | 核对服务机器上的文件路径或上传 ID |
| 403 `source_access_denied` | 核对读取权限和 `--allow-root` 范围 |
| 409 `source_changed` | 文件读取期间变化，生成完成后以新幂等键重新提交 |
| 413 `source_too_large` | 输入超过单文件大小上限 |
| 413 `capacity_exceeded` | 任务源文件超过缓存容量，缩小本次输入范围 |
| 422 `invalid_pdf` | 核对 PDF 是否有效、可直接打开 |
| 422 `page_count_mismatch` | 核对分页变化，明确页面对应关系后使用 `pairs` |
| 409 `comparison_busy` | 等待当前任务结束，再提交所需比较 |
| 409 `idempotency_conflict` | 同一次请求重试沿用原键，新的比较使用新键 |
| 404 `comparison_not_found` | 任务已不存在，需要重新提交 |
| 409 `result_not_ready` | 继续轮询状态，终态后获取报告 |
| 409 `pair_not_ready` | 活动任务可等待进度推进，终态的未处理项需要提交新任务 |
| 404 `pair_not_found` | 核对从 1 开始的比较项序号 |
| 409 `pair_comparison_failed` | 该项图片请求失败，读取单项详情的错误并检查文件、页尺寸或 DPI |
| 404 `image_not_found` | 核对四种受支持的图片类型 |
| 422 `image_render_failed` | 检查页面内容或降低 DPI 后创建新任务 |
| 500 `internal_error` | 检查服务日志，再根据错误原因处理 |

逐项错误根据其 `retryable` 与 `detail` 采取措施，先修正明确的输入或参数问题。调用超时后，以原请求和原幂等键重新提交以取得原任务，再根据其状态决定后续操作。
