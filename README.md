# vPhone

用自然语言驱动 Android 设备。项目处于 `dev` 阶段，当前面向受控模拟器和评测环境；已授权真机仅用于开发验证，尚未发布安装包。

## 当前架构

vPhone 采用**纯视觉感知**：截图是页面状态的唯一观测输入。L3 交付当前完整截图；L4 使用可配置的多模态模型结合截图和用户任务理解页面、提出下一步动作。项目不采集 App 控件树，也不运行 OCR。

```text
L5 接口层       CLI / Python API / 可选 Web UI（后续）
L4 决策层       多模态模型提出动作；本地校验、单步执行、重新观测
L3 感知层       当前截图观测（当前首版）
L2 执行层       校验并派发具体点击、滑动、按键、文本动作
L1 设备层       ADB 设备发现、截图和输入原语
```

运行链路是：采集截图 → L4 请求模型提出一个工具调用 → 本地校验参数和动作范围 → L2 执行一个动作 → 重新截图。L2 的命令返回不代表 UI 效果成功；L4 也不能复用旧截图的坐标。模型报告任务完成仍需依据最终截图核验。

## 开发环境

需要 Python 3.11+、uv、Android Platform Tools，以及已授权的 Android 设备。

```bash
uv sync --extra dev
uv run --extra dev pytest -q
uv run --extra dev ruff check .
uv run --extra dev ruff format --check .
```

## 在受控 Android 设备上运行任务

参考 [`.env.example`](.env.example) 配置项目根目录的 `.env`：`VPHONE_API_KEY`、`VPHONE_MODEL_BASE_URL` 和 `VPHONE_MODEL_ID` 为必填项。已有 `.env` 时补充缺少的字段即可，勿覆盖原有密钥。示例配置使用 DeepSeek；也可配置其他兼容图像输入与函数工具调用的 Chat Completions 服务。`.env` 已加入 `.gitignore`。准备一台 ADB 可见的受控模拟器或已授权测试设备；截图会发送到所配置的模型服务，请勿在包含敏感页面的设备上运行。

```bash
uv run python scripts/probe_coordinates.py  # 仅发送合成图，不操作手机
uv run vphone "在系统设置中查看当前电量"
```

`vphone` 是 `pyproject.toml` 注册的命令，入口为 `vphone.main:main`；也可用 `uv run python -m vphone.main "任务内容"` 启动。多设备时设置 `VPHONE_DEVICE_ID`。单次任务默认最多执行 12 个动作，可在 `.env` 调整运行预算。点击、滑动、按键和文本输入均可由模型提出；进度日志不会打印输入文本。当前版本没有人工确认工具，模型会直接执行任务明确要求的发送或删除等操作，因此只应在受控测试环境中运行，不要用于包含真实账户、支付或重要数据的个人设备。更换模型时还需验证该服务的图片及工具调用协议；配置参数与验收见 [L4 决策层](docs/architecture/l4-planner.md)。

当前版本不保存任务轨迹或截图文件。规划器只在本次运行的内存中保留任务感知轨迹：每个已执行步骤对应的页面摘要、决策原因、动作及设备命令结果。后续模型上下文使用这些任务语义而不是旧截图坐标或已输入文本；终端从同一 `StepRecord` 输出页面摘要、决策原因、当前动作的脱敏描述、命令结果、耗时和模型返回的 token usage，文本动作只显示字符数。已有的本地 `traces/` 目录不会被自动删除，并继续由 Git 忽略。

Python 调用方使用 `PlannerSession` 保存单个任务的运行状态，并重复调用 `step()`；每次调用最多派发一个设备动作，直到返回结果的 `terminal` 为 `True`。规划器会在首次观测前选择项目自带的无界面输入法，并在整个任务中保持选中，避免聚焦输入框时弹出软键盘；任务终止或上下文退出时恢复此前的输入法。

## 真机观察示例

```python
from vphone.device import AdbDeviceBackend
from vphone.perception import PerceptionEngine

backend = AdbDeviceBackend()
devices = backend.list_devices()

with backend.open(devices[0].device_id) as device:
    observation = PerceptionEngine().observe(device)

print(observation.screen.width, observation.screen.height)
print(observation.screen.sha256, observation.observation_id)
```

示例仅用于本地调试。截图可能包含个人信息，不要直接写入共享日志或提交到仓库。真实设备只读集成测试需显式设置设备 ID：

```bash
VPHONE_DEVICE_ID=<设备序列号> uv run --extra dev pytest -q -m android
```

技术设计见 [L1 设备层](docs/architecture/l1-device.md)、[L2 执行层](docs/architecture/l2-action.md)、[L3 感知层](docs/architecture/l3-perception.md)和 [L4 决策层](docs/architecture/l4-planner.md)；开发流程见 [开发规范](DEVELOPMENT.md)。
