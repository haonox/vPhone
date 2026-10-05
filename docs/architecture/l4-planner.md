# L4 纯视觉决策层技术设计

| 属性 | 内容 |
| --- | --- |
| 状态 | 首版已实现；通过单元测试、合成图坐标探针和固定真机任务验收 |
| 目标 | 将自然语言任务与当前截图转换为**最多一个**受验证的 L2 动作，形成单步闭环 |
| 对上接口 | `PlannerSession(...).step()`、`close()`、上下文管理器、`SessionResult` |
| 对下依赖 | L3 `PerceptionEngine`、L2 `ActionExecutor`、L1 `DeviceSession` |
| 模型适配 | `OpenAICompatibleDecisionModel`；端点、模型 ID 与请求选项由 `ModelConfig` 注入 |
| 非目标 | 任意任务的安全自治、批量动作、后台调度、OCR、控件树、自动业务回滚 |

## 1. 设计原则与职责

截图是页面状态的唯一观测输入。L4 每次根据**本轮完整截图**、任务和任务感知轨迹请求模型提出一个函数工具调用。轨迹由此前各轮的页面摘要、决策原因、动作类型和设备命令结果组成，用于说明 Agent 如何沿任务路径到达当前界面；旧截图坐标不会进入后续模型上下文。模型不直接接触 ADB；本地控制器先解析工具名与 JSON 参数，再把具体动作交给 L2。L2 的正常返回只能说明设备命令执行完毕，不能证明目标 UI 发生变化，因此下一步必须重新截图。

```text
用户任务 + 任务感知轨迹 + 当前 PageObservation
              ↓
OpenAICompatibleDecisionModel：截图 + 工具定义 → 一个候选决策
              ↓
tools.py：严格解析工具与参数；coordinates.py：坐标边界检查
              ↓
PlannerSession：会话历史 / 动作种类许可 / 次数与时间预算 / 终止状态
              ↓
ActionExecutor：执行一个 L2 动作
              ↓
重新 observe()；不复用上一步的坐标或假定点击已命中
```

层间分工：`provider.py` 只发送请求并接收候选；`tools.py` 只定义/解析模型协议；`session.py` 决定是否派发；L2 执行；L3 截图。未来更换模型时不应修改 L1–L3。

## 2. 数据契约

### 2.1 模型输入

当前适配器采用 OpenAI 兼容的 Chat Completions 请求格式。每次请求都是由本地重新组装的新调用，不依赖服务端对话记忆；包含任务、当前截图的宽高，以及此前各轮的页面摘要、决策原因、动作类型及其**设备命令**结果。截图以 PNG base64 数据 URL 作为 `user` 图片块发送，图像细节由配置决定；不发送过去截图、不发送 OCR 或控件树。轨迹不发送旧点击或滑动坐标，也不写出此前输入的文本内容。

首个验收配置采用 DeepSeek 的 `deepseek-flash`。其官方文档说明了 PNG 图像与函数工具调用能力：[视觉输入](https://api-docs.deepseek.com/guides/vision/) · [工具调用](https://api-docs.deepseek.com/guides/tool_calls/)。适配器在请求中明确提供截图宽高，要求返回该截图内的像素坐标。配置其他模型时，必须单独核对其对图片块、所配置的图像细节、函数工具调用及可选请求字段的支持；仅更改模型 ID 不保证兼容。

### 2.2 工具与终止决策

九种工具都要求模型提供 `screen_summary` 和 `decision_reason`。前者只描述当前截图中与任务有关的可见内容；后者简要说明本轮决策如何推进或终止任务。两者必须是非空、可打印、最多 1000 字符的单行文本，并与工具的业务参数一起接受本地严格校验。它们不是自由文本回复；已执行步骤会将其作为 `StepRecord` 的一部分输出到 CLI。

| 模型工具 | 转换结果 | 首版校验 |
| --- | --- | --- |
| `tap(x,y)` | `TapAction(Point)` | 坐标为整数且处于当前截图范围 |
| `long_press(x,y)` | `LongPressAction(Point)` | 坐标为整数且处于当前截图范围；固定长按 1000 ms |
| `swipe(start_x,start_y,end_x,end_y,duration_ms)` | `SwipeAction` | 起终点在图内；时长 100–2000 ms |
| `press_key(key)` | `KeyAction` | 仅 `BACK`、`HOME`、`ENTER`、`APP_SWITCH`；无 `POWER` 或任意键码 |
| `input_text(text)` | `TextAction` | 向当前焦点的光标或选区插入非空、可打印、最多 1000 字符的文本；日志不记录内容 |
| `replace_text(text)` | `ReplaceTextAction` | 整体替换当前焦点中的原内容；文本约束与日志脱敏同上 |
| `wait(seconds)` | `WaitAction` | 仅用于截图中可见的加载/处理中状态；有限正数，运行时上限裁剪为 30 秒 |
| `finish(answer)` | `FinishDecision` | 非空答案；必须有本轮观测；答案仍需独立验收 |
| `stop(reason)` | `StopDecision` | 明确停止，不派发设备动作 |

首版只使用**当前截图像素坐标**。`tools_for_screen()` 为本轮截图生成动态 `maximum`，`to_screen_point()` 再次验证整数类型和 `< width/height`；越界或非整数值直接拒绝，不截断、不猜测。三张合成图的像素定位偏差为 3、3、2 像素，固定真机任务也完成。

模型可能输出错误 JSON、额外字段、缺失字段、多个工具调用、未知工具或自然语言文本。遇到这种无效候选，适配器最多使用**同一张截图重新请求一次**，明确说明格式错误；这期间不执行设备动作。第二次仍不合法就停止，绝不从自然语言猜动作或补造缺失坐标。官方文档同样提醒工具参数仍需客户端验证。[Chat Completions 接口](https://api-docs.deepseek.com/api/create-chat-completion/)

### 2.3 控制器返回值

`SessionResult` 包含 `status`、`message`、`steps`、`latest_observation` 和可选的 `terminal_trace`。`steps` 是当前会话的累计设备动作记录，每项 `StepRecord` 保存所依据的观测 ID / 截图哈希、模型生成的页面摘要与决策原因、已派发动作、L2 结果和 Provider 返回的 token usage，并提供统一的脱敏动作与结果描述；CLI 和后续模型轨迹都从该记录生成展示，不直接打印 Provider 的原始响应。`finish` 和模型主动调用的 `stop` 没有设备动作，其屏幕描述和决策原因保存在 `terminal_trace`，CLI 会在终止时输出；其它终止原因没有此字段。如果同一步因工具格式错误发生一次模型重试，usage 记录两次请求的合计。CLI 不保存截图或轨迹。`status=running` 表示本次调用已完成一个动作、会话仍可继续；`status=finished` 仅表示模型根据最后一张截图宣称完成，不是独立的业务证明。其它终止状态为 `stopped`、`action_limit`、`time_limit`、`error`。`terminal` 属性统一说明调用方是否应停止推进，终止后重复调用 `step()` 会返回同一结果且不再访问设备。

### 2.4 运行时任务感知轨迹

规划器在内存中保留本次任务已经执行的步骤。下一轮不会把它们渲染成原始动作列表，而是组合成连续的任务轨迹：当时看到了什么、为什么选择该动作、动作类型以及设备命令结果。当前截图仍是页面现状的唯一依据，过去的页面摘要和命令成功都不能证明当前 UI 状态。进程结束后不会写入 `trajectory.json` 或逐轮截图；此前生成的本地 `traces/` 文件不会自动删除。

## 3. 控制流程与失败语义

`PlannerSession` 在构造时绑定任务与设备，调用方通过唯一的推进接口 `step()` 驱动会话。首次推进会先选择 L1 的无界面输入 helper，并在整个任务期间保持选中；任何终止状态都会隐藏 helper、恢复任务前的输入法，作为上下文管理器退出时也执行相同清理。每次调用重新截图并请求一个决策，最多派发一个动作，然后以 `running` 返回；调用方再次调用才会进入下一轮。默认最多派发 12 个动作、总时间预算 600 秒、动作后等待 0.5 秒再返回；CLI 可通过 `.env` 覆盖这些预算。到达动作上限后，会话仍允许一次新的截图与模型判定：如果模型已经能给出答案，就正常完成；如果还要求动作，则返回 `action_limit`，不执行第 13 次。时间预算在每次观测前和模型返回后检查；单个在途网络请求的超时由 `VPHONE_MODEL_TIMEOUT_SECONDS` 控制，整体预算不是强制中断计时器。

截图失败、模型请求失败、连续两次无效决策或设备动作失败都会以 `error` 结束。唯一自动重试是上文所述的无动作模型格式修复；设备动作绝不自动重试。设备超时可能意味着命令已经部分生效，因此不能盲目重发动作。Provider 不打印原始 Chat Completions；回调 `on_step` 只接收 `StepRecord` 并用于本地结构化进度输出，不应上传截图或敏感文本。

当前 CLI 接收任意任务文本，可使用点击、长按、滑动、按键、文本插入、文本整体替换及等待；等待计入动作次数与总时间预算，完成后重新截图。模型只应在当前画面明确显示加载或处理中状态时等待，不能因为目标控件缺失就盲目等待；长按仅用于当前截图中明确需要按住手势的目标。`replace_text` 只作用于已经聚焦的输入框；规划器仍需先根据截图点击正确字段。当前运行目标是受控模拟器和评测环境，因此没有人工确认工具；任务明确要求的发送或删除操作会直接执行。不要把这个版本用于包含真实账户、支付或重要数据的个人设备。`allowed_kinds` 可在 Python API 中限制动作类别，但不能判断某次点击的业务风险。

桌面导航提示针对当前 Google Pixel Launcher：当前页没有目标应用时，模型应上滑打开包含已安装应用的抽屉，不应在桌面横向翻页浪费动作。

## 4. 文件组织与运行

| 文件 | 职责 |
| --- | --- |
| [`models.py`](../../src/vphone/planner/models.py) | 决策、步骤记录与运行结果 |
| [`config.py`](../../src/vphone/planner/config.py) | 模型端点与请求选项的环境配置及校验 |
| [`protocol.py`](../../src/vphone/planner/protocol.py) | 可替换的模型决策接口 |
| [`coordinates.py`](../../src/vphone/planner/coordinates.py) | 本轮截图像素坐标校验 |
| [`tools.py`](../../src/vphone/planner/tools.py) | 函数工具 schema 与严格解析 |
| [`provider.py`](../../src/vphone/planner/provider.py) | 发送 PNG 截图、接收一个工具调用 |
| [`session.py`](../../src/vphone/planner/session.py) | 有状态的单步观测—决策—派发与预算 |
| [`main.py`](../../src/vphone/main.py) | 接收任务文本的命令行入口 |
| [`scripts/probe_coordinates.py`](../../scripts/probe_coordinates.py) | 无触碰合成图坐标探针 |

```bash
uv sync --extra dev
uv run python scripts/probe_coordinates.py
uv run vphone "在系统设置中查看当前电量"
```

根目录 [`.env.example`](../../.env.example) 是可提交的配置模板；本地 `.env` 已被 Git 忽略。`vphone` 从当前工作目录向上寻找 `.env` 并加载，再由 `ModelConfig.from_env()` 读取配置；已有进程环境变量优先。必填字段是 `VPHONE_API_KEY`、`VPHONE_MODEL_BASE_URL`、`VPHONE_MODEL_ID`。可选字段是 `VPHONE_MODEL_TIMEOUT_SECONDS`（默认 90）、`VPHONE_MODEL_MAX_OUTPUT_TOKENS`（默认 4096）、`VPHONE_MODEL_REASONING_EFFORT` 和 `VPHONE_MODEL_IMAGE_DETAIL`（后两者未设置时不发送）。示例配置的图片细节为 `original`。任务预算可设置 `VPHONE_MAX_ACTIONS`（CLI 默认 20）、`VPHONE_MAX_SECONDS`（默认 600）和 `VPHONE_SETTLE_SECONDS`（默认 0.5）。设备动作不自动重试；CLI 的进度日志输出页面摘要、决策理由、动作描述、设备命令结果与耗时，但不打印输入文本。

运行前应确认手机已授权，必要时设置 `VPHONE_DEVICE_ID`；脚本只在恰有一台 ready 设备时自动选择。真机截图会发给所配置的模型服务，请仅在允许传输当前页面的设备上运行。不要把 `.env`、截图、模型完整请求或响应写进提交及共享日志。更换端点后先运行无触碰坐标探针，再做受限真机验收。

## 5. 验收记录与后续边界

- 离线单测覆盖坐标边界、工具类型与字段校验、单动作闭环、动作上限、多工具调用拒绝、禁用动作不派发等路径；全量测试另覆盖 L1–L3 回归。
- 三张合成 PNG 分别在 `(295,640)`、`(120,250)`、`(470,950)` 放置品红圆点，模型像素误差为 3、3、2；该探针不接触手机。
- 真机 `10AG262J1H003X1` 从桌面开始，模型执行 8 次动作到达系统“电池”页，回答“当前电量为 67%（正在充电）”；结束截图独立核对显示“当前电量 67%（正在充电）”。
- 任务感知轨迹协议加入后，同一模型在三张合成图上均返回有效的页面摘要、决策原因和点击，像素误差为 6、2、6；从真机系统设置页进入电池页后回答“当前电量为 77%，状态为正在充电”，结束截图独立核对一致。
- 多桌面页导航提示加入后，模型从桌面向左翻页，进入“系统”文件夹的第二页找到“设置”；一次误入“蓝心智能”后根据轨迹返回并重新定位“电池”，最终回答“当前电量为 78%（正在充电）”，结束截图独立核对一致。
- 单步 Session 重构后，真机集成测试在第一次 `step()` 派发 HOME，第二次 `step()` 获取独立的新观测并返回 `finished`，验证了会话历史和真实设备动作跨调用保持一致。
- 过程中曾观察到模型给出不符合契约的坐标或字段；控制器均停止，没有自动猜测或执行无效候选。当前可靠性仍需通过多轮、多设备、不同 App 的任务集量化，不能从单次成功推断通用成功率。

后续可评估：页面稳定等待、模型输出一次修复机会、坐标局部放大与裁剪的显式变换、任务级成功率与费用/延迟监控。这些不是首版默认行为。
